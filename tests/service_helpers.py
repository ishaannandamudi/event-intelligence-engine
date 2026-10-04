"""Small native owners for final service boundary tests."""

from eie.entities.models import Issuer, Security, security_id
from eie.entities.sec import SecListingRevision, sec_listing_id
from eie.events.scope import EventScope, ScopeKind
from eie.graph.nodes import foundation_node
from eie.graph.repository import GraphRepository
from eie.interactions.repository import InteractionRepository
from eie.service.engine import EventIntelligenceService
from eie.service.models import SnapshotRequest
from eie.service.repository import ServiceRepository
from eie.service.schema import OwnerKind
from examples.entity_relationship_graph import DATASET, SOURCE
from examples.intelligence_fixtures import ISSUER
from examples.interaction_fixtures import retained_event

SID = security_id("TEST_AUTHORITY", "service-security")
CATALOG = "service-test"


def seed(store, *, ticker="OLD", known=1, valid_to=None, valid_from=0, issuer=ISSUER, sid=SID):
    graph = GraphRepository(store)
    repo = ServiceRepository(store)
    foundations = (
        Issuer(issuer, "Synthetic issuer"),
        Security(sid, issuer, "Common shares"),
        SecListingRevision(
            sec_listing_id(sid, ticker, "NYSE"),
            issuer,
            sid,
            ticker,
            "NYSE",
            valid_from,
            valid_to,
            known,
            "obs_service",
        ),
    )
    for f in foundations:
        node = foundation_node(f, SOURCE, DATASET, known)
        graph.append_node(node)
        repo.publish(CATALOG, OwnerKind.NODE, (node.revision_id,), known)
    event = retained_event(
        InteractionRepository(store), "service-release", known + 1, entity=issuer, scheduled=50
    )
    repo.publish(CATALOG, OwnerKind.EVENT, (event.event_id, "1"), known + 1)
    return EventIntelligenceService(repo), event


def request(known=20, kind=ScopeKind.COMPANY, identity=ISSUER, **kwargs):
    return SnapshotRequest(
        CATALOG,
        EventScope(kind, () if kind in (ScopeKind.MARKET, ScopeKind.MACRO_GLOBAL) else (identity,)),
        known,
        exchange="NYSE" if kind is ScopeKind.SYMBOL else None,
        **kwargs,
    )
