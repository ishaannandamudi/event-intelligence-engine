# EIE-7 completion report

Completed 2026-10-01 at engine **0.8.0**. Changes remain in the current working tree.
Nothing was committed or pushed; HEAD remains `ae7f915` (`Add live ingestion runtime
and durable persistence`). No sibling repository source/project files were edited.
EIE-8 is not implemented.

The full master specification, EIE-0–6 foundation, EIE-6 report, historical-relevant
persistence/runtime/domain tests and initial clean status were inspected. The baseline
708 tests passed before implementation. Existing public domain/live contracts remain
unchanged; only three existing version assertions changed for engine/store versions.

## A. Architecture

Historical source → bounded deterministic partition/page acquisition → eligible
content-addressed archive → pinned normalization → atomic append/index records,
lineage and progress → frozen manifest → build-bound queries/rebuild. Source historical
availability is separate from actual local acquisition. Live sessions keep their
established semantics.

## B. Package structure

`backfill`: immutable capabilities/jobs/progress/gaps, planner, coordinator, audited
sources, SEC traversal, explicit rebuild/reuse and CLI. `archive`: acquisition/object
contracts and bounded filesystem storage. `datasets`: definition/fact/manifest
contracts, SQLite repository, builder, validation and historical queries. Existing
persistence/runtime modules gain the migration, codec allowlist and CLI dispatch.

## C. Historical provider contracts

Pinned source specs declare provider, sorted resources, source class, identity
bindings, range/page/body/item/split bounds, supported/PIT coverage and assumptions.
Sources return bounded historical pages; trusted processors pin adapter/parser versions.
Provider bytes cannot upgrade configured source class or substitute evidence context.

## D. Backfill job model

Immutable semantic definitions derive deterministic dataset IDs. Explicit build
version and optional parent derive build/job IDs. Creation time is excluded from
semantic identity. Recreating the same semantic job resumes its original stored clock.

## E. Partition planning

Pure `[start,end)` plans cover every resource with consecutive bounded intervals,
including splits at capability boundaries. Plan bounds are checked before allocation.
No planning operation writes a database/archive or accesses the network.

## F. Pagination/range traversal

Bounded opaque continuations persist durably. Repeated/circular cursors, repeated
bodies with further continuations, excessive pages/results/bytes and split-limit
failures become explicit gaps. Uncommitted too-many-results partitions split at exact
midpoints; parent/child lineage is retained. Final overlapping pages deduplicate safely.

## G. Multi-resource/multi-CIK orchestration

One coordinator rotates providers/resources fairly with 1–8 workers and bounded
submitted tasks. Native SEC CIKs share one respectful contact-configured HTTP client
and rate limiter. Fixture tests cover multiple CIKs, resources and independent failures.

## H. Resume/pause/cancel

Page budgets and pause stop admission and finish bounded active work. Reopening resumes
committed progress, including a process-death `RUNNING` job. Transient attempts are
bounded per page; explicit resume retries failed partitions. Cancelled jobs are terminal
and require a new build. Custom sources must honor cooperative stop/transport bounds.

## I. Partition atomicity

Accepted observations, typed facts, acquisition metadata, lineage, gaps and progress
share one owned SQLite transaction. Progress requires durable linked pages. Seven
fault boundaries prove rollback before commit and recovery beyond committed pages.
Global persistence failures remain failed/resumable when storage is usable. Finalization
refuses incomplete roots, invalid split lineage, missing children, unlinked exhaustive
completion and unrecorded gap claims.

## J. SEC historical vertical slice

Native SEC current submissions and CIK-specific official history references are
traversed with existing parsing/normalization and transport. No documents, exhibits,
XBRL or ticker maps are fetched. Verified issuer CIK bindings are explicit and pinned.
Acceptance supports labeled reconstruction; all SEC builds disclose unknown complete
metadata-vintage coverage. Same verified context preserves live/historical raw, filing
and canonical event identities.

## K. Macro/estimate historical boundaries

Audited typed extracts support canonical, scheduled, corporate, filing, entity,
estimate, actual and guidance contracts. Exact vintages/publication cutoffs must be
substantiated by the provider. Live BLS calendars/current estimate snapshots do not
establish exhaustive historical release/correction coverage. No universal macro vintage
or licensed historical estimate collector is added.

## L. Archive architecture

Exact uncompressed page bytes use SHA-256 object names and deduplicate across
acquisitions/builds. SQLite retains object/acquisition metadata and lineage. Published
parse/cursor failures retain separate failed-acquisition records for reproducible gaps.
Failures before a page is returned do not invent archive references.

## M. Retention/license handling

Metadata-only is the default. Raw retention requires explicit allowed permission and
both retention/archive size bounds. Restricted/unknown permission cannot enable raw.
Entitlement is operator/provider policy; no URL implies licensing permission.
Metadata-only content remains queryable but cannot support raw rebuild.

## N. Archive integrity/security

Atomic fsynced publication never overwrites an existing hash object. Every read checks
regular-file type, byte length and hash. Directory descriptors and no-follow operations
reject root/prefix/object symlinks and FIFO objects. Provider bytes never control paths.
Missing/corrupt bytes invalidate validation/rebuild without automatic refetch; a crash
may leave only an unreferenced object.

