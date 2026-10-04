# EIE operations

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
previous signal handlers are restored. The ingestion runtime has no daemonization, hot reload, distributed scheduler or deployment step. The optional final snapshot service is described below.

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

## Historical foreground commands (EIE-7)

A definition file is `codec.dumps(DatasetDefinition(...))` typed JSON. It contains
no contact User-Agent. An offline page file is an ordinary JSON array with exactly:
`provider`, `resource`, `start_ns`, `end_ns`, `cursor`, `body_hex`, `source_reference`,
`next_cursor`, `coverage`, `too_many_results`. Bodies contain either audited typed
`HistoricalInput` tuples or native SEC submissions JSON according to registration.
Files are bounded at 20 MB; pages obey the source's lower bound. No arbitrary parser
module or executable type is loaded from a file.

```sh
eie --database events.sqlite backfill plan --definition definition.json
eie --database events.sqlite backfill run --definition definition.json --pages pages.json --archive archive --max-pages 1
eie --database events.sqlite backfill status --build BUILD_ID
eie --database events.sqlite backfill resume --build BUILD_ID --pages pages.json --archive archive
eie --database events.sqlite dataset manifest --build BUILD_ID --output manifest.json
eie --database events.sqlite dataset validate --build BUILD_ID --archive archive
eie --database events.sqlite dataset events-as-of --build BUILD_ID --at-ns 123
eie --database events.sqlite dataset rebuild --build BUILD_ID --archive archive --build-version archive-rebuild
```

`plan` neither creates the specified database nor opens an archive/network. Run and
resume require explicit offline `--pages` or `--live`. Historical live CLI supports
only native SEC with definition-matching CIK resources/identity bindings and
`EIE_SEC_USER_AGENT` in the process environment. Definition capability/pins must
match `sec_history(...).spec`; overrides that disagree fail before acquisition.
Contacts are never written to dataset identity. New parser code/reprocessing and
partition reuse are explicit Python APIs. The CLI rebuilds with installed pinned
processors; it does not infer a code migration. Status omits opaque cursor values.

SIGINT/SIGTERM requests a pause and finishes bounded admitted work. The Python API
also offers `request_cancel`; cancelled jobs require a new build. Page budgets pause
and reopening resumes durable progress. Queries/validation require a finalized build;
status remains available for partial/failed jobs. Damaged/missing archive objects cause
explicit validation/rebuild failures without refetch. Metadata-only retention
intentionally prevents raw rebuild. Back up SQLite and archive objects together.

Offline example: `PYTHONPATH=src python examples/historical_rebuild.py`.
Profile: `PYTHONPATH=src:. python benchmarks/historical_build.py` (200 partitions,
10,000 synthetic records, no timing threshold). See [historical backfill](historical-backfill.md),
[manifests](dataset-manifests.md), [archive](source-archive.md),
[completeness](data-completeness.md) and [completion report](EIE-7-completion.md).

## Final snapshot serving (EIE-17)

Opening a store applies additive transactional migration 12 (2.0 → 2.1). Back up before upgrading; unknown newer/partial schema fails closed. Source ingestion continues through established explicit operator commands. Publish supported retained owners through the typed catalog bridge, supplying an actual publication clock; final service publication does not replace upstream ingestion or resolution.

```sh
PYTHONPATH=src:. python examples/final_snapshot.py
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite version
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite health
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite ready
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite snapshot /trusted/request.json
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite snapshots /trusted/batch.json
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite validate eies_SNAPSHOT_ID --deep
PYTHONPATH=src python -m eie --database /tmp/eie.sqlite serve --host 127.0.0.1 --port 8765
```

The example creates a temporary offline database and closes it. Retain `examples.final_snapshot.build(Path(...))` under a trusted directory when inspecting a durable example. All CLI output is canonical JSON. Validation returns an explicit result and exit status; setup/persistence failures emit a generic message. `serve` owns one foreground worker. Stop it with Ctrl-C; the HTTP context and CLI finally block close resources. It does not launch ingestion automatically.

Health means process alive. Readiness checks store schema/integrity/readability and optional configured catalog presence. Typed callers can report `live_ingestion=False`: this is DEGRADED with historical queries still available. A closed, corrupt or missing configured store/catalog is NOT_READY. The HTTP adapter reports historical readiness; runtime/provider diagnostics are read through their established owner. Source outages do not erase retained history.

### Backup and restore

```sh
PYTHONPATH=src python -m eie --database /trusted/eie.sqlite backup /trusted/new-backup.sqlite
PYTHONPATH=src python -m eie --database /trusted/operator.sqlite restore /trusted/new-backup.sqlite /trusted/restored.sqlite
```

Outputs must be new paths in trusted existing directories. Backup uses SQLite's consistent backup API, rather than copying an active WAL database file. Restore verifies a copied candidate before exposing its exclusive destination. Stop serving before switching the active path; open the restored store, run readiness and deep validation of important retained snapshots, then point the service at it. Preserve source archive directories, retention permissions and external operator settings separately. Check free disk space and file permissions. Backups neither overwrite existing files nor bypass schema compatibility.

### Failure recovery and telemetry

Failed publication/snapshot/batch transactions roll back. Do not repair corrupt typed owner bytes or immutable snapshots in place. Restore an independently verified backup or reconstruct a new catalog/build lineage using retained permitted source evidence. New data cannot be backdated into a sealed catalog cutoff. Keep earlier audits intact.

Logs use fixed metadata categories and hashed request correlation, with no raw source bodies/errors/secrets. `/metrics` exposes counts and accumulated durations outside snapshot identity. Connection timeouts bound read/write, not calculation runtime. Request limits, serial service work and original owner policy caps bound workload; place any remotely exposed service behind separately managed authorization, TLS and admission control. See [API](service-api.md), [security](security.md), [compatibility](compatibility.md) and [performance](final-performance.md).
