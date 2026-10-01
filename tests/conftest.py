"""Deterministic synthetic event fixtures."""

from dataclasses import replace

import pytest

from eie.core.quality import ConfirmationState
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import MACRO_CPI_US
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass


@pytest.fixture
def raw() -> RawSourceObservation:
    return RawSourceObservation(
        SourceIdentity("official.example", "1", "1", SourceClass.OFFICIAL_PRIMARY),
        "cpi-2026-08",
        "1",
        "a" * 64,
        "application/json",
        100,
        101,
        published_at_ns=100,
    )


@pytest.fixture
def event(raw: RawSourceObservation) -> CanonicalEvent:
    return CanonicalEvent(
        EventIdentity(MACRO_CPI_US, EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), "cpi-2026-08"),
        EventRevision(1, RevisionKind.ORIGINAL, 103),
        LifecycleState.SCHEDULED,
        ConfirmationState.CONFIRMED_PRIMARY,
        (raw.observation_id,),
        frozenset({SourceClass.OFFICIAL_PRIMARY}),
        "CPI release",
        scheduled_for_ns=200,
        published_at_ns=100,
        source_observed_at_ns=100,
        ingested_at_ns=101,
    )


def revise(
    event: CanonicalEvent,
    number: int,
    kind: RevisionKind,
    available: int,
    lifecycle: LifecycleState,
    **changes: object,
) -> CanonicalEvent:
    revision = EventRevision(number, kind, available, number - 1)
    return replace(event, revision=revision, lifecycle=lifecycle, **changes)
