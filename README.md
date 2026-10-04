# Event Intelligence Engine

EIE is a standalone, provider-neutral foundation for descriptive event intelligence. Its first priority is answering what was knowable at an exact time. It does not produce trade instructions.

**Status:** EIE-0 through EIE-17 are complete; the planned EIE roadmap is finished.
Final snapshot, service, integration and hardening use engine `2.0.0`.
Source-backed claims preserve alternatives, categorical field resolution, independent
corroboration, conservative event aliases and historical correction/merge/split replay.
Relationship exposure and empirical impact remain separate.
Claim/evidence, relationship, impact and event-study schemas are `1.0`; final snapshot/API are `1.0`; SQLite is `2.1`.
All prior public schemas remain `1.0`. Zero runtime dependencies.
See [event studies](docs/event-studies.md), [alignment](docs/market-data-alignment.md),
[influence/decay](docs/event-influence-decay.md), [counterfactual impact](docs/counterfactual-impact.md)
and [EIE-11 completion](docs/EIE-11-completion.md). Graph contracts are documented in
[relationships](docs/entity-relationship-graph.md), [exposure](docs/exposure-graph.md),
[replay](docs/graph-replay.md) and [event propagation](docs/event-propagation.md).


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

## Offline counterfactual impact and deterministic restart

```sh
PYTHONPATH=src:. python examples/counterfactual_impact.py
```

The [runnable example](examples/counterfactual_impact.py) loads a validated raw study,
supplies exact prior target/market periods, fits alpha/beta on `[-250m,-20m]`, and
computes expected period returns, abnormal curves and independent CAR windows. It
prints normal OLS uncertainty and diagnostics, compares sector, supplied-factor and
matched-control models, reports include/exclude contamination variants, persists models,
fits, results, sensitivity and a run, and verifies a byte-identical reload. It preserves
model disagreement without choosing a winner. These are descriptive model-based
residuals, **not causal proof**. Information surprise and raw responses remain separate.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from examples.counterfactual_impact import demo

with TemporaryDirectory() as directory:
    result = demo(Path(directory))
    assert result["restart_byte_identical"]
    assert result["models"]["MARKET_MODEL"]["observations"] == 230
    assert result["contamination_variants"] == 2
```

Profile distinct daily-window events and a bounded persisted sample:

```sh
PYTHONPATH=src:. python benchmarks/counterfactual_impact.py --events 1000
```

The profile accepts up to 10,000 events and reports fitting, CAR, control matching,
validation, storage and indexed query timings. No timing threshold is enforced.
See [abnormal returns](docs/abnormal-returns.md) and [matched controls](docs/matched-controls.md).

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

EIE-0 through EIE-17 provide canonical/replay contracts, scheduled/official/SEC/corporate
events, numerical surprise, durable acquisition, historical builds, raw studies and
counterfactual estimates, sourced relationship graphs, propagation candidates,
structured claim/evidence resolution with conservative event deduplication and
verified unscheduled text extraction and prior-only novelty, materiality and separate
uncertainty components, historical analogues, separate reaction surprise and immutable
collision/sequence/propagation contexts, local two-time audits, separate latency and empirical source components. Final services and downstream integration
remain deferred. Trading and execution are outside the engine's purpose.
The [master roadmap](docs/EIE_MASTER_SPEC.md) defines later phases.

See [architecture](docs/architecture.md), [event contracts](docs/event-contract.md), [source policy](docs/source-policy.md), and [replay and causality](docs/replay-and-causality.md).

## Offline entity relationship and exposure example

```sh
PYTHONPATH=src python examples/entity_relationship_graph.py
```

The deterministic synthetic example ingests directional supplier/customer relations,
80% and 50% parent ownership, issuer/security links, classifications and explicitly
carried ETF/index composition snapshots. It queries a graph at T1, adds a supplier
termination correction at T2 and proves the T1 graph unchanged. It then traverses from
the issuer, derives a separate ownership path of `0.8 × 0.5 = 0.4`, generates related
entity candidates, persists them and verifies byte-identical historical restart.
The source event's direct company scope stays unchanged.

For an indexed repository and retained stable entity ID:

```python
from eie.graph.history import GraphHistoryQuery
from eie.graph.queries import GraphQueries

