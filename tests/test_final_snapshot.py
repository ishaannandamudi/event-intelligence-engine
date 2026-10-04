"""Causal service selection and retained owner verification."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from service_helpers import CATALOG, SID, request, seed

from eie.core.errors import PersistenceError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.events.scope import ScopeKind
from eie.interactions.repository import InteractionRepository
from eie.persistence.codec import dumps, loads
from eie.persistence.sqlite import SQLiteEventStore
from eie.service.engine import EventIntelligenceService
from eie.service.errors import ServiceError
from eie.service.models import CoverageDeclaration, ProvenancePin, SnapshotLimits
from eie.service.repository import ServiceRepository
from eie.service.schema import CompletenessState as Q
from eie.service.schema import ComponentKind as C
from eie.service.schema import ComponentState as S
from eie.service.schema import OwnerKind, ReadinessState, ServiceCode, ValidationDepth
from eie.service.validation import validate
from examples.intelligence_fixtures import ISSUER
from examples.interaction_fixtures import retained_event


@pytest.fixture
def seeded(tmp_path):
    with SQLiteEventStore(tmp_path / "service.sqlite") as store:
        engine, event = seed(store)
        yield engine, event


@pytest.mark.parametrize(
    "kind,identity",
    [(ScopeKind.COMPANY, ISSUER), (ScopeKind.SECURITY, SID), (ScopeKind.SYMBOL, "OLD")],
)
def test_identity_and_replay(seeded, kind, identity):
    engine, event = seeded
    r = request(kind=kind, identity=identity)
    value = engine.snapshot(r, request_id="credential-like untrusted correlation")
    assert value.identity.issuer_id == ISSUER
    assert value.request.valid_at_ns == r.known_as_of_ns
    refs = next(c.references for c in value.components if c.kind is C.EVENTS)
    assert refs[0].event.revision_id == event.revision_id
    assert engine.expand(value.snapshot_id, refs[0].owner_id) == event
    assert loads(dumps(value)) == value
    assert canonical_json_bytes(engine.snapshot(r, request_id="another")) == canonical_json_bytes(
        value
    )
    assert validate(engine.repository, value.snapshot_id, ValidationDepth.DEEP).valid
    assert engine.upcoming(r)[0].revision_id == event.revision_id
    assert engine.events(r).references == refs


@pytest.mark.parametrize(
    "kind",
    [
        ScopeKind.MACRO_GLOBAL,
        ScopeKind.MARKET,
        ScopeKind.SECTOR,
        ScopeKind.INDUSTRY,
        ScopeKind.INDEX,
    ],
)
def test_partial_scopes(seeded, kind):
    engine, _ = seeded
    value = engine.snapshot(request(kind=kind, identity="scope"))
    assert value.identity.issuer_id is None
    assert next(c for c in value.components if c.kind is C.IDENTITY).state is S.NOT_APPLICABLE
    assert any(c.state is S.UNAVAILABLE for c in value.components)


def test_selection_and_coverage(seeded):
    engine, _ = seeded
    r = request(components=(C.EVENTS, C.IMPACT))
    pin = ProvenancePin("COVERAGE", "explicit-calendar", "a" * 64, 10)
    engine.repository.declare(CoverageDeclaration(CATALOG, C.IMPACT, r.scope, 10, Q.COMPLETE, pin))
    value = engine.snapshot(r)
    impact = value.components[1]
    assert impact.state is S.AVAILABLE and not impact.references
    assert impact.completeness is Q.COMPLETE
    assert engine.impact(r) == impact
    assert engine.analogues(r).state is S.UNAVAILABLE
    assert engine.relationships(r).state is S.UNAVAILABLE
    assert engine.reliability(r).state is S.UNAVAILABLE


def test_required_and_bounds(seeded):
    engine, _ = seeded
    for r, code in (
        (request(components=(C.IMPACT,), required=(C.IMPACT,)), ServiceCode.INCOMPLETE_DATA),
        (
            request(limits=replace(SnapshotLimits(), max_snapshot_bytes=1)),
            ServiceCode.BOUND_EXCEEDED,
        ),
        (request(limits=replace(SnapshotLimits(), max_batch=501)), ServiceCode.BOUND_EXCEEDED),
    ):
        with pytest.raises(ServiceError) as exc:
            engine.snapshot(r)
        assert exc.value.code is code
    with pytest.raises(ValidationError):
        request(components=())
    with pytest.raises(ServiceError):
        engine.expand("absent", "anything")
    value = engine.snapshot(request())
    with pytest.raises(ServiceError):
        engine.expand(value.snapshot_id, "../../secret")


def test_future_event_concurrent_and_seals(seeded):
    engine, event = seeded
    r = request()
    before = canonical_json_bytes(engine.snapshot(r))

    def future():
        changed = retained_event(
            InteractionRepository(engine.repository.store),
            "service-release",
            30,
            entity=ISSUER,
            prior=event,
            correction=True,
        )
        engine.repository.publish(CATALOG, OwnerKind.EVENT, (changed.event_id, "2"), 30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(future)
        b = pool.submit(engine.snapshot, r)
        a.result()
        assert canonical_json_bytes(b.result()) == before
    assert canonical_json_bytes(engine.snapshot(r)) == before
    assert engine.snapshot(request(40)).components != engine.snapshot(r).components
    with pytest.raises(PersistenceError):
        engine.repository.publish(CATALOG, OwnerKind.EVENT, (event.event_id, "1"), 10)
    assert engine.repository.publish(CATALOG, OwnerKind.EVENT, (event.event_id, "1"), 2)


def test_batch_and_readiness(seeded):
    engine, _ = seeded
    r = request()
    result = engine.snapshots((r, request(kind=ScopeKind.SYMBOL, identity="MISSING"), r))
    assert [i.position for i in result.items] == [0, 1, 2]
    assert result.items[0].snapshot == result.items[2].snapshot
    assert result.items[1].failure.code is ServiceCode.HISTORICAL_DATA_UNAVAILABLE
    assert engine.ready().state is ReadinessState.READY
    assert engine.ready(live_ingestion=False).state is ReadinessState.DEGRADED
    assert engine.ready(catalogs=("absent",)).state is ReadinessState.NOT_READY
    assert engine.health().alive


def test_restart(seeded, tmp_path):
    engine, _ = seeded
    r = request()
    original = canonical_json_bytes(engine.snapshot(r))
    path = tmp_path / "service.sqlite"
    with SQLiteEventStore(path) as other:
        assert (
            canonical_json_bytes(EventIntelligenceService(ServiceRepository(other)).snapshot(r))
            == original
        )


def test_listing_reuse_change_and_ambiguity(tmp_path):
    from eie.entities.models import issuer_id, security_id
    from eie.entities.sec import SecListingRevision, sec_listing_id
    from eie.graph.nodes import foundation_node
    from eie.graph.repository import GraphRepository
    from examples.entity_relationship_graph import DATASET, SOURCE

    with SQLiteEventStore(tmp_path / "listing.sqlite") as store:
        engine, _ = seed(store)
        old = engine.snapshot(request(kind=ScopeKind.SYMBOL, identity="OLD"))
        closed = SecListingRevision(
            sec_listing_id(SID, "OLD", "NYSE"), ISSUER, SID, "OLD", "NYSE", 0, 30, 40, "obs_closed"
        )
        node = foundation_node(closed, SOURCE, DATASET, 40)
        GraphRepository(store).append_node(node)
        engine.repository.publish(CATALOG, OwnerKind.NODE, (node.revision_id,), 40)
        new_issuer = issuer_id("TEST_AUTHORITY", "second")
        new_sid = security_id("TEST_AUTHORITY", "second")
        seed(store, known=41, valid_from=30, issuer=new_issuer, sid=new_sid)
        later = engine.snapshot(
            replace(request(45, kind=ScopeKind.SYMBOL, identity="OLD"), valid_at_ns=35)
        )
        assert later.identity.security_id == new_sid
        historical = engine.snapshot(
            replace(request(45, kind=ScopeKind.SYMBOL, identity="OLD"), valid_at_ns=20)
        )
        assert historical.identity.security_id == SID
        assert engine.snapshot(old.request) == old
        changed = SecListingRevision(
            sec_listing_id(SID, "NEW", "NYSE"),
            ISSUER,
            SID,
            "NEW",
            "NYSE",
            30,
            None,
            46,
            "obs_changed",
        )
        node = foundation_node(changed, SOURCE, DATASET, 46)
        GraphRepository(store).append_node(node)
        engine.repository.publish(CATALOG, OwnerKind.NODE, (node.revision_id,), 46)
        assert (
            engine.snapshot(request(50, kind=ScopeKind.SYMBOL, identity="NEW")).identity.security_id
            == SID
        )
        # A second current security shares the ticker/exchange. No silent choice.
        third = issuer_id("TEST_AUTHORITY", "third")
        third_sid = security_id("TEST_AUTHORITY", "third")
        seed(store, known=51, valid_from=30, issuer=third, sid=third_sid)
        with pytest.raises(ServiceError) as exc:
            engine.snapshot(request(60, kind=ScopeKind.SYMBOL, identity="OLD"))
        assert exc.value.code is ServiceCode.AMBIGUOUS_IDENTITY


def test_insertion_order(tmp_path):
    results = []
    for order in (False, True):
        with SQLiteEventStore(tmp_path / f"order-{order}.sqlite") as store:
            engine, event = seed(store)
            # Reuse the exact owners in a fresh catalog, with admissible
            # publication order independent of native dependency insertion.
            pubs = engine.repository.collect(request(), C.IDENTITY, (request().scope,))
            pubs += engine.repository.collect(request(), C.EVENTS, (request().scope,))
            for p in reversed(pubs) if order else pubs:
                engine.repository.publish(
                    "ordered", p.reference.kind, p.reference.lookup, p.reference.available_ns
                )
            results.append(
                canonical_json_bytes(engine.snapshot(replace(request(), catalog_id="ordered")))
            )
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "future"},
        {"engine_version": "future"},
        {"components": ()},
        {"versions": (("engine", "future"),)},
    ],
)
def test_snapshot_model_refuses_incompatible(seeded, change):
    engine, _ = seeded
    value = engine.snapshot(request())
    with pytest.raises(ValidationError):
        replace(value, **change)


def test_snapshot_clocks_and_contract_bounds(seeded):
    from eie.service.models import (
        BatchItem,
        SnapshotCompleteness,
    )

    engine, _ = seeded
    value = engine.snapshot(request())
    event = next(c for c in value.components if c.kind is C.EVENTS)
    ref = event.references[0]
    assert ref.event.clocks.normalized is None and ref.event.clocks.snapshot is None
    for changes in (
        {"lookup": ()},
        {"known_ns": 21},
        {"schema_version": "future"},
        {"provenance": (ProvenancePin("SOURCE", "future", "a" * 64, 30),)},
    ):
        with pytest.raises(ValidationError):
            replace(ref, **changes)
    for state in (S.UNAVAILABLE, S.NOT_APPLICABLE):
        with pytest.raises(ValidationError):
            replace(event, state=state)
    with pytest.raises(ValidationError):
        replace(event, publication_ids=())
    with pytest.raises(ValidationError):
        SnapshotCompleteness(((C.EVENTS, Q.UNKNOWN), (C.EVENTS, Q.COMPLETE)))
    with pytest.raises(ValidationError):
        replace(value, provenance=(ProvenancePin("SOURCE", "future", "a" * 64, 30),))
    with pytest.raises(ValidationError):
        BatchItem(0, request().fingerprint)
    with pytest.raises(ValidationError):
        replace(engine.repository.publication(event.publication_ids[0]), ordinal=-1)
    with pytest.raises(ValidationError):
        replace(engine.repository.publication(event.publication_ids[0]), scopes=())
    with pytest.raises(ValidationError):
        replace(
            engine.repository.publication(event.publication_ids[0]),
            scopes=(request().scope, request().scope),
        )
    with pytest.raises(ValidationError):
        CoverageDeclaration(
            CATALOG,
            C.EVENTS,
            request().scope,
            10,
            Q.COMPLETE,
            ProvenancePin("COVERAGE", "future", "a" * 64, 20),
        )


def test_limits_batch_rollback_and_required_gap(seeded):
    engine, _ = seeded
    with pytest.raises(ServiceError):
        engine.snapshot(None)
    with pytest.raises(ServiceError):
        engine.snapshots((request(),) * 501)
    engine.limits = replace(SnapshotLimits(), max_batch_bytes=10)
    before = engine.repository.store._rows("SELECT COUNT(*) FROM service_snapshots")
    with pytest.raises(ServiceError):
        engine.snapshots((request(),))
    assert engine.repository.store._rows("SELECT COUNT(*) FROM service_snapshots") == before
    engine.limits = SnapshotLimits()
    engine.repository.declare(
        CoverageDeclaration(
            CATALOG,
            C.EVENTS,
            request().scope,
            10,
            Q.KNOWN_GAPS,
            ProvenancePin("COVERAGE", "fixture", "a" * 64, 10),
        )
    )
    assert engine.snapshot(request(components=(C.EVENTS,))).components[0].state is S.INCOMPLETE
    with pytest.raises(ServiceError):
        engine.snapshot(request(components=(C.EVENTS,), required=(C.EVENTS,)))


@pytest.mark.parametrize("state", [S.CONFLICTED, S.STALE, S.INCOMPLETE])
def test_owner_qualifications_are_preserved(seeded, monkeypatch, state):
    from eie.service.assembly import assemble

    engine, _ = seeded
    collected = engine.repository.collect(request(), C.EVENTS, (request().scope,))
    monkeypatch.setattr(
        engine.repository,
        "collect",
        lambda r, k, scopes, **kwargs: (
            tuple(replace(p, state=state) for p in collected) if k is C.EVENTS else ()
        ),
    )
    # MARKET needs no identity lookup, so this isolates component assembly.
    value = assemble(engine.repository, request(kind=ScopeKind.MARKET, components=(C.EVENTS,)))
    assert value.components[0].state is state


def test_reference_bound_missing_identity_and_coarse(seeded):
    from eie.events.models import StructuredFact
    from eie.evidence.claims import TimePrecision
    from eie.service import owners

    engine, event = seeded
    with pytest.raises(ServiceError) as exc:
        engine.snapshot(
            request(components=(C.IDENTITY,), limits=replace(SnapshotLimits(), max_references=1))
        )
    assert exc.value.code is ServiceCode.BOUND_EXCEEDED
    with pytest.raises(ServiceError):
        engine.snapshot(request(identity="iss_" + "f" * 64))
    with pytest.raises(ServiceError):
        engine.snapshot(request(kind=ScopeKind.SECURITY, identity="sec_" + "f" * 64))
    summary = owners.event_summary(
        replace(
            event, scheduled_for_ns=None, facts=(StructuredFact("scheduled_date", "2026-10-20"),)
        )
    )
    assert (
        summary.scheduled_for_ns is None
        and summary.scheduled_time.precision is TimePrecision.DATE_ONLY
    )
    assert summary.scheduled_time.timestamp_ns is None


def test_bad_owner_projection_and_no_phantom_claims(seeded):
    from eie.service import owners

    engine, event = seeded
    with pytest.raises(ValidationError):
        owners.payload(engine.repository.store, OwnerKind.EVENT, (event.event_id,))
    with pytest.raises(ValidationError):
        owners.payload(engine.repository.store, OwnerKind.DOMAIN, ())
    with pytest.raises(PersistenceError):
        owners.read(engine.repository.store, OwnerKind.DOMAIN, ("unknown",))
    with pytest.raises(PersistenceError):
        owners.read(engine.repository.store, OwnerKind.EVENT, ("unknown", "1"))
    # Arbitrary retained domain types are not supported service owners.
    store = engine.repository.store
    from eie.entities.models import Issuer

    issuer = Issuer(ISSUER, "Synthetic issuer")
    with store.transaction():
        store._save_contract(issuer)
    from eie.core.fingerprints import fingerprint

    identity = fingerprint("eie:durable-contract:v1", ("eie.entities.models.Issuer", issuer))
    with pytest.raises(PersistenceError):
        engine.repository.publish(CATALOG, OwnerKind.DOMAIN, (identity,), 30)
    assert engine.snapshot(request()).identity.issuer_id == ISSUER