## O. Dataset definition

Definitions pin namespace/range, source resources/capabilities/trust/identity,
engine/parser/adapter versions, settings, partition/retention policies and optional
canonical family/entity filters. Supporting domain records and source pages remain
for audit. Contacts cannot enter persisted dataset settings.

## P. Immutable manifest

Manifests freeze job/definition, versions, partition results, archive hashes, counts,
source-quality/completeness dimensions, gaps and content digests. SQL triggers guard
finished build rows and manifests, including attempts to move rows into a frozen build.
Operational clocks/build labels/hostnames do not alter content identity.

## Q. Lineage

Provider/resource/archive page → acquisition/partition → raw observation → domain/
canonical fact → build/settings/parser/adapter is queryable. Matching live raw evidence
retains its original genuine clock. Per-fact and acquisition provenance distinguish
live observation, historical reconstruction and archive replay. Replay time is separate
from original acquisition time.

## R. Gap model

Typed scoped gaps distinguish unavailable/unsupported/non-PIT history, pagination,
archive, parsing/schema failures, partial/unknown coverage, license/identity limitations
and temporal ambiguity. Retryability, discovery time and evidence codes are explicit;
only semantic gap fields affect content fingerprints.

## S. Completeness reporting

Reports separate completed, known-empty, gapped, failed, pending and split partitions,
with per-resource coverage and quality/reason counts. Known empty requires exhaustive
accepted traversal and zero matching facts; duplicate-only pages never certify empty.
Validation returns valid, valid with known gaps or invalid. No confidence score or
universal completeness claim is made.

## T. Reconstruction-quality semantics

Authoritative PIT and source-timestamp reconstruction can carry exact cutoffs under
their declared proof/assumptions. Date-only/current/unknown records have no fabricated
exact cutoff and are excluded from exact queries. Raw/local clocks remain actual.
Canonical historical projections omit EIE observed/ingested/updated times. Source
claims are qualified separately from EIE observation history.

## U. Deterministic rebuild

Explicit same-version archive rebuild yields identical content fingerprints and leaves
original manifests/records unchanged. Reprocessing with changed parser/adapter versions
creates an isolated child build. Eligible bytes are verified before new build creation.
No-reprocess/missing parser/restricted/missing/corrupt inputs fail explicitly. Frozen
completed intervals can be explicitly verified/reused for compatible extensions.

## V. Parser/adapter versioning

Definition, source, fact, acquisition and lineage pin versions. Same-version rebuild
rejects pin mismatches and output drift before manifest freeze; new-version policy requires a real pin change. Reprocessing code
is application-supplied trusted code, never a module path loaded from provider bytes.
Stored engine versions remain readable; writes require the installed pinned engine.

## W. Live/backfill coexistence

EIE-6 live tables, checkpoints, sessions and causality stay intact. Historical build
records use separate tables on the same transaction boundary. Existing live bytes do
not change when matching history arrives. Historical availability never masquerades
as a backdated live observation.

## X. Historical identity/ticker handling

Stable issuer/security/listing contracts support exact knowledge and valid-time ticker
resolution, including ticker reuse. Current SEC names/tickers do not become historical
listings. Missing bindings remain unresolved; actor/issuer distinctions remain intact.
Resource alternatives preserve one canonical logical event while retaining provenance
facts. Domain/revision fact counts can include provider/resource alternatives.

## Y. Persistence/migrations

Ordered migration 2 raises SQLite schema 1.0 → 1.1 and adds indexed jobs/partitions,
archive metadata, accepted/failed acquisitions, historical observations/facts/lineage/
gaps/operations and immutable manifests. Original migration and live rows/session layout
are preserved. Failed DDL rolls back; unknown/partial schemas fail closed. Migration
compatibility/rollback and restart tests pass.

## Z. Scalability/session-cache changes

Historical pages append indexed rows without copying/serializing complete live sessions.
Fact/lineage indexes support incremental writes and filtered as-of query streams.
Manifest record hashing streams a build; resource counts and revision checks avoid
quadratic loops. Validation/query materialization is confined to the requested build.
Existing live durable-session materialization remains unchanged.

## AA. CLI/runtime controls

`backfill plan/run/resume/status` and `dataset manifest/validate/events-as-of/rebuild`
are available through the existing CLI. Files/pages are bounded and typed/allowlisted.
Run/resume require explicit offline pages or live SEC. Planning is pure, manifest export
is typed JSON, signal shutdown requests pause, and errors are sanitized. New-code
reprocessing, cancellation and compatible reuse are Python APIs.

## AB. Security

No pickle, eval, arbitrary persisted-type imports, cloud SDK or unchecked provider path
is introduced. HTTPS references exclude credentials/query/fragment. Trusted registration
pins source/evidence classes and identities. Contacts stay in runtime environment;
operations omit raw bodies/arbitrary exception messages. Filesystem and payload bounds,
symlink refusal and hash verification have explicit tests.

## AC. Documentation

