# EIE-6 completion report

Completed on 2026-10-01 at engine version **0.7.0**. EIE-6 changes remain in the
working tree. Nothing was committed or pushed; HEAD remains
`b72be04` (`Add expectations consensus and surprise intelligence`). No sibling
repository was modified. EIE-7 work has not started.

The starting EIE-0 through EIE-5 implementation, tests, master specification,
prior completion report and clean working tree were inspected. Existing public
quantitative contracts are preserved. The original version assertion now expects
0.7.0; all other existing tests retain their behavior.

## A. Architecture

The acquisition path is transport → polling source → bounded queue → existing
adapter/raw evidence/normalization → atomic SQLite persistence and checkpoint →
existing queries and replay. Transport has no event semantics; the scheduler
does not parse payloads; persistence and queries perform no network calls.
Operational configuration and telemetry remain separate from domain settings.

## B. Runtime package structure

`runtime` contains immutable configuration, UTC/monotonic clocks, source
contracts, provider registration/factories, coordinator, diagnostics, signal
handling and CLI. `transport` contains HTTP policies/models, protocols, HTTP,
errors, retry and rate limiting. `ingestion` contains operational models,
adapter processors, queue, cursor policy and atomic batch writer. `persistence`
contains shared append validation, typed codec, session layouts, migrations and
SQLite alongside the original in-memory store.

## C. Persistence architecture

`SQLiteEventStore` implements the existing event-store protocol. It durably stores
raw observations, canonical revisions, immutable domain records, evidence links,
versioned domain-session caches and operational records. Schedule, identity,
filing, corporate, estimate, actual and guidance state survives restart.
Domain queries use restored existing engines; SQL contains no duplicate
quantitative calculations. Explicit allowlisted JSON reconstructs validated
contracts and declared engine layouts. No pickle or payload-selected imports
are used.

## D. SQLite schema and migrations

Initial store schema is **1.0**, with one ordered transactional migration.
Tables: `schema_migrations`, `observations`, `events`, `evidence`,
`domain_records`, `domain_evidence`, `sessions`, `checkpoints`, `operations`,
`quarantine` and `acquisitions`. Indexes cover source/provider item identity,
availability/event identity, issuer, accession, estimate event/measurement and
provider operations. Fresh/current stores open successfully. Unknown, future,
unversioned, partial and altered schemas fail closed; failed DDL rolls back.

## E. Transaction semantics

`BEGIN IMMEDIATE` serializes ingestion. A batch loads current namespace state,
stages the whole adapter batch, writes raw/domain/canonical records and the
restart cache, records permitted acquisition data, compares checkpoint generation,
and commits together. Nested store methods use savepoints. Exceptions before
commit roll back the complete batch. An instance `RLock`, transaction ownership
checks and bounded SQLite busy timeouts define writer concurrency. File stores
use WAL, FULL synchronization and enabled foreign keys.

## F. Store parity

The in-memory implementation remains available. Shared insertion validation
preserves identity collisions, revisions, lifecycle transitions, supersession,
rescheduling, causal availability and raw-evidence checks. Tests compare both
stores' canonical bytes, historical revisions and replay/filter results.
Restarted domain sessions preserve schedule, filing/amendment, corporate
conflict, consensus, actual and guidance query outputs. Operational checkpoints
are new SQLite/runtime contracts and have no old in-memory protocol counterpart.

## G. HTTP transport

Real standard-library GET transport provides status, allowlisted headers and
response bytes, separate bounded connect/read timeouts, body limits and explicit
errors. Default body limit is 2 MB; timeout defaults are 5/10 seconds. Requests
use identity encoding and do not execute/render content. ETag and Last-Modified
support conditional polling. A checkpoint-backed 304 is healthy no-new-data.

## H. URL and response safety

