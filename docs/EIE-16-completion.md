# EIE-16 completion report

## A. Architecture

A separate descriptive consumer projects retained canonical, raw and EIE-11 claim
revisions into compact source frames. Indexed strict-prior collection, component
calculation and immutable persistence share one transaction. Source precedence,
canonical truth, content uncertainty and market impact retain their existing owners.

## B. Package structure

Sixteen modules in `src/eie/reliability`: schema, models, clocks, inputs, bitemporal,
latency, corrections, coverage, consistency, reliability, aggregation, queries,
repository, validation, engine and package declaration. Eight test modules, an offline
example with fixtures and a profile accompany the package.

## C. Source-reliability schema

Source-reliability schema is **1.0**. Frozen allowlisted typed JSON artifacts retain
SHA-256 identities and exact references. All prior public schemas remain unchanged.

## D. Clock model

VALID_TIME, PUBLICATION_TIME, SOURCE_AVAILABLE_TIME, OBSERVED_TIME, INGESTED_TIME,
NORMALIZED_TIME and SNAPSHOT_AVAILABLE_TIME remain separate. Generic query cutoff,
event knowledge and ingestion never substitute for missing normalization/snapshot clocks.
EIE-12 accepted extracted-claim processing availability supplies normalized-field
availability, including extraction and verification. Other producers can supply explicit
trusted source-pinned timing instrumentation.

## E. Bitemporal audit

`BitemporalAuditResult` pins subject, valid-at, known-as-of, exact revision timeline,
selected per-source applicable revisions and unknown-validity members. Later clock
instrumentation appends a frame without delaying original source visibility or leaking
future clocks. Reconstructed source knowledge and actual local acquisition remain separate.

## F. Latency chain

`InformationLatency` retains clock evidence and nine signed components, including
publication/availability to observation, transport, normalized-field processing,
downstream snapshot and end-to-end gaps. Advance publication before validity can have
a legitimate signed valid→publication gap. Impossible pipeline gaps are qualified.

## G. Missing/coarse clock handling

Missing clocks remain missing, never zero. Exact UTC evidence yields integer gaps.
Date/session evidence requires an explicit IANA source timezone for conservative day
intervals, including daylight-saving boundaries; absent timezone yields no interval and
MISSING_TIMEZONE. Coarse evidence supplies no second-level point estimate. Session labels
do not imply a within-day time.

## H. Correction behavior

Explicit EIE-11 CORRECTED and canonical CORRECTION actions count ERROR_CORRECTION.
Value differences never infer an error. Rates retain count and the period's unique
source-revision sample denominator. Field-specific revisions remain distinct samples.

## I. Retraction behavior

RETRACTED/WITHDRAWN assertions count RETRACTION separately from correction. Retracted
current peers cannot confirm another source. A cancellation remains an operational
update rather than proof of retracted content.

## J. Expected revisions

Explicit macro REVISION and revised-previous numeric semantics track EXPECTED_REVISION.
Rescheduling tracks SCHEDULE_CHANGE. Updates and supersessions remain independent.
Explicit correction actions retain precedence over expected-revision classification.

## K. Source consistency

Independent rates retain self-revision frequency, same-field value stability, required
clock completeness, publication timestamp stability and identifier stability.
Immediate predecessors can be pinned outside a rolling sample for comparison; they do
not automatically enter the period sample. Changes are descriptive diagnostics.

## L. Source timeliness

Exact sample distributions retain count, missing/coarse/skew counts, median, mean,
configured linear quantiles, MAD and min/max. No tail trimming or clock correction is
performed. Invalid pipeline gaps remain in latency artifacts and explicit skew counts,
while coarse/invalid gaps do not enter exact distributions.

## M. Source coverage

`CoverageUniverse` pins the explicit expected slots, source/resource/scope/interval,
knowledge and denominator provenance. Observed, known empty, missing, unknown and not
applicable remain distinct. Observed assessments pin matching available source rows.
Ratio is observed / applicable expected slots; unknown slots remain in the denominator.
Absent or empty applicable denominator yields no ratio. The 100-slot regression preserves
95 observed / 3 missing / 2 unknown and ratio 0.95.

## N. Corroboration/conflict behavior

Exact EIE-11 semantic claim IDs and source families define peers. Latest visible source
threads include corrections that rebound to a different claim, retiring old confirmation.
Syndicated family peers are never independent. Compact compatibility reuses numeric/unit
semantics, time refinement and exclusion of audit-only metadata. Unknown time cannot
confirm or contradict an exact fact. Disagreement establishes no objective ground truth.

