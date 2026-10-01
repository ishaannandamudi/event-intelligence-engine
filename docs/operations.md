# EIE-6 operations

The minimal CLI is available as `eie` after installation or `python -m eie`.
Every database path and endpoint comes from trusted operator configuration.

## Offline controls

```sh
PYTHONPATH=src python -m eie --database /tmp/eie-demo.sqlite ingest-once SEC_EDGAR --cik 123456 --fixture tests/fixtures/sec/submissions.json
PYTHONPATH=src python -m eie --database /tmp/eie-demo.sqlite provider-health SEC_EDGAR
PYTHONPATH=src python -m eie --database /tmp/eie-demo.sqlite checkpoint SEC_EDGAR submissions:0000123456
PYTHONPATH=src python -m eie --database /tmp/eie-demo.sqlite validate-store
```

The fixture is synthetic. File reads are bounded and selected explicitly by
the operator. No test or import enables live mode. Running the first command
again suppresses duplicate raw/canonical state while advancing the successful
poll generation. CLI output is JSON. Exit codes: 0 success, 1 failed poll,
invalid store or forced/fatal run, 2 configuration/persistence setup failure.
Read-only operational commands can open an empty/current store; an unsupported
schema fails closed.

## Explicit live mode

Set `EIE_SEC_USER_AGENT` privately in the process environment to an operator
organization/contact identity. It is excluded from policy repr, logs, domain
settings, persisted records and fingerprints. No commercial secret/key boundary
is needed or bundled in this phase. Transport rejects query strings and
userinfo and exposes no Authorization/API-key header interface.

```sh
PYTHONPATH=src python -m eie --database /tmp/eie-live.sqlite ingest-once SEC_EDGAR --cik 320193 --live
PYTHONPATH=src python -m eie --database /tmp/eie-live.sqlite run SEC_EDGAR --cik 320193 --live
PYTHONPATH=src python -m eie --database /tmp/eie-bls.sqlite ingest-once BLS --live
```

These examples intentionally perform real acquisition only when invoked.
The SEC contact environment variable must be set before SEC live commands.
`run` stays in the foreground. SIGINT/SIGTERM request bounded graceful shutdown;
previous signal handlers are restored. There is no daemonization, service API,
hot reload, distributed scheduler or deployment step.

## Embedded API and configuration

Open `SQLiteEventStore` with a trusted file path and bounded busy timeout.
Register `ProviderRegistration` objects and explicitly set
`RuntimeConfig(enabled=True)`. Runtime defaults are one ingestion worker,
two fetch workers, queue capacity 16, 30-second shutdown timeout and 0.1-second
scheduler resolution. Providers default to disabled. Generic registrations
default to 60-second polls and 300-second staleness. Limit validation rejects
non-finite/negative durations, invalid capacities, incompatible settings,
duplicate providers and SEC live contact/rate violations before acquisition.

`ingest_once(provider)` performs one idle-runtime poll and commit.
`fetch_once(provider)` plus `process_next()` exposes the bounded queue for
controlled embedding/tests. `start()` drives continuous polling. Domain
sessions restore from the database on every write; query sessions can be
loaded explicitly with `load_session(namespace)`.

## Diagnostics and retention

Inspect `runtime.metrics.snapshot(runtime.queue.depth)`,
`runtime.health(provider)`, stored `poll`/`health` operations, checkpoint,
quarantine and `store.counts()`. Metrics cover polls/failures, fetched/ingested
items, duplicates, quarantines, normalization/persistence/provider failures,
rate-limited final polls, queue depth and accumulated total latency.
Per-attempt 429 history is also exposed by the HTTP source's rate limiter.

The `eie.runtime` logger emits `provider_poll` with sanitized fields for provider,
operation, outcome, duration, error category and generation. It never dumps
source bytes, contact headers, exception messages, request URLs or credentials.
No external metrics or logging dependency is added.

Retention defaults to `METADATA_ONLY` with permission `UNKNOWN`. Acquisition
hash and safe response metadata are stored. `BOUNDED_RAW` requires explicit
`ALLOWED` permission and a byte limit covering the response bound. `RESTRICTED`
and `UNKNOWN` cannot enable raw retention. This is a configuration boundary,
not vendor-specific legal policy, historical archival, or a retention optimizer.
Quarantine uses the same explicit policy. Protect DB/WAL/SHM files and backups.

To retry an identical quarantined item deliberately, call
`store.clear_quarantine(provider, resource, payload_hash, adapter_version)`.
Then poll again. There is no automatic historical reprocessing. Loss of source
window coverage after extended downtime must be treated as a source limitation,
not certified completeness.

## Verification and profile

The standard suite is deterministic and internet independent. HTTP tests use
fake connections and exercise Python's real HTTP parser over fake sockets.
Crash hooks simulate failure after fetch, preparation, raw preparation,
persistence, after parse, before checkpoint/commit and after commit. No test hits SEC or BLS.

```sh
PYTHONPATH=src:. python benchmarks/runtime_ingestion.py
```

The benchmark builds 20,000 synthetic raw observations and canonical events,
compares in-memory and SQLite insertion in 250-event batches, retries 1,000
raw/event pairs, runs three as-of enumerations, then performs 100 fixture polls
through a capacity-one queue against the populated SQLite store. It measures
throughput, batch latency, duplicate suppression, query time, poll commit
latency, queue depth, checkpoint generations and final database size. It has
no strict CI timing threshold and uses no network.

Physical process-exit tests also leave SQLite transactions open or terminate
immediately after COMMIT, then reopen and verify data/checkpoint atomicity.
Prepared-batch counters count newly created raw items up to the adapter's item
count; raw/domain creation counts and duplicate counts remain separately exposed.