HTTPS and explicit host allowlists are the default. Userinfo, query strings,
fragments, unsafe schemes and control characters are refused. All DNS answers
must be public, with explicit loopback development exceptions; connections pin
the validated IP while TLS verifies the configured hostname. Resolver waits and
abandoned DNS threads are bounded. Standalone redirects require explicit policy,
are revalidated and capped at five. Rate-limited clients require redirects
disabled so extra redirect requests cannot bypass the request budget.
Content type, Content-Length and streamed body size are checked. Compressed
responses are rejected. Response metadata excludes cookies and arbitrary headers.

## I. Retry and backoff

Defaults are three attempts, exponential 0.5-second base delay and 30-second
maximum delay. Retry-After seconds/HTTP dates are safely parsed and capped.
Only transient connection/timeouts and 429/500/502/503/504 are eligible.
Policy, certificate, parser, schema, normalization and most 4xx failures are
ineligible. Injected-clock waits are cancellable; tests have no random jitter.

## J. Rate limiting

Each HTTP source owns a sliding-window request budget. Every attempt, including
retries, consumes capacity; 429 extends a monotonic cooldown. Diagnostics expose
capacity, throttling, next eligible time and observed 429 count. SEC defaults
to two requests per second and rejects configurations above its ten-request
local ceiling. BLS uses one request per minute. Rate windows are process-local;
operators must coordinate other applications and restart behavior.

## K. Provider polling

`PollableSource` receives the resource checkpoint and cancellation signal,
returning provider-neutral acquired bytes and an optional typed cursor.
`HttpPollingSource` uses a trusted fixed URL; `FixtureSource` is explicitly
offline. The coordinator schedules declared monotonic intervals in deterministic
registry order, limits outstanding acquisitions and avoids catch-up bursts.
Provider IDs are unique, registrations immutable, and runtime/providers disabled
by default. No streaming client or distributed scheduler is introduced.

## L. Ingestion queue and workers

Fetch and ingestion workers are fixed, configured thread pools. The acquisition
queue has bounded capacity. One provider has at most one outstanding batch.
Queue rejection records `BACKPRESSURE`, degrades health and leaves the checkpoint
unchanged; source overlap/resume must offer the item again. Shutdown stops
scheduling, cancels waits, finishes bounded fetches and drains acquired batches.
Transactions finish or roll back before ordinary store closure.

## M. Checkpoint and cursor model

Schema 1.0 checkpoints contain provider/resource, generation, observed/completed
UTC, typed cursor, last source item, ETag and Last-Modified. Cursor kinds cover
source item, accession, timestamp, sequence and opaque tokens. Cursor values
are bounded and hidden from repr. Numeric high-water marks do not regress under
out-of-order delivery; kind changes fail. Non-numeric ordering is source-owned.
SEC records its latest accepted accession. Resume tokens must not be credentials.

## N. Checkpoint atomicity

Checkpoint advancement requires the calling thread's ingestion transaction and
compares the expected generation. Data and the advanced checkpoint become visible
in the same commit. Tests inject write/checkpoint failures and verify that no
cursor advances over missing data. 304 advances operational generation while
retaining event state. Normalization settings are checked at startup and again
inside preparation, including writers started before another connection's first
commit; incompatible policies cannot mix in one namespace.

## O. Restart and crash recovery

Fault boundaries cover acquisition, parse, preparation/raw preparation,
persistence, checkpoint, commit and post-commit. Recovery tests verify complete
rollback before commit and preserved data/checkpoint after commit, followed by
idempotent overlap. Physical child-process tests call `os._exit` with an open
transaction or immediately after COMMIT, then reopen SQLite. A post-commit
exception cannot falsely quarantine committed data. Checkpoint state can restore
successful health when final operational telemetry was interrupted.

## P. Provider isolation

Transport, rate, unavailable, schema, parse, normalization, persistence,
checkpoint, configuration and backpressure failures remain distinct.
One source failure does not stop unrelated providers or claim healthy empty
coverage. Global persistence failure halts scheduling and fences later
uncommitted work. Two separate SQLite connections sharing an expectations
namespace merge provider estimates without losing updates.

## Q. Source timing and latency

