from dataclasses import replace

import pytest

from eie.events.scope import ScopeKind
from eie.events.taxonomy import EventFamily
from eie.interactions.engine import InteractionEngine
from eie.interactions.models import InteractionPolicy, InteractionWindow, WindowOverride
from eie.interactions.queries import InteractionQueries
from eie.interactions.repository import InteractionRepository
from eie.interactions.schema import CollisionType as C
from eie.interactions.schema import ContaminationCategory as K
from eie.interactions.schema import OrderingBasis
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.interaction_fixtures import DATASET, MINUTE, query, retained_event


@pytest.fixture
def repo(tmp_path):
    with SQLiteEventStore(tmp_path / "interactions.sqlite") as store:
        yield InteractionRepository(store)


def test_three_distinct_bundle_events_remain_tied_and_nonadditive(repo):
    a = retained_event(repo, "earnings", 10 * MINUTE, time=10 * MINUTE, bundle="release")
    retained_event(
        repo,
        "buyback",
        10 * MINUTE,
        time=10 * MINUTE,
        bundle="release",
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.BUYBACK",
    )
    retained_event(
        repo,
        "ceo",
        10 * MINUTE,
        time=10 * MINUTE,
        bundle="release",
        family=EventFamily.MANAGEMENT_GOVERNANCE,
        code="MANAGEMENT.CEO_DEPARTURE",
    )
    q = query(repo, a, 10 * MINUTE)
    context = InteractionEngine(repo).interaction_context(q)
    assert len(context.collision_ids) == 2 and len(context.member_row_ids) == 3
    assert K.SAME_SOURCE_BUNDLE in context.contamination
    assert K.ORDERING_AMBIGUOUS in context.contamination
    seqs = [
        repo.artifact(
            i, __import__("eie.interactions.models", fromlist=["EventSequence"]).EventSequence
        )
        for i in context.sequence_ids
    ]
    assert all(len(s.layers) == 1 and len(s.layers[0]) == 3 and not s.steps for s in seqs)
    assert (
        "materiality" not in dumps(context).decode() and "severity" not in dumps(context).decode()
    )
    assert dumps(InteractionEngine(repo).interaction_context(q)) == dumps(context)


@pytest.mark.parametrize(
    "scope,kind", [(ScopeKind.COMPANY, C.SAME_ISSUER), (ScopeKind.SECURITY, C.SAME_SECURITY)]
)
def test_direct_identity_collisions(repo, scope, kind):
    a = retained_event(repo, "a", 10 * MINUTE, time=10 * MINUTE, scope=scope)
    retained_event(repo, "b", 11 * MINUTE, time=11 * MINUTE, scope=scope)
    pairs = InteractionQueries(repo).collisions(query(repo, a, 12 * MINUTE))
    assert len(pairs) == 1 and kind in pairs[0].categories
    assert C.SEQUENTIAL_RELATED in pairs[0].categories
    assert pairs[0].left_row_id < pairs[0].right_row_id


def test_macro_known_schedule_and_future_correction_do_not_change_saved_context(repo):
    a = retained_event(repo, "a", 10 * MINUTE, time=10 * MINUTE)
    macro = retained_event(
        repo,
        "cpi",
        2 * MINUTE,
        scheduled=15 * MINUTE,
        family=EventFamily.MACRO,
        code="MACRO.CPI.US",
        scope=ScopeKind.MACRO_GLOBAL,
    )
    policy = InteractionPolicy(window=InteractionWindow(basis=OrderingBasis.SCHEDULED_TIME))
    # Issuer event has an independently supplied exact schedule for this basis.
    issuer = retained_event(repo, "issuer-scheduled", 10 * MINUTE, scheduled=10 * MINUTE)
    q = query(repo, issuer, 10 * MINUTE, policy)
    frozen = InteractionEngine(repo).interaction_context(q)
    assert C.MACRO_ISSUER in InteractionQueries(repo).collisions(q)[0].categories
    retained_event(
        repo,
        "cpi",
        20 * MINUTE,
        scheduled=100 * MINUTE,
        prior=macro,
        family=EventFamily.MACRO,
        code="MACRO.CPI.US",
        scope=ScopeKind.MACRO_GLOBAL,
    )
    assert dumps(repo.context(frozen.artifact_id)) == dumps(frozen)
    assert not InteractionQueries(repo).collisions(query(repo, issuer, 21 * MINUTE, policy))
    assert a.event_id != issuer.event_id


