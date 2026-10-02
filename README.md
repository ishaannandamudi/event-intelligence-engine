# Event Intelligence Engine

EIE is a standalone, provider-neutral foundation for descriptive event intelligence. Its first priority is answering what was knowable at an exact time. It does not produce trade instructions.

**Status:** EIE-8 descriptive event studies and influence/decay, version `0.9.0`.
Supplied historical market data supports explicit anchors/windows, raw responses,
contamination flags and prior-only descriptors against frozen EIE-7 builds.
Event-study schema is `1.0`; SQLite store schema is `1.2`. Existing quantitative
schemas remain `1.0`. Zero runtime dependencies; no market fetcher or trading logic.
See [event studies](docs/event-studies.md), [alignment](docs/market-data-alignment.md),
[influence/decay](docs/event-influence-decay.md) and [completion](docs/EIE-8-completion.md).


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

## Offline event studies and deterministic restart

```sh
PYTHONPATH=src:. python examples/event_studies.py
```

The [runnable example](examples/event_studies.py) builds a synthetic EIE-7 earnings
history, supplies minute bars and session metadata, chooses publication anchors,
computes pre/post returns, volatility and prior-normalized volume, flags a co-occurring
management event, and adds three earlier studies. It computes prior-only response
statistics and single/class sampled half-response times, then serializes/reloads
byte-identical results after a SQLite restart. Every input is offline and synthetic.

Profile 10,000 events, two window definitions and a sector series with:

```sh
PYTHONPATH=src:. python benchmarks/event_studies.py
```

EIE-8 measures observed responses; abnormal-return/counterfactual models remain EIE-9.

## Offline historical build, restart and archive replay

```sh
PYTHONPATH=src python examples/historical_rebuild.py
```

The [runnable example](examples/historical_rebuild.py) defines synthetic `[10,20)`
history, plans two partitions, runs one page, closes/reopens SQLite, resumes, archives
eligible bytes, creates and validates a frozen manifest, queries at 14/19 and rebuilds
from the archive with an identical content fingerprint. It uses no internet.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from examples.historical_rebuild import demo

with TemporaryDirectory() as directory:
    result = demo(Path(directory))
    assert result["partitions"] == 2
    assert result["events"] == 2
    assert result["rebuild_matches"] is True
```

Historical CLI controls include `backfill plan/run/resume/status` and
`dataset manifest/validate/events-as-of/rebuild`. See [operations](docs/operations.md)
for bounded offline inputs and explicit SEC live setup. Historical queries report
source reconstruction assumptions separately from actual EIE acquisition clocks.

## Offline durable ingestion and restart

```sh
PYTHONPATH=src python examples/runtime_replay.py
```

The [runnable example](examples/runtime_replay.py) opens SQLite, registers an
explicit synthetic fixture source, polls once, commits data and checkpoint,
stops/closes, reopens, queries through the existing replay API and verifies that
a duplicate poll preserves event bytes. No internet is required.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from examples.runtime_replay import demo

with TemporaryDirectory() as directory:
    assert demo(Path(directory) / "events.sqlite") == {
        "events": 1,
        "generation": 2,
        "duplicates": 1,
    }
```

The foreground CLI offers `ingest-once`, `run`, `provider-health`, `checkpoint`
and `validate-store`. Acquisition commands require explicit `--fixture` or
`--live`. SEC live mode also requires an operator-configured contact User-Agent.
See [operations](docs/operations.md) for commands and shutdown behavior.

## Offline expectations and surprise replay example

Run from the repository root with the package installed, or `PYTHONPATH=src`.
The [runnable example](examples/expectations_replay.py) creates known canonical
earnings events, binds their verified issuer/reporting periods, and uses five
synthetic earlier releases plus the checked-in provider A/B fixtures.

```sh
PYTHONPATH=src python examples/expectations_replay.py
```