Acquisition completion supplies local observation UTC; worker preparation
supplies ingestion UTC. Existing source publication/acceptance/vintage/effective
times retain their meanings. Backwards clock conflicts fail without checkpoint
advance. Monotonic telemetry measures fetch, queue, preparation, parse validation,
normalization, commit and total durations. The bridge measures an initial parse
pass and then invokes the unchanged adapter's defensive parse during staging;
normalization duration includes that second pass. Production UTC synchronization
is an operator assumption. Latency stays outside canonical fingerprints.

## R. Health model

States include unknown, healthy, degraded, stale, rate limited, schema mismatch,
unavailable and stopped. Health tracks attempt/success times, consecutive failures
and category. Success and 304 reset failure streaks. Embedded health derives
staleness from registration policy; the CLI inspects persisted health history.
Shutdown records stopped state. Health transitions and poll diagnostics persist
separately from canonical history.

## S. Quarantine

Normalization-ineligible payloads record provider/resource, SHA-256, adapter
version, category and failure metadata. The same quarantined hash/version is
not normalized every poll. Changed bytes or an explicitly new adapter version
permit another attempt; `clear_quarantine` provides deliberate manual retry.
Payload fetching can continue to detect change. Normalization suppression is
distinct from another normalization attempt; repeated suppression does not
increment the quarantine's normalization-attempt count.

## T. Raw retention policy

Default acquisition storage is `METADATA_ONLY` with permission `UNKNOWN`.
`BOUNDED_RAW` requires explicit `ALLOWED` permission and a byte limit covering
the configured response bound. `UNKNOWN`/`RESTRICTED` cannot enable raw bytes.
Hash, trusted source resource and safe metadata remain available for diagnosis.
This policy also covers quarantined acquisitions. No general archive, automatic
licensed-content retention or vendor-specific legal policy is implemented.

## U. Logging and metrics

Standard-library structured logs emit provider, operation, outcome, duration,
category and checkpoint generation. Payloads, request URLs, contact headers and
exception messages are omitted. Internal metrics track polls/failures,
fetched/ingested items, duplicates, quarantines, provider/normalization/persistence
failures, final rate-limited polls, queue depth and accumulated latency.
Per-attempt 429 observations are exposed by the limiter. Persisted poll history
restores aggregate metrics; interrupted post-commit telemetry can be absent.

## V. CLI and runtime controls

`eie` and `python -m eie` provide `ingest-once`, foreground `run`,
`provider-health`, `checkpoint` and `validate-store`. Acquisition requires an
explicit mutually exclusive `--fixture` or `--live`. Configuration/persistence
setup errors return 2; failed ingestion, invalid stores and forced/fatal runs
return 1; successful operations return 0. SIGINT/SIGTERM request bounded shutdown
and previous handlers are restored. No daemon, final REST service or deployment
is added.

## W. SEC live vertical slice and BLS

SEC current submissions for one explicit CIK use the existing metadata adapter,
required operator contact User-Agent, HTTPS, rate limits, retries and payload
bounds. Contact identity stays out of domain settings/persistence/fingerprints.
Historical filing references are not followed; documents/ticker maps are not
downloaded. BLS uses its existing native iCalendar parser and official calendar
endpoint. Fed/BEA retain audited-extract/offline boundaries. Integration tests
exercise HTTP and conditionals over fake connections. No live SEC/BLS requests
were made during implementation or verification.

## X. Late and out-of-order handling

Earlier source event times observed later append later knowledge. Replay before
observation excludes the item. Provider timestamps remain distinct from local
arrival order. Existing revision rules preserve history; numeric cursors retain
their high-water mark. Tests cover late arrival, out-of-order ingestion,
publication/observation separation and prior query bytes.

## Y. Replay compatibility

Acquired events use the existing raw/canonical representation and
`StoreReplayReader`. SQLite resolves visible revisions and reuses in-memory
applicability/range logic. Restored domain engines run their existing historical
APIs. Overlap/restart preserve old clocks, adapter versions and result bytes.
No automatic historical re-normalization occurs when code changes.

## Z. Configuration and secrets

