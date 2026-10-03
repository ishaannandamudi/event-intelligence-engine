from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.interactions.engine import InteractionEngine
from eie.interactions.models import DAY, InteractionPolicy, InteractionWindow, WindowOverride
from eie.interactions.queries import InteractionQueries
from eie.interactions.schema import CollisionType as C
from eie.interactions.schema import OrderingBasis
from eie.interactions.windows import horizon_end
from eie.studies.market import MarketSession
from eie.studies.models import SessionPolicy
from examples.interaction_fixtures import DATASET, MINUTE, query, retained_event
from tests.test_interaction_collisions import repo as repo


@pytest.mark.parametrize(
    "field",
    [
        "max_pairs",
        "max_sequence_events",
        "max_paths",
        "max_chronology_entries",
        "max_chains",
        "max_hops",
        "max_output_bytes",
        "max_run_members",
        "horizon_ns",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, 100_000_000_000_000_000])
def test_policy_limits_are_typed_and_hard_capped(field, value):
    with pytest.raises(ValidationError):
        replace(InteractionPolicy(), **{field: value})


@pytest.mark.parametrize(
    "basis", [OrderingBasis.UNKNOWN, OrderingBasis.COARSE_SESSION, OrderingBasis.SCHEDULED_TIME]
)
def test_propagation_requires_precise_declared_clock(basis):
    with pytest.raises(ValidationError):
        InteractionPolicy(chronology_basis=basis)


def test_large_time_window_overrides_and_session_calendars_are_bounded():
    with pytest.raises(ValidationError):
        InteractionWindow(pre_ns=31 * DAY)
    with pytest.raises(ValidationError):
        WindowOverride("EARNINGS", 0, 31 * DAY)
    with pytest.raises(ValidationError):
        InteractionWindow(session_policy=SessionPolicy.TRADING)
    with pytest.raises(ValidationError):
        InteractionWindow(overrides=(WindowOverride("EARNINGS", 0, 0),) * 2)
    with pytest.raises(ValidationError):
        InteractionPolicy(horizon_sessions=0)
    with pytest.raises(ValidationError):
        InteractionPolicy(horizon_sessions=1)


@pytest.mark.parametrize("which", ["pairs", "sequence", "bytes", "run"])
def test_runtime_limits_fail_without_partial_snapshot(repo, which):
    a = retained_event(repo, "a", MINUTE, time=MINUTE)
    retained_event(repo, "b", MINUTE, time=MINUTE)
    retained_event(repo, "c", MINUTE, time=MINUTE)
    p = replace(
        InteractionPolicy(),
        **{
            "pairs": {"max_pairs": 1},
            "sequence": {"max_sequence_events": 1},
            "bytes": {"max_output_bytes": 4096},
            "run": {"max_run_members": 1},
        }[which],
    )
    q = query(repo, a, MINUTE, p)
    with pytest.raises(ValidationError):
        if which == "run":
            InteractionEngine(repo).run((q, q))
        else:
            InteractionEngine(repo).interaction_context(q)
    assert not repo.store._rows("SELECT id FROM interaction_contexts")


def test_supplied_trading_sessions_control_elapsed_windows_and_horizon(repo):
    sessions = (
        MarketSession("one", 0, 10 * MINUTE, 0, "synthetic", "1"),
        MarketSession("two", 20 * MINUTE, 30 * MINUTE, 0, "synthetic", "1"),
    )
    a = retained_event(repo, "a", 9 * MINUTE, time=9 * MINUTE)
    retained_event(repo, "b", 21 * MINUTE, time=21 * MINUTE)
    p = InteractionPolicy(
        window=InteractionWindow(
            pre_ns=MINUTE,
            post_ns=2 * MINUTE,
            session_policy=SessionPolicy.TRADING,
            sessions=sessions,
        ),
        horizon_sessions=2,
    )
    q = query(repo, a, 22 * MINUTE, p)
    pairs = InteractionQueries(repo).collisions(q)
    assert len(pairs) == 1 and C.OVERLAPPING_WINDOW in pairs[0].categories
    row = repo.latest(DATASET, a.event_id, 9 * MINUTE)
    assert horizon_end(row, p) == 30 * MINUTE
    with pytest.raises(ValidationError, match="calendar"):
        horizon_end(row, replace(p, horizon_sessions=3))
    api = InteractionQueries(repo)
    with pytest.raises(ValidationError):
        api.sequence(DATASET, "iss_fixture", MINUTE, 0, MINUTE, q.policy_id)
    with pytest.raises(ValidationError):
        api.sequence(DATASET, "iss_fixture", 0, MINUTE, True, q.policy_id)


def test_mixed_explicit_macro_scheduled_and_issuer_occurrence_clock(repo):
    from eie.events.scope import ScopeKind
    from eie.events.taxonomy import EventFamily

    a = retained_event(repo, "a", 10 * MINUTE, time=10 * MINUTE)
    retained_event(
        repo,
        "cpi",
        2 * MINUTE,
        scheduled=15 * MINUTE,
        scope=ScopeKind.MACRO_GLOBAL,
        family=EventFamily.MACRO,
        code="MACRO.CPI.US",
    )
    p = InteractionPolicy(
        window=InteractionWindow(
            overrides=(
                WindowOverride("MACRO", 10 * MINUTE, 10 * MINUTE, OrderingBasis.SCHEDULED_TIME),
            )
        )
    )
    pairs = InteractionQueries(repo).collisions(query(repo, a, 10 * MINUTE, p))
    assert len(pairs) == 1 and C.MACRO_ISSUER in pairs[0].categories
