"""SEC filing evidence and canonical-event normalization, without I/O."""

from hashlib import sha256

from eie.core.errors import TemporalConsistencyError
from eie.core.quality import ConfirmationState, QualityFlag
from eie.core.serialization import canonical_json_bytes
from eie.entities.sec import SecFilerRevision, SecIssuerRevision
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent, StructuredFact
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.filings.models import AmendmentStatus, FilingMetadata
from eie.filings.store import InMemoryFilingStore
from eie.providers.sec.models import SecFilingItem, SecSnapshot
from eie.providers.sec.parser import SUBMISSIONS_VERSION
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

ADAPTER_VERSION = "sec-edgar-1"


def source_identity() -> SourceIdentity:
    return SourceIdentity(
        "SEC_EDGAR", SUBMISSIONS_VERSION, ADAPTER_VERSION, SourceClass.OFFICIAL_PRIMARY
    )


def issuer_observation(
    snapshot: SecSnapshot, observed_ns: int, ingested_ns: int
) -> RawSourceObservation:
    fragment = {
        "cik": snapshot.cik.digits,
        "name": snapshot.company_name,
        "aliases": snapshot.aliases,
        "ticker_exchange": snapshot.ticker_exchange,
        "entity_type": snapshot.entity_type,
    }
    return RawSourceObservation(
        source_identity(),
        f"CIK{snapshot.cik.digits}:issuer",
        "1",
        sha256(canonical_json_bytes(fragment)).hexdigest(),
        "application/json",
        observed_ns,
        ingested_ns,
        source_reference=snapshot.source_reference,
        raw_entity_identifiers=(snapshot.cik.digits,),
        metadata=(("sec_resource", snapshot.kind.value), ("sec_schema", SUBMISSIONS_VERSION)),
    )


def filing_observation(
    item: SecFilingItem, observed_ns: int, ingested_ns: int
) -> RawSourceObservation:
    if item.accepted_at_ns > observed_ns:
        raise TemporalConsistencyError("SEC filing acceptance follows source observation")
    return RawSourceObservation(
        source_identity(),
        item.accession.formatted,
        "1",
        item.fragment_hash,
        "application/json",
        observed_ns,
        ingested_ns,
        source_reference=item.source_reference,
        raw_event_type=item.form.raw,
        raw_entity_identifiers=(item.cik.digits,),
        metadata=(("sec_schema", SUBMISSIONS_VERSION),),
    )


def amendment_relation(
    item: SecFilingItem, filings: InMemoryFilingStore, as_of_ns: int
) -> tuple[AmendmentStatus, str | None, tuple[str, ...]]:
    if not item.form.is_amendment:
        return AmendmentStatus.NOT_AMENDMENT, None, ()
    if item.report_period is None:
        return AmendmentStatus.UNRESOLVED, None, ()
    candidates = tuple(
        sorted(
            prior.filing_id
            for prior in filings.known_as_of(
                as_of_ns, cik=item.cik, base_form=item.form.base, amendment=False
            )
            if prior.report_period == item.report_period
            and prior.accepted_at_ns < item.accepted_at_ns
        )
    )
    if len(candidates) == 1:
        return AmendmentStatus.LINKED, candidates[0], ()
    if len(candidates) > 1:
        return AmendmentStatus.AMBIGUOUS, None, candidates
    return AmendmentStatus.UNRESOLVED, None, ()


def normalize_filing(
    item: SecFilingItem,
    raw: RawSourceObservation,
    filer: SecFilerRevision,
    issuer: SecIssuerRevision | None,
    filings: InMemoryFilingStore,
    observed_ns: int,
    ingested_ns: int,
) -> tuple[FilingMetadata, CanonicalEvent] | None:
    previous = filings.get(item.filing_id, ingested_ns)
    if previous is not None and previous.source_observation_id == raw.observation_id:
        return None
    status, target, candidates = amendment_relation(item, filings, ingested_ns)
    revision = 1 if previous is None else previous.revision_number + 1
    flags = frozenset(
        ({QualityFlag.CORRECTED} if previous is not None else set())
        | ({QualityFlag.ENTITY_UNRESOLVED} if issuer is None else set())
    )
    metadata = FilingMetadata(
        item.filing_id,
        item.accession,
        item.cik,
        None if issuer is None else issuer.issuer_id,
        filer.filer_id,
        item.form,
        item.filing_date,
        item.accepted_at_ns,
        item.report_period,
        item.primary_document,
        item.file_number,
        item.source_reference,
        raw.observation_id,
        ingested_ns,
        revision,
        status,
        target,
        candidates,
        flags,
    )
    values: dict[str, str | bool] = {
        "accession": item.accession.formatted,
        "filer_cik": item.cik.digits,
        "base_form": item.form.base,
        "filing_date": item.filing_date,
        "form": item.form.raw,
        "is_amendment": item.form.is_amendment,
        "primary_document": item.primary_document,
        "amendment_status": status.value,
    }
    if item.report_period is not None:
        values["report_period"] = item.report_period
    if item.file_number is not None:
        values["file_number"] = item.file_number
    if target is not None:
        values["amends_filing_id"] = target
    if issuer is not None:
        values["issuer_cik"] = issuer.cik.digits
    identity = EventIdentity(
        EventType(EventFamily.SEC_FILING, item.form.event_code),
        EventScope(
            ScopeKind.COMPANY if issuer is not None else ScopeKind.SEC_FILER,
            (issuer.issuer_id if issuer is not None else filer.filer_id,),
        ),
        f"sec:{item.cik.digits}:{item.accession.formatted}",
    )
    event = CanonicalEvent(
        identity,
        EventRevision(
            revision,
            RevisionKind.ORIGINAL if previous is None else RevisionKind.CORRECTION,
            ingested_ns,
            None if previous is None else revision - 1,
            reason_codes=() if previous is None else ("SEC_SOURCE_METADATA_CORRECTION",),
        ),
        LifecycleState.RELEASED if previous is None else LifecycleState.UPDATED,
        ConfirmationState.CONFIRMED_PRIMARY,
        (raw.observation_id,),
        frozenset({SourceClass.OFFICIAL_PRIMARY}),
        f"SEC {item.form.raw} {item.accession.formatted}",
        source_observed_at_ns=observed_ns,
        ingested_at_ns=ingested_ns,
        effective_at_ns=item.accepted_at_ns,
        facts=tuple(StructuredFact(key, values[key]) for key in sorted(values)),
        quality_flags=flags,
    )
    return metadata, event
