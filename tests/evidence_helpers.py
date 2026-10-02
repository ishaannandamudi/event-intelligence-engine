"""Small deterministic structured fixtures with independently supplied sources."""

from dataclasses import replace

from eie.evidence.claims import Claim, ClaimSubject, OccurrenceValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.queries import EvidenceQueries
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimType, Predicate, SubjectKind
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import ClaimInput, EvidenceBatch, EvidenceProvider
from examples.evidence_demo import CLAIM, DATASET, ISSUER, PERIOD, TIME, item, source

__all__ = ["CLAIM", "DATASET", "ISSUER", "PERIOD", "TIME", "item", "source", "setup", "bare"]


def setup(store: SQLiteEventStore) -> tuple[EvidenceRepository, EvidenceEngine, EvidenceQueries]:
    repo = EvidenceRepository(store)
    return repo, EvidenceEngine(repo), EvidenceQueries(repo, DATASET.dataset_id)


def bare(provider: EvidenceProvider, known: int = 100) -> EvidenceBatch:
    batch = item(provider, known, with_event=False)
    claim = Claim(
        ClaimType.EVENT_OCCURRENCE, ClaimSubject(SubjectKind.ENTITY, ISSUER), Predicate.OCCURRED
    )
    return replace(
        batch,
        claims=(
            ClaimInput(
                "occurrence", claim, OccurrenceValue(True), (batch.references[0].evidence_id,)
            ),
        ),
    )
