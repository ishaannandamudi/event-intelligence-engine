# Durable persistence

`SQLiteEventStore` implements the existing `EventStore` protocol. The in-memory
store remains available. Both use the same insertion validator for evidence,
source class, revision continuity, causal availability, supersession and
lifecycle rules. Historical resolution occurs before domain filtering.

## Schema 1.0

| Table | Purpose |
| --- | --- |
| `schema_migrations` | Ordered applied migrations and independent store version |
| `observations` | Immutable raw contracts, indexed by provider/item |
| `events` | Immutable canonical revisions, indexed by availability/identity |
| `evidence` | Raw-to-canonical relational links, with foreign keys |
| `domain_records` | Immutable schedule, entity, filing, corporate and expectation contracts |
| `domain_evidence` | Raw-to-domain links for filings, claims and observations |
| `sessions` | Latest versioned adapter/engine state needed for exact restart queries |
| `checkpoints` | Provider/resource generation, cursor and HTTP conditionals |
| `operations` | Append-only runtime health and poll outcomes |
| `quarantine` | Provider/resource/hash/adapter-version failure metadata |
| `acquisitions` | Hash, trusted resource, acquisition metadata and optional bounded bytes |

Indexes cover provider item, event availability, issuer, accession, estimate
event/measurement, and operational provider time. Contract payloads use an
explicit allowlisted typed JSON format. There is no pickle, eval, dynamic type
import, or execution of stored names. Duplicate JSON fields, unknown types,
unsupported versions and incompatible layouts fail closed.

Domain records are immutable, content-addressed contracts. The `sessions` row
is a replaceable restart cache containing full typed histories, entity mappings,
indexes, adapter identity caches and diagnostic bookkeeping. Only its latest
state is stored; it is not a sequence of raw historical source archives.
Restart materializes this state into the existing domain engines, preserving
their query APIs and fingerprints. Derived consensus and surprises are
recomputed from stored causal inputs using the existing query implementations.

The state layout has an independent `1.0` version and an explicit list of
supported fields. Future changes to engine internals require a deliberate
layout migration/reader. Loading a database never renormalizes its old source
records with a new parser. Seeded external identity provenance can remain
outside this store, as existing APIs permit; it is not silently invented as raw
evidence. Canonical, filing, claim and expectation evidence links are enforced.

## Atomicity and recovery

```text
BEGIN IMMEDIATE
  load current session
  stage an entire adapter batch
  append raw evidence
  append validated domain and canonical revisions
  replace restart cache
  persist permitted acquisition metadata/bytes
  compare and advance checkpoint generation
COMMIT
```

Checkpoint advancement requires a transaction owned by the calling thread.
Generation comparison prevents conflicting writes. A failure anywhere before
commit rolls back the entire batch, including its checkpoint. A crash after
commit retains both. On retry, adapters restore their source identity caches
and preserve original observation clocks; overlap does not duplicate events.
Health and poll diagnostics are separate operational transactions. A crash
after data commit may omit its final telemetry, while data and cursor remain
recoverable together.

Nested store methods use savepoints. An `RLock` serializes writes within one
store instance. `BEGIN IMMEDIATE` and bounded busy timeouts serialize separate
connections/processes. Workers load shared namespace state while holding that
transaction, preventing lost updates between providers using one domain engine.
Networking happens before the transaction.

Foreign keys are enabled. File databases use WAL and `synchronous=FULL` to
support readers with a serialized writer and durable commit boundaries on a
local filesystem. SQLite `:memory:` retains SQLite's in-memory journal mode.
Migrations execute ordered statements in one transaction. Unversioned,
future-version, partial, altered or incompatible schemas are refused. Failed
DDL rolls back. WAL is not a substitute for a suitable local filesystem or
reliable storage hardware.

## Queries and scale

Raw/canonical protocol queries run against indexed SQLite rows. SQLite resolves
latest visible revisions, then reuses domain applicability/range logic.
Schedule, identity, filing, corporate and expectation queries use a restored
session, for example:

```python
session = store.load_session("sec")
filings = session.filings_for_cik(cik, as_of_ns)
```

The local baseline prioritizes equivalence and recovery. Domain-session loading,
deep copying and serialization are proportional to that namespace's history;
they are not constant-time updates. Large as-of enumerations materialize their
results. The benchmark measures this implementation, with no CI timing target.
This phase introduces no server database, distributed writer or retention
optimizer.

`validate()` checks SQLite integrity, foreign keys, typed stored contracts and
restart layouts. `counts()` exposes row counts. Protect the database and WAL/SHM
files using operator-controlled filesystem permissions. Use SQLite's backup
API or a clean shutdown for backups; copying only the main file during active
WAL writes is unsafe. A backup service is outside EIE-6.

## EIE-7 migration and historical records

Engine 0.8.0 uses persistent-store schema **1.1**. Ordered migration 2 adds historical
jobs/partitions/progress, archive object metadata, accepted and failed acquisitions,
raw observations, indexed typed facts, lineage, gaps, operation records and frozen
manifests. Migration 1 and existing EIE-6 rows/session layout remain unchanged;
failed DDL rolls back, and unknown/partial schemas are refused.

Linked historical page writes require the current thread's owned transaction.
Durable page metadata/data precedes any progress increase; SQLite commits all linked
records or rolls back all of them. Raw byte publication precedes the database commit
and may leave an unreferenced object after a crash. SQL triggers freeze finalized
builds, including updates that try to move rows into another frozen build. Archive
byte verification is performed by `validate_dataset` because SQLite alone cannot
certify filesystem content. See [archive](source-archive.md) and
[manifests](dataset-manifests.md).

## EIE-8 study persistence (store 1.2)

Ordered migration 3 adds indexed immutable study definitions/results/run manifests and
historical event lookup indexes. Existing 1.0/1.1 DDL, live rows and session layout remain
unchanged; failed DDL rolls back. Study results retain bounded exact inputs, definitions,
source dataset pins and deterministic hashes. Reads verify scalar indexes, content hashes
and recalculated measurements; writes also bind the historical fact/revision/quality and
manifest. Completed artifacts have update/delete refusal triggers. See [event studies](event-studies.md).

## EIE-9 model persistence (store 1.3)

Ordered migration 4 adds indexed immutable impact model/spec/fit, result, sensitivity
set and run tables. All previous DDL remains intact. Appends verify the original study
through EIE-8's historical build/fact checks in the same transaction; failed runs/sets
leave no partial artifacts. Reloads compare scalar indexes, separately retained spec/
fit payloads, member/raw-study references, exact recalculation and fingerprints. UPDATE/
DELETE triggers enforce completed-artifact immutability. As-of result availability is
strictly prior to the query. See [counterfactual impact](counterfactual-impact.md).

## EIE-10 graph persistence (store 1.4)

Migration 5 adds graph entity/node/identifier/listing indexes, semantic edges, provider
claims, canonical edge revisions, source-item receipts, snapshots with exact node/edge
membership, and propagation results. Every graph table has update/delete refusal
triggers. Foreign keys link raw observations, endpoints, predecessor revisions and
snapshot members. Existing migration DDL and stored study/impact payloads are preserved;
a failing migration rolls back its tables and migration record together.

Indexed source/target/type/valid/known/provider/exposure searches support bounded local
adjacency. Raw evidence, all item claims/closures, canonical resolution and checkpoint
generation commit atomically. Restart validates typed payloads against indexes and
references, then recalculates snapshots/candidates at their pinned cutoff. Graph writes
respect an ordered knowledge ledger and persisted snapshots seal their cutoff. No graph
database dependency or live graph transport is introduced. See [graph replay](graph-replay.md).
