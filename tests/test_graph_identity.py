"""Verified listing reuse, symbol changes, exact mapping and identity-edge safeguards."""

from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.entities.sec import (
    SecEntityRegistry,
    SecIssuerRevision,
    SecListingRevision,
    sec_listing_id,
)
from eie.events.scope import EventScope, ScopeKind
from eie.graph.claims import Endpoint, IdentifierKind
from eie.graph.engine import GraphIngestor
from eie.graph.nodes import NodeIdentifier, foundation_node
from eie.graph.propagation import propagate
from eie.graph.taxonomy import GraphNodeType
from eie.graph.taxonomy import RelationshipType as R
from eie.graph.validation import validate_propagation
from eie.providers.sec.models import CIK
from tests.graph_helpers import DATASET, SOURCE, event, ingest, relationship, traversal
from tests.graph_helpers import graph as graph


def listings(repo, nodes):
    a, b, _, _, sa, sb, *_ = nodes
    registry = SecEntityRegistry()
    for issuer in (a, b):
        original = issuer.foundation
        revision = SecIssuerRevision(
            issuer.node_id, CIK(original.cik), original.legal_name, (), 1, "obs_" + "0" * 64
        )
        registry.add_issuer(revision)
    registry.add_security(sa.foundation)
    registry.add_security(sb.foundation)
    old = SecListingRevision(
        sec_listing_id(sa.node_id, "REUSE", "XNAS"),
        a.node_id,
        sa.node_id,
        "REUSE",
        "XNAS",
        10,
        None,
        10,
        "obs_" + "0" * 64,
    )
    registry.add_listing(old)
    repo.append_node(foundation_node(old, SOURCE, DATASET, 10))
    return registry, old


def test_ticker_reuse_two_times_and_stable_historical_graph(graph):
    repo, nodes = graph
    a, b, c, _, sa, sb, *_ = nodes
    registry, old = listings(repo, nodes)
    engine = GraphIngestor(repo, registry=registry)
    symbol = Endpoint(IdentifierKind.SYMBOL, "REUSE", GraphNodeType.ISSUER, exchange="XNAS")
    assert engine.resolve(symbol, 20, 15) == a.node_id
    original = replace(relationship("symbol-old", R.SUPPLIER_TO, a, c, 15), source_endpoint=symbol)
    first = ingest(repo, (original,), 20)
    assert repo.claim_revision(first.claim_revision_ids[0]).source_node_id == a.node_id
    closed = replace(old, valid_to_ns=20, available_from_ns=30)
    registry.add_listing(closed)
    repo.append_node(foundation_node(closed, SOURCE, DATASET, 30))
    later = SecListingRevision(
        sec_listing_id(sb.node_id, "REUSE", "XNAS"),
        b.node_id,
        sb.node_id,
        "REUSE",
        "XNAS",
        20,
        None,
        30,
        "obs_" + "0" * 64,
    )
    registry.add_listing(later)
    repo.append_node(foundation_node(later, SOURCE, DATASET, 30))
    assert engine.resolve(symbol, 20, 25) == a.node_id
    assert engine.resolve(symbol, 30, 15) == a.node_id
    assert engine.resolve(symbol, 30, 25) == b.node_id
    second = ingest(repo, (replace(original, claim_key="symbol-new", valid_from_ns=25),), 40)
    assert repo.claim_revision(second.claim_revision_ids[0]).source_node_id == b.node_id
    assert repo.claim_revision(first.claim_revision_ids[0]).source_node_id == a.node_id
    scope_event = replace(
        event(a.node_id),
        identity=replace(event(a.node_id).identity, scope=EventScope(ScopeKind.SYMBOL, ("REUSE",))),
    )
    result = propagate(scope_event, repo, 30, 25, traversal(), registry=registry, exchange="XNAS")
    assert result.direct_entities == (b.node_id,) and result.bindings[0].listing.foundation == later
    assert validate_propagation(result, repo).valid
    with pytest.raises(ValidationError):
        propagate(scope_event, repo, 30, 25, traversal())
    corrupt = replace(result, bindings=(replace(result.bindings[0], node_id=a.node_id),))
    assert not validate_propagation(corrupt, repo).valid


