# EIE-17 completion — final snapshot, service, integration and hardening

EIE-17 is complete in the working tree. Engine 2.0.0; snapshot/API schema 1.0;
SQLite store 2.1, migration 12. Earlier public component schemas remain 1.0.
No commit or push was made. EIE-0 through EIE-17 are complete and the planned
EIE roadmap is finished.

The final checks ran against the completed runtime implementation. The ordinary
full suite passed 3,164 tests; the separate socket-blocked, branch-enabled run
passed the same 3,164 tests with zero network connection attempts. Combined
statement/branch coverage is 95.720818%, reported to three decimals as 95.721%.

## A. Final architecture

Retained native owners feed a static verification/publication bridge, immutable
catalog manifests and transactionally assembled final snapshots. Typed Python,
CLI and optional local HTTP consumers share that boundary. Existing canonical
resolution, intelligence, graph and market-analysis algorithms remain owners of
their respective semantics. The architecture diagram is in
[architecture](architecture.md).

## B. Package structure

`eie.service` contains schema/models, owner bridges, repository, identity,
assembly, engine, serialization, compatibility, audit/validation, API/HTTP, CLI,
health/readiness, observability, evaluation and recovery. `eie.integration`
contains the provider-neutral protocol. The static persistence codec allowlist
and existing CLI dispatch register the final contracts.

## C. Snapshot schema

Final snapshot schema 1.0 and API schema 1.0 have independently versioned
compatibility entries. Requests and results carry the complete version matrix.
Unsupported schema versions and incompatible retained owners fail closed.

## D. Snapshot model

Frozen typed `EIESnapshot` retains normalized request, stable identity, typed
components, native artifact references, completeness and provenance. Computed
snapshot ID/fingerprint are explicit in wire output. Large native payloads remain
in their original owners and are available through bounded expansion.

## E. Scope and identity

COMPANY binds stable issuer identity; SECURITY binds security identity. SYMBOL
requires symbol and exchange and resolves historical issuer/security/listing
contracts with valid and knowledge cutoffs. Existing industry, sector, index,
market, macro and SEC filer scopes remain explicit. Ticker reuse, symbol changes,
listing closure and ambiguity are tested; ticker is never permanent identity.

## F. Time semantics

`known_as_of_ns` is mandatory. Omitted `valid_at_ns` normalizes to that explicit
cutoff. Source knowledge, acquisition/extraction/build availability and catalog
publication remain separate. No deterministic query reads the wall clock.
Coarse scheduled dates remain DATE_ONLY and missing clocks remain missing.
Upstream strict-prior rules are preserved.

## G. Component model

Nineteen typed component kinds retain AVAILABLE, UNAVAILABLE, NOT_APPLICABLE,
INCOMPLETE, STALE and CONFLICTED states. Optional absence produces an explicit
partial result. Required unavailable/incomplete components fail categorically;
stale/conflicted components retain qualifications. No master intelligence score
is added.

## H. Event component

Canonical revisions, event types, scope, lifecycle/confirmation, sourced clocks,
scheduled precision, source classes and quality remain in bounded summaries and
pinned native references. Event queries and exact scheduled upcoming queries
reuse the final request boundary. Canonical event identity/resolution is unchanged.

## I. Expectations component

Retained native domain and surprise-query artifacts expose estimate vintage,
consensus, actual, prior and information surprise through their original typed
owners. Persisted expectation sessions are replay-checked before publication;
unsupported/orphaned observations are refused.

## J. Evidence and NLP component

Verified extraction runs and EIE-11 field/event resolution runs retain accepted,
rejected, conflicting and retracted evidence. Failed extraction is incomplete,
not evidence of no event. Source instructions remain inert and unsupported
numeric, entity, precision or factual-status upgrades remain rejected. Raw
document text is not duplicated into final snapshots or exposed by expansion.

## K. Graph component

Graph snapshots and propagation references retain native cutoff, revision,
policy and valid-time context. Expansion remains bounded by the final byte cap
and upstream traversal policy. Relationship exposure is distinct from direct
event applicability and empirical market impact.

## L. Intelligence component

EIE-13 novelty, static importance, materiality and uncertainty artifacts are
referenced separately with policy/build pins and availability floors. No new
ontology, scoring subsystem or combined confidence score is introduced.

## M. Study and impact component

EIE-8 descriptive studies and EIE-9 model-specific impact estimates remain
separate components. References pin build/policy and market dataset fingerprints,
including applicable benchmark, control and factor datasets. The service does
not select a best impact model.

## N. Analogue component

EIE-14 analogue sets, historical response distributions and analogue uncertainty
remain native artifacts. Ex-post reaction surprise is a separate component with
its own availability; future candidates/responses do not enter historical views.

## O. Interaction component

EIE-15 collision, bundle, sequence, propagation chronology and contamination
artifacts retain their native policy and event-set identity. Conservative groups
preserve distinct contexts. There is no additive severity calculation.

## P. Reliability and latency component

