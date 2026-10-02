"""Deterministic offline EIE-11 ingestion, conflict, correction, replay and restart."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.core.fingerprints import fingerprint
from eie.core.quality import ConfirmationState
from eie.core.serialization import canonical_json_bytes
from eie.corporate.models import CorporateIdentity
from eie.corporate.types import CorporateKind, EarningsPeriod
from eie.entities.models import issuer_id
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.evidence.claims import Claim, ClaimSubject, TimePrecision, TimeValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.matching import EventDescriptor
from eie.evidence.queries import EvidenceQueries
from eie.evidence.references import (
    AcquisitionLineage,
    EvidenceDataset,
    EvidenceLocator,
    LocatorKind,
)
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimStatus, ClaimType, Predicate, SubjectKind
from eie.evidence.validation import validate_store
from eie.ingestion.models import RetentionPermission
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.common.models import parse_aware_ns
from eie.providers.evidence.models import ClaimInput, EvidenceBatch, EvidenceProvider
from eie.providers.evidence.normalization import field_reference
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

DATASET = EvidenceDataset("offline-example", fingerprint("eie:demo-dataset:v1", "structured"))
ISSUER = issuer_id("SEC_CIK", "0000320193")
PERIOD = EarningsPeriod(2026, 3, "2026-09-30")
CORPORATE = CorporateIdentity(CorporateKind.EARNINGS_SCHEDULE, ISSUER, PERIOD.key)
EVENT_TYPE = EventType(EventFamily.EARNINGS, "EARNINGS.SCHEDULE")
TIME = TimeValue(
    TimePrecision.EXACT, "2026-10-30", parse_aware_ns("2026-10-30T20:00:00Z", "schedule")
)
CLAIM = Claim(
    ClaimType.EARNINGS_SCHEDULE,
    ClaimSubject(SubjectKind.CORPORATE, CORPORATE.corporate_id),
    Predicate.SCHEDULED_FOR,
)


def item_payload(batch: EvidenceBatch) -> bytes:
    return canonical_json_bytes(
        {
            "scheduled_for": batch.claims[0].value,
            "status": batch.claims[0].status,
            "version": batch.events[0].revision.number,
        }
    )


def source(
    name: str, source_class: SourceClass = SourceClass.SECONDARY_NEWS, *, family: str | None = None
) -> EvidenceProvider:
    return EvidenceProvider(
        SourceIdentity(name, "1", "1", source_class),
        family or name,
        DATASET,
        frozenset(ClaimType),
        normalized_retention=RetentionPermission.ALLOWED,
    )


def item(
    provider: EvidenceProvider,
    known_ns: int,
    value: TimeValue = TIME,
    *,
    prior: EvidenceBatch | None = None,
    status: ClaimStatus = ClaimStatus.ASSERTED,
    with_event: bool = True,
) -> EvidenceBatch:
    number = 1 if prior is None else prior.events[0].revision.number + 1
    payload_hash = sha256(
        canonical_json_bytes({"scheduled_for": value, "status": status, "version": number})
    ).hexdigest()
    raw = RawSourceObservation(
        provider.source,
        "earnings-calendar",
        str(number),
        payload_hash,
        "application/json",
        known_ns,
        known_ns,
    )
    lineage = AcquisitionLineage(
        raw.observation_id,
        known_ns,
        known_ns,
        provider.parser_version,
        fingerprint("eie:demo-settings:v1", "UTC"),
        checkpoint=f"page-{number}",
    )
    reference = field_reference(
        raw.observation_id,
        raw.source_item_id,
        payload_hash,
        provider.parser_version,
        provider.source.adapter_version,
        EvidenceLocator(LocatorKind.JSON_POINTER, "/scheduled_for"),
        value,
    )
    event_identity = EventIdentity(
        EVENT_TYPE,
        EventScope(ScopeKind.COMPANY, (ISSUER,)),
        provider.source.name + ":" + PERIOD.key,
    )
    event = CanonicalEvent(
        event_identity,
        EventRevision(
            number,
            RevisionKind.ORIGINAL if number == 1 else RevisionKind.CORRECTION,
            known_ns,
            None if number == 1 else number - 1,
        ),
        LifecycleState.SCHEDULED,
        ConfirmationState.REPORTED,
        (raw.observation_id,),
        frozenset({provider.source.source_class}),
        "Structured earnings schedule",
        scheduled_for_ns=value.timestamp_ns,
    )
    descriptor = EventDescriptor(
        event.event_id,
        number,
        EVENT_TYPE,
        provider.source,
        provider.dataset.dataset_id,
        "calendar",
        "earnings",
        raw.observation_id,
        known_ns,
        (ClaimSubject(SubjectKind.ENTITY, ISSUER),),
        number,
        None if prior is None else prior.descriptors[0].descriptor_id,
        PERIOD,
        value,
        corporate_identity=CORPORATE,
    )
    return EvidenceBatch(
        "calendar",
        raw,
        lineage,
        (reference,),
        (
            ClaimInput(
                "time",
                CLAIM,
                value,
                (reference.evidence_id,),
                status,
                event_id=event.event_id if with_event else None,
            ),
        ),
        (event,) if with_event else (),
        (descriptor,) if with_event else (),
    )


def demonstrate(path: Path) -> dict[str, object]:
    a = source("OFFICIAL", SourceClass.OFFICIAL_PRIMARY)
    b, c = source("FEED_B"), source("FEED_C")
    first = item(a, 100)
    correction = item(
        a,
        400,
        replace(TIME, timestamp_ns=int(TIME.timestamp_ns or 0) + 60_000_000_000),
        prior=first,
        status=ClaimStatus.CORRECTED,
    )
    with SQLiteEventStore(path) as store:
        repo = EvidenceRepository(store)
        engine = EvidenceEngine(repo)
        queries = EvidenceQueries(repo, DATASET.dataset_id)
        engine.ingest(a, first, source_payload=item_payload(first))
        second = item(b, 200)
        engine.ingest(b, second, source_payload=item_payload(second))
        before = dumps(queries.resolution(CLAIM.claim_id, 200))
        assert queries.resolution(CLAIM.claim_id, 200).summary.independent_source_count == 2
        third = item(
            c, 300, replace(TIME, timestamp_ns=int(TIME.timestamp_ns or 0) + 120_000_000_000)
        )
        engine.ingest(c, third, source_payload=item_payload(third))
        assert queries.resolution(CLAIM.claim_id, 300).conflicts
        engine.ingest(a, correction, source_payload=item_payload(correction))
        after = dumps(queries.resolution(CLAIM.claim_id, 400))
        assert queries.resolution(CLAIM.claim_id, 400).value == correction.claims[0].value
        assert dumps(queries.resolution(CLAIM.claim_id, 200)) == before
        resolution_ids, run_id = engine.freeze(queries, 400)
        event_bytes = dumps(queries.event_resolution(400))
        assert len(queries.event_resolution(400).groups) == 1
        assert len(queries.event_resolution(400).groups[0].member_event_ids) == 3
        validate_store(repo)
    with SQLiteEventStore(path) as store:
        repo = EvidenceRepository(store)
        queries = EvidenceQueries(repo, DATASET.dataset_id)
        assert dumps(queries.resolution(CLAIM.claim_id, 200)) == before
        assert dumps(queries.resolution(CLAIM.claim_id, 400)) == after
        assert dumps(queries.event_resolution(400)) == event_bytes
        assert dumps(repo.stored_event_run(run_id)) == event_bytes
        assert dumps(repo.stored_resolution(resolution_ids[0])) == after
        return {
            "claim_fingerprint": queries.resolution(CLAIM.claim_id, 400).result_fingerprint,
            "event_fingerprint": queries.event_resolution(400).result_fingerprint,
            "independent_sources_before_conflict": 2,
            "source_events_retained": 3,
            "history_retained": len(queries.claim_history(CLAIM.claim_id, 400)),
            "restart_byte_identical": True,
        }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(canonical_json_bytes(demonstrate(Path(directory) / "evidence.sqlite")).decode())
