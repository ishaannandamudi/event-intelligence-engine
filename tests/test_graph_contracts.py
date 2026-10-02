"""Typed schema, stable identities, interval/exposure boundaries and hostile envelopes."""

from dataclasses import replace

import pytest

from eie.core.errors import PersistenceError, ValidationError
from eie.graph import GRAPH_SCHEMA_VERSION
from eie.graph.claims import (
    ClaimRevisionKind,
    Endpoint,
    IdentifierKind,
    RelationshipItem,
    ValidSemantics,
    edge_identity,
)
from eie.graph.exposure import (
    ExposureDomain,
    ExposureMeasurement,
    ExposureType,
    ExposureUnit,
    HoldingQuantity,
    compose_ownership,
    exposure_from_percent,
)
from eie.graph.models import GraphPolicy
from eie.graph.nodes import GraphDataset, NodeIdentifier, node_id
from eie.graph.repository import typed
from eie.graph.taxonomy import GraphNodeType, RelationshipType, canonical_endpoints
from eie.persistence import codec
from eie.providers.relationships.adapter import RelationshipAdapter
from eie.providers.relationships.models import RelationshipBatch, SnapshotPolicy, SnapshotScope
from examples.entity_relationship_graph import endpoint, fixture_nodes, relationship
from tests.graph_helpers import provider, traversal


@pytest.mark.parametrize(
    "field,value",
    [
        ("point", float("nan")),
        ("point", float("inf")),
        ("point", True),
        ("point", 1.01),
        ("point", -0.01),
        ("unit", ExposureUnit.PERCENT),
        ("domain", ExposureDomain.SIGNED_PORTFOLIO),
        ("reference_ns", -1),
        ("point", None),
        ("lower", 0.1),
    ],
)
def test_exposure_contract_refuses_malformed(field, value):
    base = ExposureMeasurement(ExposureType.OWNERSHIP_PERCENT, 10, "audited", point=0.5)
    with pytest.raises(ValidationError):
        replace(base, **{field: value})


def test_exposure_ranges_units_short_holdings_and_unknown():
    exact = exposure_from_percent(ExposureType.OWNERSHIP_PERCENT, 80, 10, "audited")
    assert exact.point == 0.8 and exact.unit is ExposureUnit.FRACTION
    with pytest.raises(ValidationError):
        exposure_from_percent(ExposureType.OWNERSHIP_PERCENT, True, 10, "audited")
    bounded = ExposureMeasurement(
        ExposureType.OWNERSHIP_PERCENT, 10, "audited", lower=0.1, lower_inclusive=False
    )
    derived = compose_ownership((exact, bounded), ("first", "second"))
    assert (
        derived is not None
        and derived.point is None
        and derived.lower == pytest.approx(0.08)
        and derived.upper == 0.8
        and not derived.lower_inclusive
    )
    assert compose_ownership((None, exact), ("first", "second")) is None
    with pytest.raises(ValidationError):
        compose_ownership((exact,), ())
    with pytest.raises(ValidationError):
        compose_ownership(
            (exact, replace(exact, measurement_type=ExposureType.REVENUE_SHARE)), ("a", "b")
        )
    with pytest.raises(ValidationError):
        compose_ownership((replace(exact, point=1e-250),) * 2, ("a", "b"))
    with pytest.raises(ValidationError):
        ExposureMeasurement(ExposureType.OWNERSHIP_PERCENT, 10, "audited", lower=0.4, upper=0.3)
    with pytest.raises(ValidationError):
        ExposureMeasurement(
            ExposureType.OWNERSHIP_PERCENT,
            10,
            "audited",
            lower=0.4,
            upper=0.4,
            upper_inclusive=False,
        )
    short = ExposureMeasurement(
        ExposureType.PORTFOLIO_WEIGHT,
        10,
        "audited",
        point=-0.12,
        domain=ExposureDomain.SIGNED_PORTFOLIO,
    )
    assert short.point == -0.12
    assert HoldingQuantity(10, shares=123, market_value=456, currency="USD").shares == 123


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"shares": -1},
        {"market_value": 1},
        {"shares": 1, "currency": "USD"},
        {"market_value": 1, "currency": "usd"},
    ],
)
def test_holding_malformed(kwargs):
    with pytest.raises(ValidationError):
        HoldingQuantity(10, **kwargs)


@pytest.mark.parametrize("authority", ["TICKER", "SYMBOL", "NAME", "", " bad"])
def test_stable_node_identifiers(authority):
    with pytest.raises(ValidationError):
        NodeIdentifier(authority, "ABC")


def test_node_foundation_schema_and_classification_namespace():
    a, _, _, _, security, _, etf, index, _, _ = fixture_nodes()
    assert GRAPH_SCHEMA_VERSION == "1.0"
    assert a.node_id == a.foundation.issuer_id
    assert security.node_id == security.foundation.security_id
    assert node_id(GraphNodeType.INDEX, "INDEX_PROVIDER", "42") != node_id(
        GraphNodeType.INDEX, "OTHER_PROVIDER", "42"
    )
    assert node_id(GraphNodeType.SECTOR, "TAXONOMY_1", "10") != node_id(
        GraphNodeType.SECTOR, "TAXONOMY_2", "10"
    )
    assert codec.loads(codec.dumps((a, security, etf, index))) == (a, security, etf, index)
    for value in (replace(a, label="new name", known_from_ns=1), index):
        assert value.revision_id
    for change in (
        {"node_id": index.node_id},
        {"foundation": None},
        {"identifiers": ()},
        {"known_from_ns": True},
    ):
        with pytest.raises(ValidationError):
            replace(a, **change)
    with pytest.raises(ValidationError):
        node_id(GraphNodeType.ISSUER, "SEC_CIK", "1")
    with pytest.raises(ValidationError):
        replace(security, node_id=index.node_id)
    with pytest.raises(ValidationError):
        replace(index, foundation=a.foundation)
    with pytest.raises(ValidationError):
        GraphDataset("dataset", "bad", "1")


