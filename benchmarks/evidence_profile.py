"""Representative offline normalized storage/profile; no CI timing threshold."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.core.fingerprints import fingerprint
from eie.core.serialization import canonical_json_bytes
from eie.evidence.claims import Claim, ClaimSubject, OccurrenceValue
from eie.evidence.dedup import resolve_events
from eie.evidence.engine import EvidenceEngine
from eie.evidence.queries import EvidenceQueries
from eie.evidence.references import AcquisitionLineage, EvidenceLocator, LocatorKind
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimStatus, ClaimType, Predicate, SubjectKind
from eie.evidence.validation import validate_store
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import ClaimInput, EvidenceBatch
from eie.providers.evidence.normalization import field_reference
from eie.sources.models import RawSourceObservation
from eie.sources.trust import SourceClass
from examples.evidence_demo import DATASET, item, source


def profile(root: Path, *, items: int = 100, claims_per_item: int = 20) -> dict[str, object]:
    path = root / "profile.sqlite"
    provider = source("PROFILE", SourceClass.OFFICIAL_PRIMARY)
    claim_ids = []
    t = perf_counter()
    with SQLiteEventStore(path) as store:
        repo = EvidenceRepository(store)
        engine = EvidenceEngine(repo)
        for n in range(items):
            content = {f"field-{i}": True for i in range(claims_per_item * 2)}
            payload = canonical_json_bytes(content)
            raw = RawSourceObservation(
                provider.source,
                f"item-{n}",
                "1",
                sha256(payload).hexdigest(),
                "application/json",
                100 + n,
                100 + n,
            )
            lineage = AcquisitionLineage(
                raw.observation_id,
                100 + n,
                100 + n,
                provider.parser_version,
                "a" * 64,
                checkpoint=f"page-{n}",
            )
            refs = tuple(
                sorted(
                    (
                        field_reference(
                            raw.observation_id,
                            raw.source_item_id,
                            raw.payload_hash,
                            provider.parser_version,
                            "1",
                            EvidenceLocator(LocatorKind.FIELD, key),
                            value,
                        )
                        for key, value in content.items()
                    ),
                    key=lambda r: r.evidence_id,
                )
            )
            claims = []
            for i in range(claims_per_item):
                subject = ClaimSubject(
                    SubjectKind.ENTITY, "iss_" + fingerprint("eie:profile-entity:v1", (n, i))
                )
                claim = Claim(ClaimType.EVENT_OCCURRENCE, subject, Predicate.OCCURRED)
                claim_ids.append(claim.claim_id)
                claims.append(
                    ClaimInput(
                        f"item-{n:04d}-claim-{i:04d}",
                        claim,
                        OccurrenceValue(True),
                        tuple(sorted((refs[2 * i].evidence_id, refs[2 * i + 1].evidence_id))),
                    )
                )
            batch = EvidenceBatch("structured", raw, lineage, refs, tuple(claims))
            if n == 0:
                initial = batch
            engine.ingest(provider, batch, source_payload=payload)
        secondary = source("PROFILE_B")
        for p, known, state, value in (
            (secondary, 100 + items, ClaimStatus.ASSERTED, False),
            (provider, 101 + items, ClaimStatus.CORRECTED, True),
        ):
            content = {r.locator.path: value for r in initial.references}
            payload = canonical_json_bytes(content)
            raw = replace(
                initial.raw,
                source=p.source,
                source_item_version="2",
                payload_hash=sha256(payload).hexdigest(),
                source_observed_at_ns=known,
                ingested_at_ns=known,
            )
            refs = tuple(
                field_reference(
                    raw.observation_id,
                    raw.source_item_id,
                    raw.payload_hash,
                    p.parser_version,
                    "1",
                    r.locator,
                    value,
                )
                for r in initial.references
            )
            mapping = {
                old.evidence_id: new.evidence_id
                for old, new in zip(initial.references, refs, strict=True)
            }
            revised_claims = tuple(
                replace(
                    c,
                    value=OccurrenceValue(value),
                    status=state,
                    evidence_ids=tuple(sorted(mapping[e] for e in c.evidence_ids)),
                )
                for c in initial.claims
            )
            lineage = AcquisitionLineage(
                raw.observation_id,
                known,
                known,
                p.parser_version,
                "a" * 64,
                checkpoint="revision-page",
            )
            engine.ingest(
                p,
                EvidenceBatch(
                    "structured",
                    raw,
                    lineage,
                    tuple(sorted(refs, key=lambda r: r.evidence_id)),
                    revised_claims,
                ),
                source_payload=payload,
            )
        ingestion = perf_counter() - t
        queries = EvidenceQueries(repo, DATASET.dataset_id)
        t = perf_counter()
        cutoff = 101 + items
        results = tuple(queries.resolution(i, cutoff) for i in claim_ids)
        resolution = perf_counter() - t
        t = perf_counter()
        for claim_id in claim_ids[:100]:
            queries.evidence_for_claim(claim_id, cutoff)
        lookups = perf_counter() - t
        t = perf_counter()
        replay = queries.claims_as_of(100 + items // 2)
        historical = perf_counter() - t
        t = perf_counter()
        base = item(source("EVENT_PROFILE"), 100).descriptors[0]
        descriptors = []
        for i in range(1000):
            entity = ClaimSubject(
                SubjectKind.ENTITY, "iss_" + fingerprint("eie:profile-event-entity:v1", i // 5)
            )
            descriptors.append(
                replace(
                    base,
                    source=source(f"P{i}").source,
                    event_id="evt_" + fingerprint("eie:profile-event:v1", i),
                    entities=(entity,),
                    corporate_identity=None,
                )
            )
        run, candidates = resolve_events(tuple(descriptors), DATASET.dataset_id, 100)
        blocking = perf_counter() - t
        expected = dumps(results[0])
        counts = {
            table: store._rows(f"SELECT COUNT(*) FROM {table}")[0][0]
            for table in (
                "observations",
                "ce_claims",
                "ce_references",
                "ce_assertions",
                "ce_assertion_evidence",
                "ce_receipts",
            )
        }
        store._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = path.stat().st_size
    t = perf_counter()
    with SQLiteEventStore(path) as store:
        repo = EvidenceRepository(store)
        checked = validate_store(repo)
        assert (
            dumps(EvidenceQueries(repo, DATASET.dataset_id).resolution(claim_ids[0], cutoff))
            == expected
        )
    restart = perf_counter() - t
    return {
        "workload": {
            "source_items": items + 2,
            "claims": len(claim_ids),
            "references": (items + 2) * claims_per_item * 2,
            "provider_assertions": (items + 2) * claims_per_item,
            "conflicting_propositions": claims_per_item,
            "corrections": claims_per_item,
            "event_descriptors": 1000,
        },
        "seconds": {
            "atomic_ingestion": ingestion,
            "claim_resolution": resolution,
            "100_evidence_lookups": lookups,
            "historical_replay": historical,
            "blocked_candidate_generation": blocking,
            "fully_checked_restart": restart,
        },
        "storage_bytes": size,
        "row_counts": counts,
        "candidate_count": len(candidates),
        "group_count": len(run.groups),
        "historical_claim_count": len(replay),
        "validated_counts": dict(checked),
        "restart_byte_identical": True,
    }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(json.dumps(profile(Path(directory)), sort_keys=True, indent=2))
