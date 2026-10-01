# Event Intelligence Engine

EIE is a standalone, provider-neutral foundation for descriptive event intelligence. Its first priority is answering what was knowable at an exact time. It does not produce trade instructions.

**Status:** EIE-1 scheduled-event engine, version `0.2.0`. EIE-0 raw observations, canonical event revisions, entity identity, source contracts, in-memory store, and causal replay remain the foundation. Scheduled-event history, causal queries, source coverage, and configurable event windows are implemented. Live providers and the final intelligence snapshot are deferred.

## Install and check

Python 3.11 or newer is required.

```sh
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
```

## Deterministic example

```python
from dataclasses import replace

from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import MACRO_CPI_US
from eie.core.quality import ConfirmationState
from eie.persistence.memory import InMemoryEventStore
from eie.replay.protocols import StoreReplayReader
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

source = SourceIdentity("example.gov", "1", "1", SourceClass.OFFICIAL_PRIMARY)
raw = RawSourceObservation(
    source, "cpi-2026-08", "1", "a" * 64, "application/json", 140, 140, published_at_ns=140
)
store = InMemoryEventStore()
store.append_observation(raw)
identity = EventIdentity(MACRO_CPI_US, EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), "cpi-2026-08")
original = CanonicalEvent(
    identity,
    EventRevision(1, RevisionKind.ORIGINAL, 140),
    LifecycleState.RELEASED,
    ConfirmationState.CONFIRMED_PRIMARY,
    (raw.observation_id,),
    frozenset({SourceClass.OFFICIAL_PRIMARY}),
    "CPI release",
    published_at_ns=140,
)
store.append_event(original)
corrected = replace(
    original,
    revision=EventRevision(2, RevisionKind.CORRECTION, 150, supersedes_revision_number=1),
    lifecycle=LifecycleState.UPDATED,
)
store.append_event(corrected)
reader = StoreReplayReader(store)
assert reader.events_known_at(139).events == ()
assert reader.events_known_at(149).events == (original,)
assert reader.events_known_at(150).events == (corrected,)
```

The tiny timestamps are synthetic UTC nanoseconds. Production callers should use `datetime_to_ns` on an aware datetime or supply validated UTC nanoseconds.

## Offline scheduled-event example

```python
from dataclasses import replace

from eie.config.settings import Settings
from eie.core.quality import ConfirmationState
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import MACRO_CPI_US
from eie.scheduled.engine import ScheduledEventEngine
from eie.scheduled.index import ScheduledEventIndex
from eie.scheduled.models import ScheduleClaim, ScheduleRevision
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

index = ScheduledEventIndex(Settings())
engine = ScheduledEventEngine(index)
source = SourceIdentity("example.gov", "1", "1", SourceClass.OFFICIAL_PRIMARY)
raw1 = RawSourceObservation(source, "cpi-original", "1", "a" * 64, "application/json", 10, 10)
raw2 = RawSourceObservation(source, "cpi-moved", "1", "b" * 64, "application/json", 20, 20)
index.append_observation(raw1)
index.append_observation(raw2)
identity = EventIdentity(MACRO_CPI_US, EventScope(ScopeKind.MACRO_COUNTRY, ("US",)), "cpi-period")
original = CanonicalEvent(
    identity,
    EventRevision(1, RevisionKind.ORIGINAL, 10),
    LifecycleState.SCHEDULED,
    ConfirmationState.CONFIRMED_PRIMARY,
    (raw1.observation_id,),
    frozenset({SourceClass.OFFICIAL_PRIMARY}),
    "CPI release",
    scheduled_for_ns=100,
)
index.append(
    ScheduleRevision(
        original,
        (ScheduleClaim(raw1.observation_id, SourceClass.OFFICIAL_PRIMARY, 100, 10),),
        raw1.observation_id,
    )
)
before = engine.upcoming(15, horizon_ns=200).events[0].scheduled_for_ns
moved = replace(
    original,
    revision=EventRevision(2, RevisionKind.RESCHEDULE, 20, 1),
    lifecycle=LifecycleState.RESCHEDULED,
    evidence_observation_ids=(raw2.observation_id,),
    scheduled_for_ns=120,
)
index.append(
    ScheduleRevision(
        moved,
        (ScheduleClaim(raw2.observation_id, SourceClass.OFFICIAL_PRIMARY, 120, 20),),
        raw2.observation_id,
    )
)
assert before == 100
assert engine.upcoming(15, horizon_ns=200).events[0].scheduled_for_ns == 100
assert engine.upcoming(25, horizon_ns=200).events[0].scheduled_for_ns == 120
```

The schedule time and the time at which it became knowable are separate. See [scheduled events](docs/scheduled-events.md) for query, window, coverage, and conflict contracts.

## Scope

EIE-1 implements deterministic offline scheduled-event behavior. It has no live ingestion, news classification, event studies, relationship graph, trading signal, broker, HTTP service, or integration with MCE or AME. The [master roadmap](docs/EIE_MASTER_SPEC.md) defines later phases.

See [architecture](docs/architecture.md), [event contracts](docs/event-contract.md), [source policy](docs/source-policy.md), and [replay and causality](docs/replay-and-causality.md).