queries = GraphQueries(repository)
customers = queries.customers(issuer_id, known_as_of_ns=30, valid_at_ns=25)
revisions = queries.search_history(
    GraphHistoryQuery(40, node_id=issuer_id, known_interval=(20, 41), valid_interval=(10, 30))
)
```

Small timestamps illustrate logical clocks; production inputs use UTC nanoseconds.
The [offline profile](benchmarks/entity_relationship_graph.py) measures ingestion,
adjacency, depth-two traversal, paths, propagation, storage and verified restart:
`PYTHONPATH=src python benchmarks/entity_relationship_graph.py --nodes 2000`.

## Offline multi-source claim/evidence example

```sh
PYTHONPATH=src:. python examples/evidence_demo.py
```

The runnable example ingests provider A's earnings schedule into raw metadata,
precise hashed evidence and a semantic time claim. Provider B produces a duplicate
candidate and canonical alias group with two independent supporting origins. Provider C's
conflicting time remains retained. A later official correction resolves the time field,
preserving the old value, evidence, source event revisions and earlier grouping.
It replays before/after the correction, freezes the complete cutoff, persists/restarts
and asserts byte-identical claim and event resolution. Output includes fingerprints.

```python
from eie.evidence.engine import EvidenceEngine
from eie.evidence.queries import EvidenceQueries
from eie.evidence.repository import EvidenceRepository

repository = EvidenceRepository(sqlite_store)
engine = EvidenceEngine(repository)
receipt = engine.ingest(configured_provider, structured_batch)
queries = EvidenceQueries(repository, configured_provider.dataset.dataset_id)
field = queries.resolution(semantic_claim_id, known_as_of_ns=400)
group = queries.event_group(source_event_id, known_as_of_ns=400)
saved_claims, saved_event_run = engine.freeze(queries, known_as_of_ns=400)
```

The code above assumes the named typed inputs; the executable example supplies them.
See [claim/evidence](docs/claim-evidence-graph.md), [deduplication](docs/multi-source-deduplication.md),
[claim resolution](docs/claim-resolution.md) and [evidence replay](docs/evidence-replay.md).
Run the representative [profile](benchmarks/evidence_profile.py):
`PYTHONPATH=src:. python benchmarks/evidence_profile.py`.


## Offline source-backed text example

```sh
PYTHONPATH=src:. python examples/text_demo.py
```

The executable example performs these twelve steps:

1. Creates a synthetic press release with trusted source/retention configuration.
2. Normalizes UTF-8 and segments the document with original-offset mapping.
3. Extracts explicit CIK, numeric values, units, dates, period and person/role.
4. Uses a deterministic fake model to propose structured events.
5. Verifies exact spans, hashes, entities, units, context and time precision.
6. Rejects one hallucinated numeric proposal.
7. Accepts verified earnings, buyback and management claims.
8. Commits those claims through EIE-11 with original source evidence.
9. Ingests a second source document with a conflicting revenue value.
10. Asserts that EIE-11 preserves the disagreement.
11. Persists and restarts the store.
12. Asserts byte-identical historical extraction/audit results.

```python
from eie.nlp.engine import TextEngine
from eie.nlp.queries import TextQueries
from eie.nlp.repository import TextRepository

