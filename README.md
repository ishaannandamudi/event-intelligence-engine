# Event Intelligence Engine

EIE is a standalone, provider-neutral foundation for descriptive event intelligence. Its first priority is answering what was knowable at an exact time. It does not produce trade instructions.

**Status:** EIE-3 SEC EDGAR filing metadata and issuer identity, version `0.4.0`. EIE-0 through EIE-2 contracts and Federal Reserve, BLS, and BEA adapters remain available. SEC submissions, historical metadata files, and ticker associations are parsed offline with causal filing, amendment, and identity queries. Providers are disabled by default; live collection and filing content analysis are deferred.

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

## Offline official-source example

Run this from the repository root. The fixture transcribes a small [official BLS calendar](https://www.bls.gov/schedule/2026/09_sched.htm) subset; the later reschedule is synthetic and demonstrates replay.

```python
import json
from datetime import datetime
from pathlib import Path

from eie.config.settings import Settings
from eie.core.time import datetime_to_ns
from eie.providers.bls.adapter import BLSAdapter
from eie.providers.common.ingestion import OfficialSourceIngestor
from eie.providers.common.settings import OfficialProviderSettings
from eie.scheduled.engine import ScheduledEventEngine
from eie.scheduled.index import ScheduledEventIndex


def ns(value: str) -> int:
    return datetime_to_ns(datetime.fromisoformat(value.replace("Z", "+00:00")))


index = ScheduledEventIndex(Settings(official=OfficialProviderSettings(enabled_ids=("BLS",))))
ingestor = OfficialSourceIngestor(index)
engine = ScheduledEventEngine(index)
fixture = json.loads(Path("tests/fixtures/official/bls_schedule.json").read_text())
fixture["items"] = [item for item in fixture["items"] if item["release"] == "CPI"]
t1, t2 = ns("2026-09-01T00:00:00Z"), ns("2026-09-02T00:00:00Z")
ingestor.ingest_payload(BLSAdapter(), json.dumps(fixture).encode(), t1, t1)
before = engine.upcoming(t1).events[0].scheduled_for_ns
fixture["items"][0].update(
    {"status": "RESCHEDULE", "item_version": "2", "scheduled_at": "2026-09-12T08:30:00-04:00"}
)
ingestor.ingest_payload(BLSAdapter(), json.dumps(fixture).encode(), t2, t2)
assert engine.upcoming(t1).events[0].scheduled_for_ns == before
assert engine.upcoming(t2).events[0].scheduled_for_ns != before
```

The raw observation, canonical revision, and schedule revision each retain their own identity and provenance. See [official sources](docs/official-sources.md) for source formats, health, time semantics, and limits.

## Offline SEC filing example

Run from the repository root. These EDGAR-shaped fixtures are synthetic; their names, CIKs, and filings are examples.

```python
from pathlib import Path

from eie.config.settings import Settings
from eie.providers.sec.adapter import SecIngestor
from eie.providers.sec.models import CIK
from eie.providers.sec.settings import SecSettings

fixture = Path("tests/fixtures/sec")
ingestor = SecIngestor(Settings(sec=SecSettings(enabled=True)))
before = 1_767_978_000_000_000_000
accepted = 1_768_071_792_000_000_000
ingestor.ingest_ticker_map((fixture / "ticker_map.json").read_bytes(), before, before)
assert ingestor.filings_for_cik(CIK.parse(123456), accepted - 1).filings == ()
ingestor.ingest_payload(
    (fixture / "submissions.json").read_bytes(),
    "https://data.sec.gov/submissions/CIK0000123456.json",
    accepted,
    accepted,
)
filing = ingestor.filings_for_cik(CIK.parse(123456), accepted).filings[0]
assert filing.metadata.form.raw == "10-Q"
assert filing.historical_symbols == (("OLD", "NYSE"),)
```

The accepted timestamp and EIE knowledge time are distinct when observation or ingestion is delayed. See [SEC EDGAR](docs/sec-edgar.md) and [entity identity](docs/entity-identity.md) for the exact rules.

## Scope

EIE-3 implements deterministic offline SEC filing metadata normalization alongside EIE-2 official sources. It has no bundled live collection, filing NLP, earnings, event studies, relationship graph, trading signal, broker, HTTP service, or integration with MCE or AME. The [master roadmap](docs/EIE_MASTER_SPEC.md) defines later phases.

See [architecture](docs/architecture.md), [event contracts](docs/event-contract.md), [source policy](docs/source-policy.md), and [replay and causality](docs/replay-and-causality.md).
