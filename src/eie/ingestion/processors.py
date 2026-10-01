"""Bridges to stable offline adapters; scheduling never parses source content."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol, cast

from eie.core.errors import EIEError, ValidationError
from eie.filings.models import FilingMetadata
from eie.ingestion.models import Cursor, CursorKind, FailureClass, label
from eie.persistence.sessions import Session, contracts
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.common.ingestion import IngestionDiagnostics, OfficialSourceIngestor
from eie.providers.common.protocols import OfficialAdapter
from eie.providers.corporate.adapter import (
    CorporateAdapter,
    CorporateDiagnostics,
    CorporateIngestor,
)
from eie.providers.estimates.adapter import EstimateAdapter, EstimateDiagnostics, EstimateIngestor
from eie.providers.sec.adapter import SecAdapter, SecDiagnostics, SecIngestor
from eie.providers.sec.models import CIK
from eie.runtime.clock import RuntimeClock, SystemClock
from eie.sources.models import RawSourceObservation


class IngestionFailure(EIEError):
    def __init__(self, category: FailureClass) -> None:
        super().__init__(category.value)
        self.category = category


@dataclass(frozen=True, slots=True)
class PreparedBatch:
    session: Session
    items_seen: int
    raw_created: int
    domain_created: int
    duplicates: int
    cursor: Cursor | None = None
    last_source_item: str | None = None
    parse_ns: int = 0
    normalize_ns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(
            self.session, (SecIngestor, CorporateIngestor, EstimateIngestor, OfficialSourceIngestor)
        ):
            raise ValidationError("typed prepared domain session required")
        for count in (
            self.items_seen,
            self.raw_created,
            self.domain_created,
            self.duplicates,
            self.parse_ns,
            self.normalize_ns,
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValidationError("nonnegative prepared counters required")
        if self.cursor is not None and not isinstance(self.cursor, Cursor):
            raise ValidationError("typed prepared cursor required")
        if self.last_source_item is not None:
            label(self.last_source_item)


class IngestionProcessor(Protocol):
    provider_id: str
    adapter_version: str
    namespace: str

    def initial_session(self) -> Session: ...
    def prepare(
        self,
        session: Session,
        payload: bytes,
        observed_ns: int,
        ingested_ns: int,
        *,
        boundary: Callable[[str], None] | None = None,
    ) -> PreparedBatch: ...


class AdapterProcessor:
    """Copy-on-write boundary also makes the EIE-2 multi-item path atomic."""

    def __init__(
        self,
        namespace: str,
        session: Session,
        adapter: SecAdapter | CorporateAdapter | EstimateAdapter | OfficialAdapter,
        *,
        source_url: str | None = None,
        expected_cik: CIK | None = None,
        clock: RuntimeClock | None = None,
    ) -> None:
        label(namespace)
        if isinstance(session, SecIngestor):
            valid = isinstance(adapter, SecAdapter) and session.settings.sec.enabled
            if source_url is None or session.settings.sec.user_agent is not None:
                raise ValidationError(
                    "SEC runtime requires endpoint and separate operational User-Agent"
                )
        elif isinstance(session, CorporateIngestor):
            valid = isinstance(adapter, CorporateAdapter) and session.settings.corporate.enabled
        elif isinstance(session, EstimateIngestor):
            valid = (
                isinstance(adapter, EstimateAdapter)
                and session.engine.settings.expectations.enabled
            )
        else:
            valid = (
                not isinstance(adapter, (SecAdapter, CorporateAdapter, EstimateAdapter))
                and adapter.provider_id in session.settings.official.enabled_ids
            )
        if not valid:
            raise ValidationError("compatible enabled domain adapter/session required")
        self.namespace, self.provider_id = namespace, adapter.provider_id
        self.adapter_version = getattr(
            adapter,
            "adapter_version",
            "corporate-extract-1"
            if isinstance(adapter, CorporateAdapter)
            else "estimate-extract-1",
        )
        self._seed, self._adapter = deepcopy(session), adapter
        self._settings = (
            session.engine.settings if isinstance(session, EstimateIngestor) else session.settings
        )
        self._url, self._cik = source_url, expected_cik
        self.clock = clock or SystemClock()

    def initial_session(self) -> Session:
        return deepcopy(self._seed)

    def prepare(
        self,
        session: Session,
        payload: bytes,
        observed_ns: int,
        ingested_ns: int,
        *,
        boundary: Callable[[str], None] | None = None,
    ) -> PreparedBatch:
        if type(session) is not type(self._seed):
            raise IngestionFailure(FailureClass.CONFIGURATION_FAILURE)
        settings = (
            session.engine.settings if isinstance(session, EstimateIngestor) else session.settings
        )
        if settings != self._settings:
            raise IngestionFailure(FailureClass.CONFIGURATION_FAILURE)
        staged = deepcopy(session)
        parse_started = self.clock.monotonic_ns()
        try:
            if isinstance(self._adapter, SecAdapter):
                self._adapter.parse(payload, cast(str, self._url), expected_cik=self._cik)
            else:
                self._adapter.parse(payload)
        except ProviderSchemaMismatchError as exc:
            raise IngestionFailure(FailureClass.SCHEMA_MISMATCH) from exc
        except ProviderParseError as exc:
            raise IngestionFailure(FailureClass.PARSE_FAILURE) from exc
        except EIEError as exc:
            raise IngestionFailure(FailureClass.SCHEMA_MISMATCH) from exc
        parse_ns = self.clock.monotonic_ns() - parse_started
        if boundary is not None:
            boundary("after_parse")
        normalize_started = self.clock.monotonic_ns()
        if isinstance(staged, SecIngestor):
            diagnostic = staged.ingest_payload(
                payload, cast(str, self._url), observed_ns, ingested_ns, expected_cik=self._cik
            )
            created = diagnostic.filings_created_or_updated + diagnostic.issuers_created_or_updated
        elif isinstance(staged, CorporateIngestor):
            diagnostic_c = staged.ingest_payload(
                cast(CorporateAdapter, self._adapter), payload, observed_ns, ingested_ns
            )
            created = diagnostic_c.events_created_or_updated
            return self._result(staged, diagnostic_c, created, parse_ns, normalize_started)
        elif isinstance(staged, EstimateIngestor):
            diagnostic_e = staged.ingest_payload(
                cast(EstimateAdapter, self._adapter), payload, observed_ns, ingested_ns
            )
            created = diagnostic_e.observations_created
            return self._result(staged, diagnostic_e, created, parse_ns, normalize_started)
        else:
            diagnostic_o = staged.ingest_payload(
                cast(OfficialAdapter, self._adapter), payload, observed_ns, ingested_ns
            )
            created = diagnostic_o.canonical_created + diagnostic_o.schedule_revisions_created
            return self._result(staged, diagnostic_o, created, parse_ns, normalize_started)
        return self._result(staged, diagnostic, created, parse_ns, normalize_started)

    def _result(
        self,
        session: Session,
        diagnostic: SecDiagnostics
        | CorporateDiagnostics
        | EstimateDiagnostics
        | IngestionDiagnostics,
        created: int,
        parse_ns: int,
        normalize_started: int,
    ) -> PreparedBatch:
        error = getattr(diagnostic, "error_category", None)
        if error is not None:
            category = {
                "SCHEMA_MISMATCH": FailureClass.SCHEMA_MISMATCH,
                "PARSE_ERROR": FailureClass.PARSE_FAILURE,
                "RATE_LIMITED": FailureClass.RATE_LIMIT,
                "UNAVAILABLE": FailureClass.SOURCE_UNAVAILABLE,
            }.get(str(error), FailureClass.NORMALIZATION_FAILURE)
            raise IngestionFailure(category)
        raw = [
            v
            for v in contracts(session)
            if isinstance(v, RawSourceObservation) and v.source.name == self.provider_id
        ]
        last = max(raw, key=lambda v: (v.ingested_at_ns, v.source_item_id), default=None)
        item = None if last is None else last.source_item_id
        cursor = None if item is None else Cursor(CursorKind.SOURCE_ITEM, item)
        if isinstance(session, SecIngestor):
            filings = [v for v in contracts(session) if isinstance(v, FilingMetadata)]
            filing = max(
                filings, key=lambda v: (v.accepted_at_ns, v.accession.formatted), default=None
            )
            cursor = (
                None if filing is None else Cursor(CursorKind.ACCESSION, filing.accession.formatted)
            )
            if filing is not None:
                item = filing.accession.formatted
        return PreparedBatch(
            session,
            diagnostic.items_seen,
            diagnostic.raw_created,
            created,
            diagnostic.duplicates_suppressed,
            cursor,
            item,
            parse_ns,
            self.clock.monotonic_ns() - normalize_started,
        )