## O. Field/event-type reliability

Queries explicitly name dataset, provider, resource and subject kind. Optional exact
event-type, predicate and entity/scope filters remain independent. Omitting a filter means
all retained rows in the named resource, not an automatic entire-provider grade.
Claims, canonical revisions and raw deliveries remain separate sample universes.

## P. Rolling windows

ALL_PRIOR, TRAILING_DAYS and TRAILING_OBSERVATIONS require explicit policies. Observation
windows include the complete timestamp tie at their boundary, or fail the bound. Later
instrumentation retains original source sample time and never inflates revision counts.
History and comparison expose saved cutoff results and component deltas across periods.

## Q. Source-class separation

Source class stays categorical metadata. The example retains OFFICIAL_PRIMARY for both
A/B despite different delays and correction counts. No master reliability score, source
ranking, automatic precedence change or trading signal is implemented.

## R. Reliability uncertainty

Independent LOW_SAMPLE, INCOMPLETE_UNIVERSE, SOURCE_FAMILY_AMBIGUITY,
CONFLICT_GROUND_TRUTH_UNKNOWN, RECONSTRUCTION_LIMITATION and audit UNKNOWN_VALIDITY
reasons retain their own meaning. EIE-13 uncertainty and extraction confidence are unchanged.

## S. Latency uncertainty

Specific missing clock names, COARSE_TIMESTAMP, MISSING_TIMEZONE, CLOCK_SKEW,
NEGATIVE_LATENCY and RECONSTRUCTED_AVAILABILITY qualify measurements. No uncertainty
is replaced by an opaque scalar or market-impact statistic.

## T. Prior-only semantics

Source samples and diagnostic frames must be strictly prior to a reliability cutoff.
Audits admit frames available at the cutoff. Exact source availability and later
instrumentation availability remain independent. Real EIE-7 acquisition tests prove
backfill cannot enter an earlier local baseline. Future corrections, rebindings and clock
frames leave saved historical bytes unchanged.

## U. Policy versioning

Grouping, explicit period, minimum count, quantiles, required clocks, correction/conflict
definitions, coverage-universe identity and bounds participate in result identity.
Changed policy/version produces a new result. Stored source versions and exact timing
fingerprints retain lineage; no owner history is overwritten.

## V. Persistence/migrations

Additive transactional migration **11** advances SQLite **1.9→2.0** with five immutable
tables: source rows/frames, artifacts, request mappings, coverage assessments and seals.
Indices cover provider/resource/family/type/field/scope, publication/observation/source/
diagnostic time, source revision, cutoff and policy. All earlier migration DDL is unchanged.
Restart, rollback, migration failure, corruption and concurrent future ingestion are tested.

## W. Query APIs

`source_reliability`, `latency`, `audit`, `coverage`, `history`, `compare` and bounded
atomic `run` expose typed artifacts. `index_claim`, `index_event`, `index_observation`,
`append_universe` and `append_assessment` establish the explicit retained input universe.
Normal queries use compact indices and never deeply reconstruct every source observation.

## X. Validation

Compact validation recalculates source sample selection, components, latency, coverage,
revision timelines, quality and fingerprints from pinned rows. Deep validation additionally
rechecks retained source/acquisition/timing owners and exact historical query membership.
Source/clock/denominator contradictions, count/byte bounds and corrupt indices fail closed.

## Y. Performance/storage

The profile uses 1,000 original observations, three concrete sources, corrections,
retractions, explicit coverage slots and three rolling queries. Each query selects 250
source revisions despite multiple instrumentation frames. Source ingestion/indexing and
deep validation remain more expensive than compact cached reads. Detailed timings and
storage are in `benchmarks/EIE-16-profile.json`: 2,078 compact frames, 1,044 artifacts,
187,465 result bytes and 36,802,560 SQLite bytes. No CI timing threshold is imposed.

## Z. Security

Closed typed JSON, static imports, parameterized queries and validated integer cutoff
composition are reused. No eval/exec/pickle, network, source-triggered filesystem action,
external model/tool call or trading action is introduced. Real inert fake system/tool/
shell/URL/credential text reaches the source fixture and stays out of diagnostics.

## AA. Documentation