@pytest.mark.parametrize(
    "kind,inverse",
    [
        (RelationshipType.PARENT_OF, RelationshipType.SUBSIDIARY_OF),
        (RelationshipType.SUPPLIER_TO, RelationshipType.CUSTOMER_OF),
        (RelationshipType.OWNS_EQUITY_IN, RelationshipType.OWNED_BY),
        (RelationshipType.SUCCESSOR_OF, RelationshipType.PREDECESSOR_OF),
        (RelationshipType.SECURITY_HAS_LISTING, RelationshipType.LISTING_REPRESENTS_SECURITY),
    ],
)
def test_inverse_and_symmetric_identity(kind, inverse):
    assert edge_identity(kind, "a", "b", ()) == edge_identity(inverse, "b", "a", ())
    assert canonical_endpoints(inverse, "b", "a") == (kind, "a", "b")
    assert edge_identity(RelationshipType.PEER_OF, "b", "a", ()) == edge_identity(
        RelationshipType.PEER_OF, "a", "b", ()
    )
    assert edge_identity(kind, "a", "b", ()) != edge_identity(
        kind, "a", "b", (("product", "chips"),)
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"valid_from_ns": None},
        {"valid_to_ns": 10},
        {"valid_to_ns": 9},
        {"valid_semantics": ValidSemantics.SNAPSHOT, "valid_to_ns": 20},
        {"revision_kind": ClaimRevisionKind.TERMINATION},
        {"qualifiers": (("z", "a"), ("a", "b"))},
        {"qualifiers": (("a", ""),)},
        {"state": "ASSERTED"},
        {"holding": HoldingQuantity(10, shares=1)},
    ],
)
def test_relationship_contract_boundaries(kwargs):
    a, b, *_ = fixture_nodes()
    with pytest.raises(ValidationError):
        replace(relationship("test", RelationshipType.SUPPLIER_TO, a, b), **kwargs)


def test_unknown_time_and_endpoint_contracts():
    a, b, *_ = fixture_nodes()
    row = replace(
        relationship("unknown", RelationshipType.PEER_OF, a, b),
        valid_from_ns=None,
        valid_semantics=ValidSemantics.UNKNOWN,
    )
    assert row.valid_from_ns is None
    with pytest.raises(ValidationError):
        replace(row, valid_from_ns=10)
    for args in (
        (IdentifierKind.SYMBOL, "ABC", GraphNodeType.ISSUER),
        (IdentifierKind.SYMBOL, "ABC", GraphNodeType.SECURITY, None, "XNAS"),
        (IdentifierKind.CIK, "1", GraphNodeType.ISSUER),
        (IdentifierKind.PROVIDER_ID, "42", GraphNodeType.ISSUER),
        (IdentifierKind.NODE_ID, a.node_id, GraphNodeType.ISSUER, None, "XNAS"),
    ):
        with pytest.raises(ValidationError):
            Endpoint(*args)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_depth": -1},
        {"max_depth": 9},
        {"max_nodes": 0},
        {"max_edges": 20001},
        {"max_paths": 0},
        {"adjacency_limit": 100001},
        {"relationship_types": ()},
        {"max_depth": True},
    ],
)
def test_traversal_policy_limits(kwargs):
    with pytest.raises(ValidationError):
        replace(traversal(), **kwargs)


def test_provider_bounds_untrusted_codec_and_capabilities():
    a, b, *_ = fixture_nodes()
    row = relationship("s", RelationshipType.SUPPLIER_TO, a, b)
    batch = RelationshipBatch("supply", "item", "1", 20, 20, (row,))
    adapter = RelationshipAdapter(provider())
    assert adapter.parse(codec.dumps(batch)) == batch
    for payload in (
        b"{}",
        b"bad json",
        b'{"type":"os.system","fields":{}}',
        b'{"type":"x","type":"y","fields":{}}',
        codec.dumps(GraphPolicy()),
        b"x" * 20_000_001,
        "not bytes",
    ):
        with pytest.raises(ValidationError):
            adapter.parse(payload)
    with pytest.raises(PersistenceError):
        typed(b"{}", RelationshipItem)
    with pytest.raises(PersistenceError):
        typed("not bytes", RelationshipItem)
    with pytest.raises(PersistenceError):
        typed(codec.dumps(GraphPolicy()), RelationshipItem)
    with pytest.raises(ValidationError):
        RelationshipAdapter(
            replace(provider(), capabilities=frozenset({RelationshipType.PEER_OF}))
        ).parse(codec.dumps(batch))
    with pytest.raises(ValidationError):
        replace(batch, items=(row, row))
    with pytest.raises(ValidationError):
        replace(batch, items=(row,) * 10001)
    with pytest.raises(ValidationError):
        replace(batch, source_observed_at_ns=21)
    with pytest.raises(ValidationError):
        replace(batch, published_at_ns=21)
    with pytest.raises(ValidationError):
        SnapshotScope(endpoint(a), RelationshipType.SUPPLIER_TO, 10)
    with pytest.raises(ValidationError):
        SnapshotScope(endpoint(fixture_nodes()[6]), RelationshipType.ETF_HOLDS, 10, False, True)
    with pytest.raises(ValidationError):
        replace(provider(), capabilities=frozenset())
    assert SnapshotPolicy.POINT_ONLY.value