repository = TextRepository(sqlite_store)
run = TextEngine(repository).ingest(
    text_provider,
    text_document,
    source_bytes,
    entity_catalog,
    extraction_available_ns,
    model=optional_model,
)
queries = TextQueries(repository, text_document.dataset_id)
audit = queries.audit(run.run_id, known_as_of_ns=extraction_available_ns)
```

The named inputs are typed contracts; the executable example supplies them.
Source text is untrusted data and model output is a proposal. Failed extraction
never certifies event absence. Model provenance is distinct from provider provenance.
Extraction quality never upgrades source reliability or rumor/denial into confirmation.

See [text ingestion](docs/text-ingestion.md), [structured extraction](docs/structured-extraction.md),
[model assistance](docs/model-assisted-extraction.md), [security](docs/nlp-security.md),
[unscheduled events](docs/unscheduled-events.md) and [EIE-12 completion](docs/EIE-12-completion.md).
The [offline profile](benchmarks/text_extraction.py) measures 10,000 documents with
fake model assistance on a subset, 25 persisted documents, audit/restart and storage:
`PYTHONPATH=src:. python benchmarks/text_extraction.py`.
Text-extraction schema remains `1.0`; event-intelligence schema is `1.0`, SQLite store
is `1.7`, and all prior public schemas remain `1.0`. Runtime dependencies remain empty.

## Offline novelty, materiality and uncertainty

```sh
PYTHONPATH=src:. python examples/intelligence_demo.py
PYTHONPATH=src:. python benchmarks/event_intelligence.py --events 10000 --output /tmp/eie13-profile.json
```

The demonstration constructs three sourced prior financial quarters, extracts a
new multi-event document, and calculates all six novelty dimensions. Current
revenue of USD 5.2 billion has a USD 0.4 billion change from the latest comparable
quarter, prior median USD 4.7 billion and MAD USD 0.1 billion. EPS without a prior
cohort reports `NO_BASELINE`. Document/factual/entity/cross-source results retain
their separate states and evidence references.

Static earnings importance is HIGH. The explicit amount policy yields VERY_HIGH
event materiality while missing company scale and exposure stay unknown. All eight
uncertainty components retain their own state/reasons, including unresolved event
or entity context and analogue readiness.

An existing EIE-5 surprise result is attached separately. Missing analyst consensus
retains `CONSENSUS_MISSING`; novelty remains available. A second historical
build-bound example attaches a **real persisted EIE-9 impact result** as an
`EX_POST_AUDIT`, proves that the original ex-ante materiality and result bytes did
not change, restarts SQLite and verifies byte-identical intelligence and audit.
Historical source cutoff and actual local availability are shown separately.

This covers prior history, new-event processing, document/factual/numeric novelty,
static importance, materiality, uncertainty, separate surprise/impact references,
impact separation, persistence/restart and identical bytes. See
[novelty](docs/novelty-intelligence.md), [materiality](docs/materiality-intelligence.md),
[uncertainty](docs/uncertainty-decomposition.md), [replay](docs/intelligence-replay.md)
and [completion](docs/EIE-13-completion.md). Historical comparisons are added below in EIE-14.

## Historical analogues and market-reaction surprise (EIE-14)

```bash
PYTHONPATH=src:. python examples/historical_analogues.py
```

The offline example builds real historical event/intelligence/study artifacts,
selects strictly prior analogues with visible feature distances, freezes their
response distribution and later compares the exact target response. It displays
signed and magnitude percentiles and residual separately from an EIE-5 information
surprise object and a real EIE-9 counterfactual impact. Metadata-only source fixtures
have no numeric actual/consensus, so information surprise explicitly remains missing.
Persist/restart proves byte-identical analogue and reaction artifacts. Historical
source cutoff remains distinct from actual build/processing availability.

Engine `1.5.0`, historical-analogue schema `1.0`, SQLite store `1.8`; all previous
public schemas stay `1.0` and runtime dependencies remain empty. No response
forecast, collision model, service/API, sibling integration or trading logic is added.
See [historical analogues](docs/historical-analogues.md),
[distance](docs/analogue-distance.md), [reaction surprise](docs/market-reaction-surprise.md)
and [uncertainty](docs/analogue-uncertainty.md), with the
[A–AQ completion report](docs/EIE-14-completion.md).


## Event collision, interaction and propagation chronology (EIE-15)

```sh
PYTHONPATH=src:. python examples/event_interactions.py
PYTHONPATH=src:. python benchmarks/event_interactions.py --count 1000
```

The [offline example](examples/event_interactions.py) retains distinct same-source
earnings and buyback events, an explicitly timed overlapping CPI release, and a
historical supplier/customer edge. A later cutoff adds a subsequent customer event.
Real EIE-13 materiality results stay separate per event; there is no combined severity
or causal attribution. Both historical snapshots survive restart with identical bytes.

Immutable windows, collision categories, independent occurrence/knowledge sequences,
partial ordering, structured lifecycle/correction/retraction relations, graph path
references and finite propagation chains use event-interaction schema `1.0`.
Engine `1.6.0`; additive SQLite store `1.9`; prior public schemas remain unchanged.
Indexed time/entity/bundle/scope searches and explicit hard bounds prevent global
pairwise history comparison. EIE-11 owns aliases, EIE-10 owns graph history, and
EIE-8/9/13/14 remain independent. No new runtime dependency is introduced.

See [collisions](docs/event-collisions.md), [sequences](docs/event-sequences.md),
[propagation chronology](docs/propagation-chronology.md),
[contamination](docs/interaction-aware-contamination.md) and
[A–AQ completion report](docs/EIE-15-completion.md). EIE-16 reliability and latency
are implemented below; final services, sibling integration and trading remain deferred.


## Bitemporal intelligence, source reliability and latency (EIE-16)

```sh
PYTHONPATH=src:. python examples/source_reliability.py
PYTHONPATH=src:. python benchmarks/source_reliability.py --count 1000
```

The [offline example](examples/source_reliability.py) ingests independent A/B source
claims through EIE-11, retains A's explicit correction, measures separate clock gaps,
keeps both OFFICIAL_PRIMARY source classes unchanged, and preserves distinct coverage
states under a pinned expected universe. Earlier reliability/audit bytes remain unchanged
after correction and restart. There is no master reliability score or provider ranking.

Event type, predicate, scope, resource and rolling periods remain explicit. Later timing
instrumentation appends a diagnostic frame while sample identity and original local
availability remain stable. Expected revisions, retractions, corrections, consistency,
timeliness, coverage and independent-source evidence stay separate. Market impact,
content uncertainty, source precedence and canonical truth retain their owners.

Engine `1.7.0`, source-reliability schema `1.0`, SQLite `2.0`; prior schemas unchanged.
See [bitemporal intelligence](docs/bitemporal-intelligence.md),
[reliability](docs/source-reliability.md), [latency](docs/information-latency.md),
[coverage](docs/source-coverage.md) and [A–AM completion](docs/EIE-16-completion.md).
EIE-17 supplies the final service/integration boundary below. Trading execution is outside EIE.


## Final snapshot and service (EIE-17)

The final immutable snapshot requires an explicit cutoff and preserves typed event,
expectation/surprise, verified evidence/extraction, graph, intelligence, study/impact,
analogue/reaction, interaction and reliability/latency components. It uses bounded
native references and event summaries, with explicit state and completeness per
component. It does not flatten them into a score. Symbol requests bind to stable
historical issuer/security/listing identities. Provenance includes versions and
policy/build/market/reference fingerprints. Missing coverage is UNKNOWN.

```sh
PYTHONPATH=src:. python examples/final_snapshot.py
PYTHONPATH=src:. python benchmarks/final_snapshot.py --profile /tmp/eie-final.pstats
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite version
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite health
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite ready
```

The offline example spans all EIE layers using synthetic issuers, financial claims,
relationship data and market bars. Its official/SEC/corporate fixtures are synthetic
extracts. Expectations use the earlier demonstration's explicit synthetic clocks;
service publication retains their actual later registration clock. It verifies
native references and byte-identical restart. This is validation infrastructure,
not empirical information coverage or a market calibration dataset.

Typed consumers use `EventIntelligenceProvider.snapshot`, selected component methods,
bounded batches and membership-checked expansion. Optional local HTTP exposes
version, health/readiness, snapshots, components, metrics and expansion. No import
starts acquisition or a server. CLI commands also include snapshot/snapshots,
validate, backup, restore and explicit serve. See [API](docs/service-api.md),
[integration](docs/integration.md), [operations](docs/operations.md),
[compatibility](docs/compatibility.md), [security](docs/security.md),
[performance](docs/final-performance.md) and [final report](docs/EIE-17-completion.md).

Evaluation keeps operational correctness, information quality and market-impact
usefulness separate. EIE provides event intelligence infrastructure. It does not
prove a profitable strategy. A future AFRE can consume this contract; forecasting,
portfolio construction and trading execution are outside EIE.
