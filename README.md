# Event Intelligence Engine

EIE is a standalone, provider-neutral foundation for descriptive event intelligence. Its first priority is answering what was knowable at an exact time. It does not produce trade instructions.

**Status:** EIE-0 foundation, engine version `0.1.0`. Raw observations, canonical event revisions, entity identity, source contracts, an in-memory store, and causal replay are implemented. Live providers and the final intelligence snapshot are deferred.

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

## Scope

EIE-0 implements contracts and deterministic offline behavior. It has no live ingestion, news classification, event studies, relationship graph, trading signal, broker, HTTP service, or integration with MCE or AME. The [master roadmap](docs/EIE_MASTER_SPEC.md) defines later phases. EIE-1 is the scheduled-event engine, including upcoming-event queries, window states, and schedule-known-at replay.

See [architecture](docs/architecture.md), [event contracts](docs/event-contract.md), [source policy](docs/source-policy.md), and [replay and causality](docs/replay-and-causality.md).
