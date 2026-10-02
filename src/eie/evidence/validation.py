"""Full bounded restart validation, including linked index and causal histories."""

from eie.core.errors import PersistenceError
from eie.core.time import require_ns
from eie.evidence.graph import validate_revision
from eie.evidence.repository import EvidenceRepository


def validate_store(
    repository: EvidenceRepository, *, max_records: int = 100000
) -> tuple[tuple[str, int], ...]:
    repo = repository
    methods = {
        "ce_datasets": repo.dataset,
        "ce_acquisitions": repo.lineage,
        "ce_claims": repo.claim,
        "ce_references": repo.reference,
        "ce_assertions": repo.assertion,
        "ce_relations": repo.relationship,
        "ce_descriptors": repo.descriptor,
        "ce_claim_resolutions": repo.stored_resolution,
        "ce_candidates": repo.candidate,
        "ce_event_runs": repo.stored_event_run,
        "ce_receipts": repo.receipt,
    }
    counts: list[tuple[str, int]] = []
    with repo.store.transaction():
        if repo.store._rows("PRAGMA foreign_key_check"):
            raise PersistenceError("evidence foreign reference integrity failure")
        for dataset, known in repo.store._rows("SELECT dataset,known FROM ce_seals"):
            if not isinstance(dataset, str) or not isinstance(known, int):
                raise PersistenceError("invalid evidence sealed cutoff")
            repo.dataset(dataset)
            require_ns(known)
        if repo.store._rows(
            "SELECT id FROM ce_acquisitions "
            "WHERE id NOT IN (SELECT lineage FROM ce_receipts) LIMIT 1"
        ):
            raise PersistenceError("incomplete evidence source-item checkpoint")
        for table, read in methods.items():
            ids = repo._ids(f"SELECT id FROM {table} ORDER BY id", (), max_records)
            for identity in ids:
                read(identity)
            counts.append((table, len(ids)))
        for identity in repo._ids(
            "SELECT id FROM ce_assertions ORDER BY thread,number", (), max_records
        ):
            row = repo.assertion(identity)
            validate_revision(
                row, None if row.previous_id is None else repo.assertion(row.previous_id)
            )
        for identity in repo._ids(
            "SELECT id FROM ce_descriptors ORDER BY thread,number", (), max_records
        ):
            descriptor = repo.descriptor(identity)
            if descriptor.previous_id is not None:
                previous = repo.descriptor(descriptor.previous_id)
                if (
                    descriptor.thread_id != previous.thread_id
                    or descriptor.number != previous.number + 1
                    or descriptor.known_from_ns <= previous.known_from_ns
                ):
                    raise PersistenceError("stored event revision chain mismatch")
        return tuple(sorted(counts))
