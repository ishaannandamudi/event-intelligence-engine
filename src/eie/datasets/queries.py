"""Build-bound historical views; local provenance never becomes a fake past observation."""

from dataclasses import replace

from eie.core.errors import PersistenceError
from eie.core.time import require_ns
from eie.datasets.models import HistoricalFact, semantic_record
from eie.datasets.repository import DatasetRepository
from eie.entities.models import Issuer, Listing, Security
from eie.entities.registry import InMemoryEntityRegistry
from eie.events.models import CanonicalEvent
from eie.events.scope import ApplicabilityQuery, applies_directly
from eie.filings.models import AmendmentStatus, FilingMetadata
from eie.providers.sec.models import CIK
from eie.replay.models import KnownEvents


class DatasetReader:
    def __init__(self, repository: DatasetRepository, build_id: str) -> None:
        self.repository, self.build_id = repository, build_id
        manifest = repository.manifest(build_id)
        if manifest is None:
            raise PersistenceError("dataset query requires an explicitly finalized build")
        self.manifest = manifest

    def records(
        self,
        as_of_ns: int,
        *,
        kind: str | None = None,
        provider: str | None = None,
        resource: str | None = None,
        latest: bool = False,
    ) -> tuple[HistoricalFact, ...]:
        require_ns(as_of_ns)
        values = tuple(
            f
            for f in self.repository.iter_facts(
                self.build_id, as_of_ns=as_of_ns, kind=kind, provider=provider, resource=resource
            )
            if f.source_available_ns is not None
            and f.source_available_ns <= as_of_ns
            and (kind is None or type(f.record).__name__ == kind)
            and (provider is None or f.provider_id == provider)
            and (resource is None or f.resource_id == resource)
        )
        ordered = sorted(
            values,
            key=lambda f: (
                f.provider_id,
                f.logical_id,
                f.source_available_ns or 0,
                f.source_revision,
                f.fact_id,
            ),
        )
        if latest:
            selected: dict[tuple[str, str, str], HistoricalFact] = {}
            for f in ordered:
                key = (f.provider_id, type(f.record).__name__, f.logical_id)
                prior = selected.get(key)
                if (
                    prior is not None
                    and (prior.source_revision, prior.source_available_ns)
                    == (f.source_revision, f.source_available_ns)
                    and semantic_record(prior.record) != semantic_record(f.record)
                ):
                    raise PersistenceError(
                        "historical revision conflict requires explicit resolution"
                    )
                if prior is None or (f.source_revision, f.source_available_ns or 0) > (
                    prior.source_revision,
                    prior.source_available_ns or 0,
                ):
                    selected[key] = f
            return tuple(selected[key] for key in sorted(selected))
        return tuple(ordered)

    def events_known_at(
        self,
        as_of_ns: int,
        query: ApplicabilityQuery | None = None,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> KnownEvents:
        if start_ns is not None:
            require_ns(start_ns)
        if end_ns is not None:
            require_ns(end_ns)
        if start_ns is not None and end_ns is not None and end_ns < start_ns:
            raise PersistenceError("historical event range reversed")
        selected: dict[str, tuple[int, CanonicalEvent]] = {}
        for fact in self.records(as_of_ns, kind="CanonicalEvent", latest=True):
            event = fact.record
            assert isinstance(event, CanonicalEvent) and fact.source_available_ns is not None
            # Local clock fields are absent in this source-availability projection.
            projected = replace(
                event,
                revision=replace(event.revision, available_from_ns=fact.source_available_ns),
                source_observed_at_ns=None,
                ingested_at_ns=None,
                last_updated_at_ns=None,
            )
            if query is not None and not applies_directly(projected.identity.scope, query):
                continue
            relevant = (
                projected.effective_at_ns
                if projected.effective_at_ns is not None
                else projected.scheduled_for_ns
            )
            if (start_ns is not None or end_ns is not None) and (
                relevant is None
                or (start_ns is not None and relevant < start_ns)
                or (end_ns is not None and relevant >= end_ns)
            ):
                continue
            order = self.manifest.job.definition.settings.source_precedence.order_for(
                event.identity.event_type.code
            )
            rank = order.index(fact.raw.source.source_class)
            if projected.event_id not in selected or rank < selected[projected.event_id][0]:
                selected[projected.event_id] = rank, projected
        return KnownEvents(as_of_ns, tuple(selected[key][1] for key in sorted(selected)))

    def filings(self, cik: CIK, as_of_ns: int) -> tuple[FilingMetadata, ...]:
        filings = []
        for fact in self.records(as_of_ns, kind="FilingMetadata", latest=True):
            metadata = fact.record
            assert isinstance(metadata, FilingMetadata) and fact.source_available_ns is not None
            if metadata.cik == cik:
                filings.append(replace(metadata, available_from_ns=fact.source_available_ns))
        ordered = sorted(filings, key=lambda f: (f.accepted_at_ns, f.filing_id))
        result: list[FilingMetadata] = []
        for metadata in ordered:
            if metadata.form.is_amendment and metadata.report_period is not None:
                candidates = tuple(
                    sorted(
                        f.filing_id
                        for f in result
                        if not f.form.is_amendment
                        and f.form.base == metadata.form.base
                        and f.report_period == metadata.report_period
                        and f.accepted_at_ns < metadata.accepted_at_ns
                    )
                )
                status = (
                    AmendmentStatus.LINKED
                    if len(candidates) == 1
                    else AmendmentStatus.AMBIGUOUS
                    if len(candidates) > 1
                    else AmendmentStatus.UNRESOLVED
                )
                metadata = replace(
                    metadata,
                    amendment_status=status,
                    amends_filing_id=candidates[0] if len(candidates) == 1 else None,
                    amendment_candidates=candidates if len(candidates) > 1 else (),
                )
            result.append(metadata)
        return tuple(result)

    def registry(self, as_of_ns: int) -> InMemoryEntityRegistry:
        registry = InMemoryEntityRegistry()
        facts = self.records(as_of_ns, latest=True)
        for fact in facts:
            if isinstance(fact.record, Issuer):
                registry.add_issuer(fact.record)
        for fact in facts:
            if isinstance(fact.record, Security):
                registry.add_security(fact.record)
        for fact in facts:
            if isinstance(fact.record, Listing):
                assert fact.source_available_ns is not None
                registry.add_listing(
                    replace(fact.record, available_from_ns=fact.source_available_ns)
                )
        return registry
