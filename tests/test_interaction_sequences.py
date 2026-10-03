from dataclasses import replace

import pytest

from eie.core.errors import ValidationError
from eie.events.taxonomy import EventFamily
from eie.interactions.models import EventSequence
from eie.interactions.queries import InteractionQueries
from eie.interactions.schema import DomainState as S
from eie.interactions.schema import OrderingBasis
from eie.persistence.codec import dumps
from examples.interaction_fixtures import DATASET, MINUTE, query, retained_event
from tests.test_interaction_collisions import repo as repo


def test_occurrence_and_information_orders_are_independent(repo):
    b = retained_event(repo, "b", 3 * MINUTE, time=2 * MINUTE)
    a = retained_event(repo, "a", 4 * MINUTE, time=MINUTE)
    q = query(repo, a, 5 * MINUTE)
    api = InteractionQueries(repo)
    occurrence = api.sequence(DATASET, "iss_fixture", 0, 5 * MINUTE, 5 * MINUTE, q.policy_id)
    knowledge = api.sequence(
        DATASET, "iss_fixture", 0, 5 * MINUTE, 5 * MINUTE, q.policy_id, OrderingBasis.KNOWLEDGE_TIME
    )
    assert occurrence.steps[0].knowledge_gap == -MINUTE
    assert knowledge.steps[0].event_gap == -MINUTE
    assert occurrence.steps[0].elapsed_ns == knowledge.steps[0].elapsed_ns == MINUTE
    assert occurrence.layers == tuple(reversed(knowledge.layers))
    assert b.event_id != a.event_id


def test_partial_coarse_order_and_stable_serialization(repo):
    a = retained_event(repo, "a", MINUTE, date="1970-01-01")
    retained_event(repo, "b", 2 * MINUTE, date="1970-01-01")
    q = query(repo, a, 3 * MINUTE)
    result = InteractionQueries(repo).sequence(
        DATASET, "iss_fixture", 0, 3 * MINUTE, 3 * MINUTE, q.policy_id
    )
    assert not result.layers and len(result.unordered_row_ids) == 2 and not result.steps
    assert "PARTIAL_ORDER" in result.quality
    assert dumps(repo.artifact(result.artifact_id, EventSequence)) == dumps(result)


@pytest.mark.parametrize(
    "family,code,states",
    [
        (
            EventFamily.CORPORATE_ACTION,
            "CORPORATE_ACTION.MERGER",
            (S.RUMORED, S.ANNOUNCED, S.APPROVED, S.CLOSED),
        ),
        (EventFamily.EARNINGS, "EARNINGS.GUIDANCE", (S.ISSUED, S.REVISED, S.WITHDRAWN)),
        (
            EventFamily.MANAGEMENT_GOVERNANCE,
            "MANAGEMENT.CEO_DEPARTURE",
            (S.DEPARTURE_ANNOUNCED, S.DEPARTURE_EFFECTIVE, S.REPLACEMENT),
        ),
    ],
)
def test_structured_domain_lifecycle(repo, family, code, states):
    first = None
    for i, state in enumerate(states, 1):
        current = retained_event(
            repo,
            str(i),
            i * MINUTE,
            time=i * MINUTE,
            family=family,
            code=code,
            thread="structured-thread",
            state=state.value,
        )
        first = first or current
    q = query(repo, first, 10 * MINUTE)
    values = InteractionQueries(repo).state_transitions(
        DATASET, "structured-thread", 10 * MINUTE, q.policy_id
    )
    assert len(values) == len(states) - 1
    assert all(v.relation.value == "STATE_TRANSITION_TO" for v in values)
    assert values[-1].to_state in states


@pytest.mark.parametrize("retracted", [False, True])
def test_correction_and_retraction_do_not_become_completion(repo, retracted):
    a = retained_event(
        repo,
        "deal",
        MINUTE,
        time=MINUTE,
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
        thread="deal",
        state="RUMORED",
    )
    retained_event(
        repo,
        "deal",
        2 * MINUTE,
        time=MINUTE,
        prior=a,
        correction=not retracted,
        retracted=retracted,
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
        thread="deal",
        state="RUMORED",
    )
    q = query(repo, a, 3 * MINUTE)
    values = InteractionQueries(repo).state_transitions(DATASET, "deal", 3 * MINUTE, q.policy_id)
    assert len(values) == 1
    assert values[0].relation.value == ("RETRACTION_OF" if retracted else "CORRECTION_OF")
    assert values[0].to_state is not S.CLOSED
    assert not InteractionQueries(repo).collisions(q)


def test_invalid_progression_and_mixed_families_fail(repo):
    a = retained_event(
        repo,
        "a",
        MINUTE,
        time=MINUTE,
        thread="t",
        state="ANNOUNCED",
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
    )
    retained_event(
        repo,
        "b",
        2 * MINUTE,
        time=2 * MINUTE,
        thread="t",
        state="CLOSED",
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
    )
    q = query(repo, a, 3 * MINUTE)
    with pytest.raises(ValidationError, match="progression"):
        InteractionQueries(repo).state_transitions(DATASET, "t", 3 * MINUTE, q.policy_id)


def test_tied_states_cannot_invent_progression(repo):
    a = retained_event(
        repo,
        "a",
        MINUTE,
        time=MINUTE,
        thread="t",
        state="ANNOUNCED",
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
    )
    retained_event(
        repo,
        "b",
        MINUTE,
        time=MINUTE,
        thread="t",
        state="APPROVED",
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
    )
    q = query(repo, a, MINUTE)
    assert not InteractionQueries(repo).state_transitions(DATASET, "t", MINUTE, q.policy_id)
    with pytest.raises(ValidationError):
        replace(q, known_as_of_ns=True)


def test_tied_prior_states_do_not_choose_stable_id_as_next_transition(repo):
    a = retained_event(
        repo,
        "a",
        MINUTE,
        time=MINUTE,
        thread="t",
        state="ANNOUNCED",
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
    )
    retained_event(
        repo,
        "b",
        MINUTE,
        time=MINUTE,
        thread="t",
        state="APPROVED",
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
    )
    retained_event(
        repo,
        "c",
        2 * MINUTE,
        time=2 * MINUTE,
        thread="t",
        state="CLOSED",
        family=EventFamily.CORPORATE_ACTION,
        code="CORPORATE_ACTION.MERGER",
    )
    q = query(repo, a, 3 * MINUTE)
    assert not InteractionQueries(repo).state_transitions(DATASET, "t", 3 * MINUTE, q.policy_id)