Four new documents explain two-time audit, source diagnostics, information latency and
coverage. README, master boundary, architecture, replay, source policy, claim/evidence,
evidence replay, extraction, uncertainty and persistence are updated. All **226 local
links across 15 changed/new documents** resolve, and all **39 A–AM sections** are present.
The offline example uses the existing EIE-11 owner and verifies correction replay and restart bytes.

## AB. Tests added

**83 cases across eight modules** cover exact/missing/coarse/timezone/DST clocks,
bitemporal validity, original versus later instrumentation, expected revisions,
corrections/retractions/rebindings, duplicate frames, syndication, separate classes,
field/type grouping, strict-prior rolling/ties, explicit coverage, real EIE-7 lineage,
EIE-12 processing availability, policy/count/byte bounds, inert source text, migration,
corruption, hash seeds, insertion order, rollback, concurrency and the README example.

## AC. Full pytest result

Final full pytest passed **3,041 tests in 172.29 s**. The starting socket-blocked baseline
passed **2,958 tests in 148.00 s**, with zero network connection attempts. Focused
EIE-16 tests passed **83 cases in 3.64 s**.

## AD. Coverage

The full branch-enabled socket-blocked run passed **3,041 tests in 452.90 s** with
**zero network connection attempts**. Combined statement/branch coverage is
**95.893%**, above the required 95% gate: 21,857 / 22,475 statements and
7,098 / 7,720 branches covered. The new reliability package has **94.220% combined
coverage**. Both the plain full suite and the guarded full suite passed.

## AE. Ruff

Ruff lint passes. Ruff format check passes: **500 files already formatted**.

## AF. Mypy

Strict mypy passes for **295 source files**, with no errors.

## AG. Git diff check

Whitespace checks pass. Full new and tracked source/test/example/profile/documentation
changes are reviewed. Earlier source changes are additive codec/migration registration
and current engine version; prior tests change only current version expectations.

## AH. Secret/unfinished-marker scan

The tracked/new scan checked **525 files**: no secret patterns, trailing whitespace or new
unfinished markers. The two pre-existing marker references remain at
`docs/EIE-5-completion.md:51` and `docs/EIE_MASTER_SPEC.md:1618`.

## AI. Benchmark/profile

The final 1,000-observation profile completed with three providers, 20 corrections,
19 retractions, three 250-sample rolling results and byte-identical restart.
Measured seconds: ingestion/indexing **41.0875**, indexed collection **1.6037**,
prior filtering **0.0746**, latency extraction **0.3658**, aggregation **0.6808**,
coverage **0.0008**, calculation/persistence **2.3162**, compact validation **2.0741**,
deep validation **9.6443**, cached queries **0.0248**, restart/query **0.0452**.
The profiled run took **57.4190 s**. These are local measurements under profiling and
concurrent verification, not production performance guarantees. Exact measurements are
retained in `benchmarks/EIE-16-profile.json`.

## AJ. Engine/schema/store versions

Engine **1.7.0**, source-reliability schema **1.0**, SQLite **2.0**. All prior public
schemas retain **1.0**.

## AK. Dependencies

Zero runtime dependencies remain. Python 3.11+ standard library and existing contracts
are reused. Sibling repositories were not modified or imported; existing external
development-tool executables supplied verification tooling only.

## AL. Remaining limitations

The diagnostic universe requires explicit retained owner indexing; absence does not prove
no events or global completeness. Coverage denominators/negative assessments and external
instrumentation are trusted supplied evidence contracts, not independently established
external truth. EIE-12 normalization availability includes extraction/verification,
not isolated CPU-stage completion. Coarse sessions supply only a day interval with explicit
timezone. Corroboration requires exact EIE-11 claim/family identity and does not infer new
aliases or truth. Empirical rates describe retained claim-field revisions in an explicit
period; comparisons do not discover regime changes or rank providers. The measured workload
is 1,000 original observations, not a claim of production-scale performance. Deep owner
validation and ingestion remain relatively expensive. No final service is added.

## AM. Exact proposed EIE-17 scope

**Final Snapshot, Service, Integration & Hardening**: final typed EIE snapshot and
provider-neutral integration boundaries; explicit service/API and batch queries;
health/readiness, final replay guarantees, cross-package end-to-end validation,
operational metrics/observability, security/persistence/performance hardening, final
documentation and compatibility guarantees; separate evaluation of operational correctness,
information quality and market-impact usefulness. No trading execution. This is proposed
scope only; EIE-17 is not implemented.

No commit or push was performed. Work stops after EIE-16.