@pytest.mark.parametrize("permit", [True, False])
def test_date_only_does_not_create_exact_gaps_or_order(repo, permit):
    a = retained_event(repo, "coarse", MINUTE, date="1970-01-01")
    retained_event(repo, "exact", 2 * MINUTE, time=2 * MINUTE)
    policy = InteractionPolicy(window=InteractionWindow(permit_coarse=permit))
    pairs = InteractionQueries(repo).collisions(query(repo, a, 3 * MINUTE, policy))
    assert bool(pairs) == permit
    if pairs:
        assert "PARTIAL_ORDER" in pairs[0].quality
        assert C.SAME_TIMESTAMP not in pairs[0].categories


@pytest.mark.parametrize("basis", list(OrderingBasis))
def test_each_window_basis_has_explicit_semantics(repo, basis):
    a = retained_event(repo, "a", MINUTE, time=MINUTE, scheduled=MINUTE, bundle="x")
    retained_event(repo, "b", MINUTE, time=MINUTE, scheduled=MINUTE, bundle="x")
    policy = InteractionPolicy(window=InteractionWindow(basis=basis))
    result = InteractionQueries(repo).collisions(query(repo, a, MINUTE, policy))
    assert len(result) == 1 and C.SAME_SOURCE_BUNDLE in result[0].categories
    assert (C.SAME_TIMESTAMP in result[0].categories) == (
        basis not in (OrderingBasis.UNKNOWN, OrderingBasis.COARSE_SESSION)
    )


def test_window_boundary_overrides_and_filter(repo):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    retained_event(repo, "b", 12 * MINUTE, time=12 * MINUTE)
    zero = InteractionPolicy(window=InteractionWindow(pre_ns=0, post_ns=0))
    assert not InteractionQueries(repo).collisions(query(repo, a, 13 * MINUTE, zero))
    p = replace(
        zero, window=replace(zero.window, overrides=(WindowOverride("EARNINGS", 0, 11 * MINUTE),))
    )
    q = query(repo, a, 13 * MINUTE, p, collision_types=(C.SAME_ISSUER,))
    assert len(InteractionQueries(repo).collisions(q)) == 1
    assert not InteractionQueries(repo).collisions(replace(q, collision_types=(C.GRAPH_RELATED,)))


def test_isolated_and_missing_time_do_not_mean_failed_source(repo):
    a = retained_event(repo, "unknown", MINUTE)
    ctx = InteractionQueries(repo).interaction_context(query(repo, a, MINUTE))
    assert ctx.contamination == (K.ISOLATED,)
    assert "UNKNOWN_TIME" in ctx.quality
    assert not ctx.chronology_ids


def test_market_wide_event_is_typed_not_inferred_from_prices(repo):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    retained_event(
        repo,
        "market",
        MINUTE,
        time=MINUTE,
        scope=ScopeKind.MARKET,
        family=EventFamily.MARKET_STRUCTURE,
        code="MARKET_STRUCTURE.DISRUPTION",
    )
    assert (
        C.MARKET_ISSUER in InteractionQueries(repo).collisions(query(repo, a, MINUTE))[0].categories
    )
    assert repo.store._rows("SELECT COUNT(*) FROM interaction_events") == [(2,)]


def test_sequence_query_filters_and_bounds(repo):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    retained_event(repo, "b", 2 * MINUTE, time=2 * MINUTE)
    q = query(repo, a, 3 * MINUTE)
    seq = InteractionQueries(repo).sequence(
        DATASET,
        "iss_fixture",
        0,
        3 * MINUTE,
        3 * MINUTE,
        q.policy_id,
        event_types=("EARNINGS.RELEASE",),
    )
    assert len(seq.steps) == 1
    assert seq.steps[0].elapsed_ns == MINUTE
    assert seq.steps[0].event_gap == seq.steps[0].knowledge_gap == MINUTE
