# Provider runtime

`runtime/config.py` holds immutable operational limits, `clock.py` separates
UTC and monotonic time, `sources.py` defines polling/acquisition, `providers.py`
validates registrations, `coordinator.py` schedules bounded threads, and
`diagnostics.py`/`shutdown.py` own telemetry and foreground termination.
`ingestion` contains typed operational contracts, processors, a bounded queue,
checkpoint policy and the atomic batch writer. `transport` contains HTTP,
protocols, URL policy, retries, errors and per-provider rate limiting.

## Registration and scheduling

Provider IDs are deterministic, unique and processed in sorted registry order.
Each registration declares a resource, processor namespace, enabled flag,
poll interval, staleness interval, response bound and retention policy.
Result-affecting domain settings must match the persisted namespace at startup
and again inside each adapter preparation transaction. Changes to a populated namespace's normalization settings require an explicit
migration/reprocessing plan; this runtime refuses such changes. A new namespace
is suitable for disjoint data, and does not authorize replaying old identities
under a new policy. Operational
timeouts and workers can change without altering old result fingerprints.

`start()` creates exactly the configured fetch and ingestion workers, plus an
optional scheduler thread. `tick()` dispatches currently due providers in
deterministic order, with at most `fetch_count` polls in flight including their
queued ingestion. One provider has at most one outstanding batch. Poll cadence
uses monotonic deadlines and does not generate an unbounded catch-up burst.
`start(schedule=False)` and `tick()` support controlled tests.

The acquisition queue is bounded. A full queue rejects that attempt with
`BACKPRESSURE`, records degraded health and leaves its checkpoint unchanged.
The source must offer the item again through its overlap/resume contract.
Current SEC polling naturally overlaps the recent submissions response; EIE
does not promise recovery of items that have already fallen out of a source's
current window. General downtime gap detection and historical traversal belong
to EIE-7.

## Retry and rate control

`RetryPolicy` defines finite attempts, exponential base delay and maximum delay.
Default HTTP attempts are three with 0.5-second exponential backoff, capped at
30 seconds. Retry-After seconds or HTTP dates are parsed and capped. Eligible
statuses are 429, 500, 502, 503 and 504; timeouts/connection failures are eligible.
Most 4xx and all parse/schema/normalization/policy failures are ineligible.
Waits are cancellable and use an injected runtime clock. There is no jitter.

Each HTTP source has its own sliding-window `RateLimiter`. Every HTTP attempt,
including retries, consumes capacity. Retrying clients reject enabled redirects
at construction, preventing additional redirect requests from bypassing that
budget. Standalone transport still supports policy-bounded redirects.
429 extends its monotonic pause.
`client.limiter.state()` exposes capacity, throttling, next eligible monotonic
time and observed 429 count. Rate windows are process-local and reset at restart;
checkpoint and health persistence do not establish a distributed rate budget.
Operators should use one coordinated SEC acquisition owner and avoid restart
bursts. No optional circuit breaker is added; finite transport retries and
quarantine handle their respective failure classes.

## Failure isolation and health

Operational outcomes distinguish transport, rate limit, source unavailable,
schema, parse, normalization, persistence, checkpoint, configuration and
backpressure failures. Source failure does not assert a healthy empty calendar
or create negative coverage. Other providers can continue. A global persistence
failure stops scheduling and fences later uncommitted writes.

`health(provider)` exposes healthy, degraded, stale, rate limited, schema
mismatch, unavailable, stopped or unknown state, last attempt/success and
consecutive failures. Staleness is derived using registration policy. Health
history and `PollResult` are persisted independently of canonical history.
Success resets failure streaks, including healthy 304 responses.

Malformed payloads are quarantined by provider/resource/hash/adapter version.
The identical quarantined payload is fetched if necessary to detect change,
but normalization is suppressed on later polls. A changed payload can recover;
a manual clear or explicitly new adapter version permits another attempt.
Quarantine records preserve first/last failure, attempts, hash and category.
They carry no payload by default. Manual retry does not retrospectively replace
already normalized historical records.

## Checkpoints and streams

Checkpoint schema `1.0` supports source item, accession, timestamp, sequence and
opaque cursor kinds, generation, last source item, acquisition/completion UTC,
ETag and Last-Modified. Numeric sequence/timestamp cursors keep the maximum
acknowledged value during overlap or out-of-order delivery; changing cursor
kind within a resource fails. Opaque/accession/source-item ordering remains a
source-specific contract. The resume token must not contain credentials.

The queue and batch writer accept provider-neutral acquired bytes and typed
cursors. EIE-6 supplies a polling driver; it does not implement a streaming
client or force a streaming provider to implement polling. A later bounded
stream driver can use the same preparation/persistence/checkpoint boundary.
No paid stream or generic reconnect framework is claimed here.

## Shutdown

Shutdown stops new scheduling and cancels pending retry/rate waits, then waits
for bounded fetches and drains acquired items through the workers. Active
transactions finish or roll back. Final stopped health is persisted and the
store closes after all owned workers exit. `shutdown()` returns whether it
completed within its real monotonic deadline.

If a custom source or processor exceeds the deadline, forced shutdown requests cancellation at
its next cooperative commit boundary and returns `False`. Python cannot safely kill a thread.
The fixed worker threads are daemon threads; the store is left open while a
worker may own its transaction. The foreground CLI exits with failure without
blocking on that connection. On a continuing embedded process, release the
custom operation and call shutdown again before closing/reusing its database.
All uncommitted items are retriable. A commit already admitted at the
shutdown boundary can finish atomically; its committed checkpoint remains valid. Custom sources/processors must respect their bounded runtime contracts.