def test_symbol_change_and_identity_inverse_links(graph):
    repo, nodes = graph
    a, b, _, _, sa, *_ = nodes
    registry, old = listings(repo, nodes)
    listing = repo.node(old.listing_id, 10)
    ingest(
        repo,
        (
            relationship("security-listing", R.SECURITY_HAS_LISTING, sa, listing),
            relationship("listing-security", R.LISTING_REPRESENTS_SECURITY, listing, sa),
        ),
        20,
    )
    assert len(repo.store._rows("SELECT * FROM graph_edges")) == 1
    closed = replace(old, valid_to_ns=30, available_from_ns=40)
    registry.add_listing(closed)
    repo.append_node(foundation_node(closed, SOURCE, DATASET, 40))
    new = replace(
        old,
        listing_id=sec_listing_id(sa.node_id, "RENAMED", "XNAS"),
        ticker="RENAMED",
        valid_from_ns=30,
        available_from_ns=40,
    )
    registry.add_listing(new)
    repo.append_node(foundation_node(new, SOURCE, DATASET, 40))
    assert repo.symbol_listings("REUSE", "XNAS", 20, 35)[0].foundation == old
    assert not repo.symbol_listings("REUSE", "XNAS", 40, 35)
    assert repo.symbol_listings("RENAMED", "XNAS", 40, 35)[0].foundation.security_id == sa.node_id
    with pytest.raises(ValidationError):
        ingest(repo, (relationship("false-issuer", R.ISSUER_ISSUES_SECURITY, b, sa),), 50)
    with pytest.raises(ValidationError):
        repo.append_node(
            replace(
                nodes[5],
                foundation=replace(nodes[5].foundation, issuer_id=a.node_id),
                known_from_ns=40,
            )
        )
    with pytest.raises(ValidationError):
        repo.symbol_listings("bad symbol", "XNAS", 40, 35)


def test_ambiguous_identity_mapping_never_resolves_arbitrarily(graph):
    repo, nodes = graph
    a, b, _, _, _, sb, *_ = nodes
    registry, _ = listings(repo, nodes)
    competing = SecListingRevision(
        sec_listing_id(sb.node_id, "REUSE", "XNAS"),
        b.node_id,
        sb.node_id,
        "REUSE",
        "XNAS",
        10,
        None,
        15,
        "obs_" + "0" * 64,
    )
    registry.add_listing(competing)
    repo.append_node(foundation_node(competing, SOURCE, DATASET, 15))
    symbol = Endpoint(IdentifierKind.SYMBOL, "REUSE", GraphNodeType.ISSUER, exchange="XNAS")
    assert GraphIngestor(repo, registry=registry).resolve(symbol, 20, 10) is None
    for node in (a, b):
        repo.append_node(
            replace(
                node,
                known_from_ns=20,
                identifiers=tuple(
                    sorted(
                        (*node.identifiers, NodeIdentifier("AUDITED", "42")),
                        key=lambda i: (i.authority, i.value),
                    )
                ),
            )
        )
    exact = Endpoint(IdentifierKind.PROVIDER_ID, "42", GraphNodeType.ISSUER, "AUDITED")
    assert GraphIngestor(repo).resolve(exact, 20, 10) is None
    assert GraphIngestor(repo).resolve(exact, 19, 10) is None


@pytest.mark.parametrize(
    "kind,source,target",
    [
        (R.PARENT_OF, 0, 4),
        (R.SUPPLIER_TO, 0, 7),
        (R.SECTOR_MEMBER_OF, 0, 9),
        (R.INDUSTRY_MEMBER_OF, 0, 8),
        (R.ETF_HOLDS, 7, 4),
        (R.INDEX_MEMBER_OF, 0, 6),
        (R.ISSUER_ISSUES_SECURITY, 1, 4),
    ],
)
def test_endpoint_type_mismatch_atomic(graph, kind, source, target):
    repo, nodes = graph
    with pytest.raises(ValidationError):
        ingest(repo, (relationship("wrong", kind, nodes[source], nodes[target]),), 20)
    assert not repo.store._rows("SELECT * FROM graph_claims") and not repo.store._rows(
        "SELECT * FROM observations"
    )