Immutable operational `RuntimeConfig` and transport/provider policies are
separate from result-relevant `Settings`. Worker counts, timeouts, health and
generations do not change domain fingerprints. SEC contact comes from trusted
configuration or `EIE_SEC_USER_AGENT`, is hidden from policy repr and excluded
from stored domain state/logs. No commercial key is required, and no
Authorization/API-key header interface is introduced. Result-affecting settings
changes require an explicit migration/reprocessing plan; hot reload is absent.

## AA. Security

URL/DNS/TLS policy, bounded bytes/timeouts/retries/threads/queues, header
allowlists, non-executable typed JSON and fail-closed schema/layout validation
protect acquisition boundaries. Provider bytes cannot choose filesystem paths.
Fixture and SQLite paths are trusted operator configuration. Foreign keys and
typed/index consistency checks protect linked causal records. Database/WAL/SHM
permissions and safe backup handling are documented operator responsibilities.
Stored data does not instruct execution or silently enable networking.

## AB. Documentation

Created [live-ingestion.md](live-ingestion.md), [persistence.md](persistence.md),
[provider-runtime.md](provider-runtime.md) and [operations.md](operations.md).
Updated README, architecture, replay/causality, source policy, official sources,
SEC, corporate events, expectations/consensus, event contracts and master status.
[runtime_replay.py](../examples/runtime_replay.py) demonstrates SQLite creation,
synthetic polling, atomic checkpoint, shutdown, reopening, replay and harmless
duplicate overlap. All seven README Python examples pass.

## AC. Tests added

Added **206** deterministic cases across five modules:

| Module | Cases |
| --- | ---: |
| `test_sqlite_persistence.py` | 44 |
| `test_transport_runtime.py` | 81 |
| `test_ingestion_runtime.py` | 21 |
| `test_runtime_controls.py` | 43 |
| `test_runtime_recovery_edges.py` | 17 |

They cover parity, serialization/migrations/FKs, all domain-family persistence,
revision history, checkpoint rollback, physical and injected crashes,
concurrent connections, HTTP parser/fake sockets, unsafe URL/DNS/redirects,
timeouts/limits/compression/content types, retries/rates, scheduler/queues,
isolation/recovery, shutdown, causality, configuration races, redaction and CLI.
The original **502** cases continue passing.

## AD. Full pytest result

Final branch-enabled run: **708 passed in 2.34 seconds**.
Final socket-blocked run: **708 passed in 1.31 seconds**. The harness preloads
standard-library SSL, then blocks socket creation, connection and DNS before
test collection; controlled transport tests use fake connections/sockets.
Physical crash children perform SQLite-only work. Tests require no internet.

## AE. Coverage

Overall branch-enabled combined coverage: **96.42%** (7,342/7,529 statements,
2,394/2,568 branch opportunities). New EIE-6 source group: **96.80%**
(1,813/1,853 statements, 546/584 branch opportunities). The new group includes
runtime, transport, ingestion and the five added persistence modules.
Both combined measures exceed the 95% gate. Standalone branch coverage is
93.22% overall; combined coverage uses the repository's branch-enabled metric.

## AF. Ruff

Lint passes. Format check passes: **165 files already formatted**.

## AG. Mypy

Strict checking passes: **no issues in 119 source files**.

## AH. Diff checks and review

`git diff --check` passes. Tracked changes and all new implementation,
tests/examples/benchmark/docs were reviewed; new-file whitespace checks pass.
Git status contains the intended EIE-6 working-tree changes. The in-memory
validation extraction preserves the prior behavior. No commit or push occurred.

## AI. Credential and unfinished-work audit

The tracked/new text-file scan found no credential signatures, private-key
material or trailing whitespace. No unfinished-work markers occur in code.
Two existing documentation occurrences describe quality gates in the EIE-5
report and master specification; neither is an outstanding implementation item.
Contact/payload/log redaction tests also pass.

## AJ. Benchmark and profile