```python
from examples.expectations_replay import build_example
from eie.core.serialization import canonical_json_bytes
from eie.expectations.models import EPS_DILUTED

service, event_id = build_example()
engine = service.engine
assert engine.consensus(event_id, EPS_DILUTED, 150, provider_id="SYNTH_A").value == 2.40
assert engine.consensus(event_id, EPS_DILUTED, 250, provider_id="SYNTH_A").value == 2.45
assert len(engine.consensus(event_id, EPS_DILUTED, 250).provider_claims) == 2
release = engine.surprise(event_id, EPS_DILUTED, 300, provider_id="SYNTH_A")
assert release.consensus.value == 2.45
assert round(release.raw_surprise, 2) == 0.05
assert release.normalization.observation_count == 5
assert round(release.normalization.scale, 4) == 2.9652
assert round(release.normalized_surprise, 6) == 0.016862

# At T4 the provider retrospectively changes consensus to 2.47.
assert engine.consensus(event_id, EPS_DILUTED, 400, provider_id="SYNTH_A").value == 2.47
later = engine.surprise(event_id, EPS_DILUTED, 400, provider_id="SYNTH_A")
assert later.consensus.value == release.consensus.value
assert later.normalization.result_fingerprint == release.normalization.result_fingerprint
assert canonical_json_bytes(
    engine.surprise(event_id, EPS_DILUTED, 300, provider_id="SYNTH_A")
) == canonical_json_bytes(release)
```

The example verifies earlier query bytes before and after future ingestion.
The cutoff is strictly before original actual publication; history excludes the
current and same-time releases. EPS/revenue and macro components remain separate.

## Offline earnings conflict and replay example

Run this from the repository root. All three inputs are synthetic, checked-in extracts; no network connection is used.

```python
from pathlib import Path

from eie.config.settings import Settings
from eie.core.serialization import canonical_json_bytes
from eie.corporate.models import CorporateKind
from eie.corporate.settings import CorporateSettings
from eie.entities.sec import SecEntityRegistry, SecIssuerRevision, sec_issuer_id
from eie.providers.corporate.adapter import CorporateAdapter, CorporateIngestor
from eie.providers.sec.models import CIK
from eie.sources.trust import SourceClass

t0 = 1_788_000_000_000_000_000
t1 = 1_788_264_000_000_000_000
t2 = 1_788_350_400_000_000_000
t3 = 1_788_436_800_000_000_000
issuer = sec_issuer_id(CIK.parse(123456))
registry = SecEntityRegistry()
registry.add_issuer(SecIssuerRevision(issuer, CIK.parse(123456), "Alpha", (), t0, "obs_issuer"))
engine = CorporateIngestor(Settings(corporate=CorporateSettings(enabled=True)), registry)
fixtures = Path("tests/fixtures/corporate")


def ingest(name: str, provider: str, source_class: SourceClass, at_ns: int) -> None:
    source = CorporateAdapter(provider, source_class, frozenset(CorporateKind))
    result = engine.ingest_payload(source, (fixtures / f"{name}.json").read_bytes(), at_ns, at_ns)
    assert result.error_category is None


ingest("provider_a", "SYNTH_A", SourceClass.LICENSED_NEWS, t1)
first = engine.index.upcoming_earnings(t1).events[0]
assert first.corporate.selected_claim.payload.scheduled_date == "2026-10-20"
before = canonical_json_bytes(engine.index.upcoming_earnings(t1))

ingest("provider_b", "SYNTH_B", SourceClass.LICENSED_NEWS, t2)
conflict = engine.index.upcoming_earnings(t2).events[0].corporate
assert len(conflict.claims) == 2 and conflict.conflict_reason_codes
assert len({claim.observation_id for claim in conflict.claims}) == 2

ingest("company", "SYNTH_COMPANY", SourceClass.COMPANY_PRIMARY, t3)
revised = engine.index.upcoming_earnings(t3).events[0].corporate
assert revised.selected_claim.provider_id == "SYNTH_COMPANY"
assert revised.selected_claim.payload.scheduled_for_ns == 1_792_585_800_000_000_000
assert len(revised.claims) == 3
assert canonical_json_bytes(engine.index.upcoming_earnings(t1)) == before
assert len(engine.index.known_as_of(t3, kind=CorporateKind.EARNINGS_CALL).events) == 1
```

The two news calendars disagree on date and session. The company extract then supplies an exact release time and a separate call; earlier query bytes remain identical.

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

EIE-5 adds offline expectations, consensus, actual/previous/revised-previous values,
earnings and macro component surprises, guidance, and strictly prior historical
normalization alongside EIE-4 corporate events, EIE-3 SEC metadata, and EIE-2
official sources. Live collection, durable storage, filing NLP, event studies,
relationship graphs, trading, HTTP services, and MCE/AME integration remain deferred.
The [master roadmap](docs/EIE_MASTER_SPEC.md) defines later phases.

See [architecture](docs/architecture.md), [event contracts](docs/event-contract.md), [source policy](docs/source-policy.md), and [replay and causality](docs/replay-and-causality.md).