Source reliability, information latency and source coverage are separate typed
components. Native source class, empirical diagnostics, correction behavior and
availability clocks are preserved. Future diagnostic frames and instrumentation
do not alter earlier snapshots; no master reliability score is introduced.

## Q. Completeness and quality

Completeness is COMPLETE, KNOWN_GAPS, UNKNOWN or NOT_APPLICABLE independently
from component state/quality. Only trusted retained coverage evidence establishes
complete or known-empty coverage. MARKET means the published catalog universe;
it does not claim complete worldwide coverage.

## R. Provenance

Native owner IDs/fingerprints/byte hashes, event revisions, resolution runs,
graph snapshots, extraction lineage, policies, builds, market datasets,
analogue/interaction/reliability artifacts and engine/schema/store versions are
pinned. Publication clocks supplement rather than replace native source clocks.

## S. Deterministic identity

Domain-separated fingerprints cover result-relevant typed state, normalized
request, selection, filters, limits and versions. Snapshot IDs derive from those
fingerprints. Request correlation IDs, metrics and operational timings are
excluded. Same-time conflicting admissible alternatives remain explicit.

## T. Serialization

Canonical deterministic JSON provides stable field order and byte output. Wire
views include computed IDs without circular semantic hashing. Static allowlisted
typed JSON supports persisted contracts without executable deserialization or
input-directed imports. Output bounds are checked on the serialized wire view.

## U. Batch queries

Batches preserve input positions and per-item typed outcomes. Savepoints isolate
bad requests; aggregate overflow rolls back newly created batch snapshots.
Defaults allow up to 500 items and 20 MB response bytes. Count/size overflow is
explicit rather than silently truncated.

## V. Integration contract

`EventIntelligenceProvider` exposes snapshots, batches, components, expansion,
health/readiness and versions. Downstream consumers need no direct SQLite access
or provider-specific ingestion knowledge. The neutral example is in
[integration](integration.md).

## W. Service and API

Optional standard-library HTTP runs only after an explicit operator command and
binds IPv4 loopback. Version, health, readiness, metrics, snapshot/batch, event,
upcoming, impact, analogue, relationship, reliability and expansion routes are
documented. The existing CLI adds snapshot, snapshots, health, ready, version,
validate, backup, restore and serve commands with canonical JSON output.

## X. Errors

Eight stable service categories distinguish invalid request, missing artifact,
ambiguous identity, unavailable history, incomplete data, exceeded bounds,
internal validation failure and not-ready state. Unknown fields, duplicate keys,
wrong types, malformed timestamps and oversized inputs fail explicitly. External
errors contain no raw exception messages or stack traces.

## Y. Health and readiness

Health answers whether the process is alive. READY/DEGRADED/NOT_READY separately
check store version, readability, physical/FK integrity and configured catalogs.
Optional live ingestion outage can be DEGRADED while historical serving remains
available. Closed/corrupt required state fails readiness.

## Z. Observability

Fixed-category counters and accumulated durations cover requests, assembly,
batches, expansion, validation, cache reuse, database locks and readiness.
Structured logs contain safe IDs/status/timing and hashed request correlation;
they exclude raw source bodies, credentials, request payloads and errors. Runtime
source diagnostics retain their existing owners. Metrics do not affect identity.

## AA. Replay guarantees

The same artifacts, cutoff, policies, builds and versions yield byte-identical
snapshots across restart and tested hash seeds. Semantically equivalent
admissible publication order is deterministic. Concurrent future ingestion and
future events, source corrections, extraction, graph, market, intelligence,
analogue, interaction, reliability and latency changes preserve historical bytes.

## AB. Migration hardening

Migration 12 is additive and transactional. Migrations 1–11 preserve exact prior
DDL. Every supported prefix upgrades/restarts; failed DDL rolls back. Unknown
future and partial schema are refused, and old retained bytes remain unchanged.

## AC. Persistence hardening

Immutable triggers protect final manifests, membership, coverage, expectations,
snapshots and seals. Typed payload fingerprints, publication/member indices,
native byte hashes and request identities are checked. One transaction collects
and retains each snapshot. Cutoff seals reject retrospective registration into
frozen views. Corruption/orphan references fail closed.

## AD. Backup and recovery

SQLite's backup API produces a consistent copy under the store lock. Recovery
checks schema, physical/FK integrity and final/native references before exposing
a new exclusive output. Existing/symlink output paths are refused. Backup/restore
then identical snapshot is tested. Archive objects and operator settings require
separate backups; important restored snapshots should receive deep validation.

## AE. Performance hardening

Indexed scope/cutoff lookups, retained artifacts, compact validation and explicit
byte/count limits bound normal work. Deep auditing is opt-in. Persisted reuse
does not bypass compact validation. No upstream algorithm was rewritten solely
for benchmark improvement.

## AF. Security hardening

The new boundary contains no eval, exec, pickle, shell or source-controlled
imports. SQL values are parameterized and owner table names are static. HTTP
accepts no arbitrary paths/URLs/SQL, requires retained snapshot membership for
expansion, rejects unsafe framing and enforces body/output limits. Loopback,
serial work, backlog/timeouts, no wildcard CORS and sanitized logging define the
local boundary. It adds no fake authentication or model/tool executor.

