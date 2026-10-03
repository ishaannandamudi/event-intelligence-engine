"""Trusted retained fixtures and component inspection for EIE-13 tests."""

from eie.evidence.engine import EvidenceEngine
from eie.evidence.repository import EvidenceRepository
from eie.intelligence.collection import collect
from eie.intelligence.engine import IntelligenceEngine
from eie.intelligence.models import IntelligenceRequest
from eie.intelligence.policy import IntelligenceLimits, IntelligencePolicy
from eie.intelligence.repository import IntelligenceRepository
from examples.intelligence_fixtures import DATASET

DEFAULT_POLICY = IntelligencePolicy()


def ingest(store, provider, batch):
    return EvidenceEngine(EvidenceRepository(store)).ingest(provider, batch)


def request(repo, batch, known=None, policy=DEFAULT_POLICY, **kwargs):
    repo.append_policy(policy)
    event = batch.events[0]
    known = event.revision.available_from_ns if known is None else known
    return IntelligenceRequest(
        event.event_id,
        event.revision.number,
        DATASET.dataset_id,
        known,
        known,
        policy.policy_id,
        **kwargs,
    )


def compute(store, batch, known=None, policy=DEFAULT_POLICY, **kwargs):
    repo = IntelligenceRepository(store)
    return IntelligenceEngine(repo).compute(request(repo, batch, known, policy, **kwargs))


def inputs(store, batch, known=None, policy=DEFAULT_POLICY, **kwargs):
    repo = IntelligenceRepository(store)
    req = request(repo, batch, known, policy, **kwargs)
    with store.transaction():
        return collect(store, req, policy, IntelligenceLimits())


def novelty(value, dimension):
    return tuple(c for c in value.novelty if c.dimension.value == dimension)


def metrics(component):
    return {m.name: m.value for m in component.metrics}


def uncertainty(value, kind):
    return next(c for c in value.uncertainty if c.kind.value == kind)