The offline [benchmark](../benchmarks/runtime_ingestion.py) creates **20,000 raw
observations and 20,000 canonical events**, inserts in **80 batches of 250**,
retries **1,000 raw/event pairs** (2,000 duplicate operations), and enumerates
as-of cutoffs returning 100/10,000/20,000 events. It then runs **100 synthetic SEC
polls** through a **capacity-one queue** against that populated SQLite database,
and reopens to verify persistence and the earlier cutoff.

Local profile: Python **3.11.16**, SQLite **3.53.4**, macOS **26.6.2 arm64**.
Numbers are descriptive; there is no CI timing threshold.

| Measurement | In memory | SQLite |
| --- | ---: | ---: |
| Insertion seconds | 0.366776 | 4.352642 |
| Events per second | 54,529.25 | 4,594.91 |
| Median 250-event batch milliseconds | 4.577 | 51.253 |
| Duplicate-operation seconds | 0.009647 | 0.102177 |
| Three replay enumerations, seconds | 0.027520 | 1.877001 |

The 100 polls took **0.577099 seconds**, with **2.752 ms** median reported commit
latency, **99 duplicates**, maximum queue depth **1**, and checkpoint generation
**100**. Closed SQLite database size was **74,440,704 bytes**. Batch latency
includes insert/validation work; polling commit latency includes session writes,
acquisition/checkpoint and commit. The polling domain session is small even
though the canonical database is populated; this does not establish constant
time updates for a large domain-session history.

## AK. Engine and schema versions

Engine/package **0.7.0**. All existing quantitative/domain schemas remain
**1.0**. New runtime/ingestion and persistent-store schemas are **1.0**;
internal durable session layout is independently **1.0**. No unrelated schema
version was changed.

## AL. Dependencies

Runtime dependencies remain **zero**. HTTP, SQLite, concurrency, JSON, logging,
CLI and signals use the Python standard library. Existing development tools
were used without installing new dependencies. No server database, Kafka,
Redis, Celery, Airflow, metrics service or distributed infrastructure was added.

## AM. Remaining limitations

- Domain sessions are loaded, copied and serialized with their history. The
  latest cache duplicates immutable persisted contracts; domain queries restore
  materialized engines. This is a correct local baseline with a known scaling
  cost, not an archive-oriented storage layout.
- SEC acquisition covers one configured current submissions resource per
  coordinator registration. Duplicate provider IDs are rejected. Multi-CIK
  watchlists, historical references, gap/completeness reporting and documents
  require later provider/resource orchestration or backfill work.
- A provider's recent window can lose items during long downtime. Overlap and
  cursors recover only what the source still offers; completeness is not claimed.
- Rate windows/cooldowns reset on restart and are not shared across processes.
  Retrying polling clients do not enable redirects. Standalone redirects are
  bounded separately.
- Forced shutdown is cooperative. Python cannot kill a blocked custom worker.
  A timed-out embedded runtime leaves the store open while a worker may own a
  transaction; the foreground CLI exits with failure without blocking on it.
  Uncommitted work retries after restart. An already admitted commit may finish
  atomically. Custom sources/processors must honor bounded operation contracts.
- Parse validation and unchanged adapter ingestion currently perform two parse
  passes. Telemetry documents that cost. Interrupted post-commit diagnostics
  may be absent without compromising committed data/checkpoints.
- There is no streaming driver/reconnect implementation, licensed collector,
  Fed/BEA HTML collector, circuit breaker, automatic reprocessing, raw archive,
  retention optimizer, backup service, final API or trading functionality.

## AN. Exact proposed EIE-7 scope

**Historical Backfill, Dataset Construction & Source Archival**:

1. Provider-aware historical range traversal and pagination/cursors.
2. Resumable backfill jobs with explicit progress/checkpoint semantics.
3. Partitioning and reproducible dataset manifests.
4. Explicit raw-source archival and licensing/retention policy.
5. Dataset provenance and parser/adapter version lineage.
6. Gap detection and historical completeness/vintage reports.
7. Deterministic rebuilds under an explicit reprocessing policy.
8. Revision-preserving historical datasets and causal reconstruction boundaries.

These are proposed next-phase tasks only. EIE-6 stops here.