## AG. Compatibility and versioning

Engine, API, snapshot, store and component versions are explicit and independently
checked. Earlier public component schemas remain unchanged. Future unsupported
versions fail closed; future incompatible APIs require an explicit new versioned
boundary. See [compatibility](compatibility.md).

## AH. Operational correctness evaluation

The offline evaluation separately reports deep replay validation, historical
immutability and source-outage separation. Tests establish failure refusal,
transactional migration/publication/batch behavior, availability, identity,
restart and future-data guarantees. This is verified fixture behavior rather
than an unmeasured production reliability claim.

## AI. Information-quality evaluation

Evidence, extraction, identity, intelligence, analogue, interaction, reliability
and coverage states remain separately inspectable. The example has retained
evidence but UNKNOWN completeness for its published components; no synthetic
completeness or universal correctness score is asserted.

## AJ. Market-impact usefulness evaluation

The example separately demonstrates available EIE-8 studies, EIE-9 impact and
EIE-14 reaction surprise references. Coverage/uncertainty remain explicit. EIE
provides event intelligence infrastructure. It does not prove a profitable
trading strategy or predictive alpha.

## AK. Downstream AFRE boundary

A future AFRE can retain snapshot identity, cutoff, versions and native
references alongside its own research inputs. Forecast targets, calibration,
portfolio construction and trading belong downstream. No AFRE, sibling
integration implementation or EIE-18 was added.

## AL. Documentation

README, final architecture, replay/causality, operations, service API,
integration, compatibility, security, performance and this report describe the
completed boundary. Local documentation links were reviewed; roadmap status
now explicitly records EIE-0 through EIE-17 completion.

## AM. Tests added

123 focused collected cases cover snapshots, historical identity/ticker reuse,
partial/required states, bounds, batches, APIs/CLI, adversarial inputs/logging,
end-to-end native references, future perturbations, order/hash-seed/restart,
concurrency, all migration prefixes, corruption and backup/restore. Earlier
tests retain assertions with only necessary target-version updates.

## AN. Full pytest

3,164 tests passed in 202.02 seconds, including the 3,041 prior cases and 123
final-phase cases. The focused final-phase run passed 123 cases in 31.76 seconds.

## AO. Coverage

The full branch-enabled run reports combined coverage of 95.720818%
(95.721% to three decimals), exceeding the 95% gate. It covers 23,391 of 24,086
statements and 7,590 of 8,280 branches. No coverage threshold/exclusions were
changed for this phase.

## AP. Ruff

Lint passes. Formatting check passes for 536 files.

## AQ. Mypy

Strict mypy passes for 318 source files.

## AR. Git diff check

The whitespace/error diff check passes. Full tracked/untracked status and diff
were reviewed; changes remain in the working tree with no commit or push.

## AS. Secret and unfinished-marker scan

The full 561-file tracked/untracked text scan finds no credential/private-key patterns,
no trailing whitespace and no new unfinished implementation markers. Two
pre-existing historical documentation markers remain in the EIE-5 report and
master specification quality-gate prose.

## AT. Socket-blocked run

The full socket-blocked suite passes 3,164 tests in 526.78 seconds with zero
network connection attempts. This same run supplies final branch coverage.

## AU. Final benchmark

Measured unprofiled full snapshot 39.20 ms; compact snapshot 16.07 ms; compact
batches of 100/500 requests 1.800/9.086 s; compact/deep validation 27.40 ms/1.180 s;
restart query 38.08 ms. Full wire snapshot is 84,793 bytes; SQLite fixture is
4,272,128 bytes. The separate profile identifies repeated compact validation,
typed decoding, canonical serialization and hashing as hotspots. See
[performance](final-performance.md) for all workloads and measurement limits.

## AV. Engine, schema and store versions

Engine 2.0.0; final snapshot 1.0; API 1.0; SQLite store 2.1/migration 12; all
previous public component schemas 1.0; Python 3.11+.

## AW. Dependencies

Zero runtime dependencies. HTTP, JSON, SQLite, backup and profiling use the
standard library. Existing optional development tools remain development-only.
No sibling code imports or modifications are introduced.

## AX. Remaining limitations

Coverage is the explicitly published catalog, not a universal event universe.
The service queries one instant; interval scans are not exposed. Upstream
missing clocks, ambiguity, uncertainty and data gaps remain visible. HTTP is a
serial local research adapter without remote authorization/TLS/tenancy or a CPU
deadline. Backup needs separate source archives. Benchmarks use a small synthetic
fixture and repeated compact requests; no production SLA, throughput guarantee,
predictive alpha or profitability is claimed.

## AY. Final EIE status

**EIE-0 through EIE-17 are complete. The planned EIE roadmap is finished.**
Final EIE-17 changes are left in the current working tree for review. No commit,
push, AFRE work or EIE-18 work was performed. Work stops after this report.