Added historical-backfill, dataset-manifests, source-archive and data-completeness guides.
Updated README, master spec, architecture, replay/causality, persistence, live ingestion,
provider runtime, source policy, SEC, expectations and operations. The executable offline
example proves partial build → reopen/resume → archive/manifest → validate/as-of →
identical archive rebuild. Benchmark usage and remaining assumptions are documented.

## AD. Tests added

**190 additional offline cases**, for **898 total**. New suites cover strict contracts,
plans, range/cursor/split limits, worker fairness, resume/pause/cancel/retry, seven crash
boundaries, archive retention/security/integrity, immutable manifests, damaged metadata/
lineage/indexes, explicit rebuild policies, reuse, CLI, SEC multi-CIK/reference traversal,
amendments, schedule changes, estimate vintages, macro corrections, late discovery,
ticker reuse and live/historical duplicates. Existing 708 tests retain their behavior.

## AE. Full pytest result

Full pytest: **898 passed in 3.24 s**. Full branch-instrumented pytest:
**898 passed in 6.30 s**. Full socket-blocked pytest: **898 passed in 3.24 s**,
with **zero network connection attempts**. Every automated source uses deterministic
fixtures/fakes; no live request was made.

## AF. Coverage

Combined branch-enabled overall coverage: **96.51%** (9,342 statements; 3,278 branches).
New `backfill`/`archive`/`datasets` group: **96.73%** (1,803 statements; 708 branches).
The required 95% overall gate passes. Percentages use covered statements plus covered
branches divided by their combined denominator.

## AG. Ruff

Lint passes. Repository-wide formatting check passes. Final source/test/example/profile
files are formatted; no lint suppression was introduced for unfinished functionality.

## AH. Mypy

Strict mypy passes **137 source files** with zero issues.

## AI. Diff check/review

`git diff --check` and new-file whitespace scan pass. Modified foundation/version tests
and new implementation/tests/docs were reviewed. Initial and final Git status reviewed;
changes are intentionally unstaged/uncommitted and HEAD remains `ae7f915`.

## AJ. Secret/unfinished marker scan

Credential/private-key patterns found no credentials. No actionable unfinished markers
were found. Two pre-existing documentation matches describe review gates in the master
spec and EIE-5 report; neither represents unfinished code. Synthetic negative URL/contact
fixtures use invalid/example domains.

## AK. Benchmark/profile

Offline local profile: 200 consecutive partitions × 50 audited synthetic canonical event
items each; one bounded page per partition, sequential page pipeline, SQLite WAL and raw
filesystem archive. Fixture generation is outside the measured acquisition/build time.
Original and rebuilt builds share exactly 200 content-addressed objects; both validate.
No timing threshold or production throughput claim is imposed.

| Measurement | Result |
|---|---:|
| Partitions | 200 |
| Source items / normalized records | 10,000 / 10,000 |
| Archive objects / bytes | 200 / 21,018,200 |
| Database bytes including original + rebuild + WAL at sample | 160,059,152 |
| Planning | 0.0105 s |
| Page build | 10.9491 s |
| Source item throughput | 913.32/s |
| Manifest construction | 3.0222 s |
| Original validation | 5.9684 s |
| Archive rebuild including manifest | 14.5224 s |
| Rebuilt content fingerprint | Identical |

Run `PYTHONPATH=src:. python benchmarks/historical_build.py`. Results depend on local
hardware/filesystem; this workload measures append/index construction and reproducibility.

## AL. Versions

| Contract | Version |
|---|---|
| Engine / package | 0.8.0 |
| Existing domain/config/observation schemas | 1.0 |
| Existing runtime/ingestion schemas | 1.0 |
| Durable session layout | 1.0 |
| New backfill/archive/dataset schemas | 1.0 |
| SQLite persistent store | 1.1 |

## AM. Dependencies

Zero runtime dependencies remain. Python 3.11+ and standard-library SQLite/filesystem/
HTTP/concurrency primitives are used. Existing local development tools were reused;
no sibling dependency installation or source edits occurred.

## AN. Remaining limitations

SEC current metadata cannot establish exhaustive correction/vintage history; reconstructed
source timing is explicitly assumption-qualified. All referenced SEC metadata pages can
be re-requested across date partitions. Generic licensed/macro history requires audited
supplied vintages; no general collector is promised. Single-fragment interchange excludes
multi-source adjudicated records. Query/validation can materialize one build. Live session
caches remain materialized. Reuse excludes gapped/split roots and requires retained inputs.
Metadata-only inputs cannot raw-rebuild. Archive GC/compression/encryption/cloud stores,
automatic repair, distributed workers and build-diff tooling remain absent. Custom sources
must honor bounded cancellation. No live SEC request or production-scale benchmark was
performed in this offline phase.

## AO. Exact proposed EIE-8 scope

Event Studies + Event Influence/Decay: market-data alignment around events; event windows;
pre/post returns; realized volatility; volume/liquidity response; supplied options reaction;
sector/index reaction; prior-only descriptive response statistics; empirical influence/
decay curves; contamination flags; regime/context conditioning foundations. Preserve
point-in-time dataset/gap/provenance boundaries and make no causal-proof claims. No EIE-8
functionality, counterfactual model, trading signal or execution feature is implemented here.
