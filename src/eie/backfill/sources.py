"""Provider-neutral historical pages and audited contract-extract normalization."""

from dataclasses import dataclass, field, fields, is_dataclass, replace
from threading import Event
from typing import Protocol, cast

from eie.backfill.models import CoverageClaim, GapReason, Partition, SourceSpec, cursor_value
from eie.config.settings import Settings
from eie.core.errors import PersistenceError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.datasets.models import DomainRecord, HistoricalFact, HistoricalInput
from eie.entities.models import Listing
from eie.ingestion.models import label
from eie.persistence import codec
from eie.sources.models import RawSourceObservation
from eie.transport.errors import AcquisitionCancelled


@dataclass(frozen=True, slots=True)
class HistoricalPage:
    body: bytes = field(repr=False)
    source_reference: str
    content_type: str = "application/json"
    next_cursor: str | None = None
    coverage: CoverageClaim = CoverageClaim.UNKNOWN
    too_many_results: bool = False
    original_acquired_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes) or not isinstance(self.coverage, CoverageClaim):
            raise ValidationError("typed historical page required")
        label(self.source_reference)
        label(self.content_type)
        if not isinstance(self.too_many_results, bool):
            raise ValidationError("typed historical split signal required")
        if self.original_acquired_ns is not None:
            require_ns(self.original_acquired_ns)
        if self.next_cursor is not None:
            cursor_value(self.next_cursor)


class HistoricalGapError(ValidationError):
    def __init__(self, reason: GapReason) -> None:
        super().__init__("recorded historical coverage gap")
        self.reason = reason


class HistoricalSource(Protocol):
    spec: SourceSpec

    def fetch(self, partition: Partition, cursor: str | None, stop: Event) -> HistoricalPage: ...


class HistoricalProcessor(Protocol):
    adapter_version: str
    parser_version: str

    def normalize(
        self, page: HistoricalPage, partition: Partition, acquired_ns: int, settings: Settings
    ) -> tuple[HistoricalFact, ...]: ...


def logical_identity(record: DomainRecord) -> str:
    nested = getattr(record, "event", None)
    if nested is not None:
        return cast(str, nested.event_id)
    if isinstance(record, Listing):
        return "listing_" + fingerprint(
            "eie:historical-listing:v1",
            (record.security_id, record.ticker, record.exchange, record.valid_from_ns),
        )
    for name in (
        "event_id",
        "filing_id",
        "corporate_id",
        "stream_id",
        "listing_id",
        "security_id",
        "issuer_id",
    ):
        value = getattr(record, name, None)
        if isinstance(value, str):
            return value
    raise ValidationError("historical contract has no stable logical identity")


def reclock(value: object, acquired_ns: int, evidence_id: str) -> object:
    """Normalize local clocks honestly, retaining source publication/vintage fields."""
    if is_dataclass(value) and not isinstance(value, type):
        updates: dict[str, object] = {}
        for f in fields(value):
            if not f.init:
                continue
            old = getattr(value, f.name)
            if f.name in (
                "source_observed_at_ns",
                "ingested_at_ns",
                "available_from_ns",
                "last_updated_at_ns",
            ):
                updates[f.name] = acquired_ns
            elif f.name in ("source_observation_id", "observation_id", "selected_observation_id"):
                updates[f.name] = evidence_id
            elif f.name == "evidence_observation_ids":
                updates[f.name] = (evidence_id,)
            else:
                updates[f.name] = reclock(old, acquired_ns, evidence_id)
        return replace(value, **updates)
    if isinstance(value, tuple):
        return tuple(reclock(v, acquired_ns, evidence_id) for v in value)
    return value


class ContractHistoryProcessor:
    """Explicit offline audited typed extract, never an unaudited live collector."""

    def __init__(
        self,
        adapter_version: str = "historical-contract-1",
        parser_version: str = "historical-contract-json-1",
    ) -> None:
        label(adapter_version)
        label(parser_version)
        self.adapter_version, self.parser_version = adapter_version, parser_version

    def normalize(
        self, page: HistoricalPage, partition: Partition, acquired_ns: int, settings: Settings
    ) -> tuple[HistoricalFact, ...]:
        if page.content_type != "application/json":
            raise ValidationError("audited extract requires JSON media type")
        try:
            values = codec.loads(page.body)
        except PersistenceError as exc:
            raise ValidationError("historical contract extract schema mismatch") from exc
        if not isinstance(values, tuple) or any(not isinstance(v, HistoricalInput) for v in values):
            raise ValidationError("historical extract must contain typed audited inputs")
        result = []
        for value in values:
            if value.source.name != partition.provider_id:
                raise ValidationError("historical provider disagrees with registration")
            if not partition.interval.contains(value.range_at_ns):
                continue
            evidence_record = getattr(value.record, "event", value.record)
            evidence = getattr(evidence_record, "evidence_observation_ids", ())
            if len(evidence) > 1:
                raise ValidationError("audited interchange requires one source fragment per record")
            classes = getattr(
                evidence_record, "source_classes", frozenset({value.source.source_class})
            )
            if classes != frozenset({value.source.source_class}):
                raise ValidationError("audited extract source claims disagree with evidence")
            context = getattr(value.record, "source", None)
            if getattr(context, "provider_id", value.source.name) != value.source.name:
                raise ValidationError("audited provider context disagrees with source identity")
            source = replace(value.source, adapter_version=self.adapter_version)
            raw = RawSourceObservation(
                source,
                value.item_id,
                value.item_version,
                value.fragment_hash,
                page.content_type,
                acquired_ns,
                acquired_ns,
                source_reference=page.source_reference,
            )
            record = cast(DomainRecord, reclock(value.record, acquired_ns, raw.observation_id))
            result.append(
                HistoricalFact(
                    partition.provider_id,
                    partition.resource_id,
                    logical_identity(record),
                    value.source_revision,
                    value.source_available_ns,
                    value.quality,
                    raw,
                    record,
                    self.adapter_version,
                    self.parser_version,
                )
            )
        return tuple(result)


class FixtureHistoricalSource:
    def __init__(
        self,
        spec: SourceSpec,
        pages: dict[tuple[str, int, int, str | None], HistoricalPage | Exception],
    ) -> None:
        self.spec, self._pages = spec, dict(pages)
        self.requests = 0

    def fetch(self, partition: Partition, cursor: str | None, stop: Event) -> HistoricalPage:
        if stop.is_set():
            raise AcquisitionCancelled("historical source stopped")
        self.requests += 1
        value = self._pages.get(
            (partition.resource_id, partition.interval.start_ns, partition.interval.end_ns, cursor)
        )
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ValidationError("unconfigured historical fixture page")
        return value
