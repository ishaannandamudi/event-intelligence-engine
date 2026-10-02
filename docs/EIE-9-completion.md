# EIE-9 completion report

Completed 2026-10-02 at engine **1.0.0**. The initial working tree was clean at
`4eebe82` (`Add event studies and influence decay`). The full master specification,
EIE-9 requirements, EIE-8 report and foundation/study/market/persistence contracts were
inspected. All **1,064 baseline tests** passed before changes. EIE-9 changes remain
uncommitted in this working tree. No sibling repository edits, commit or push occurred.

## A. Architecture

Validated EIE-8 raw study + supplied prior target/benchmark/factor/control history →
causal input projection and explicit model specification → prior paired-period OLS or
deterministic control matching → expected/abnormal period returns, independent CAR
windows and uncertainty → immutable model/result/set/run storage and recalculation.
Information surprise, raw response and counterfactual residuals remain separate.
Estimates do not establish causal proof or select a model from event-window performance.

## B. Package structure

The new `eie.impact` package contains 15 modules: `__init__`, `inputs`, `models`,
`estimation`, `regression`, `fitting`, `controls`, `abnormal`, `uncertainty`,
`diagnostics`, `sensitivity`, `engine`, `queries`, `repository` and `validation`.
Shared regression/fitting code implements the small model families without duplicating
window, provenance or numerical checks.

## C. Impact schema

Impact schema **1.0**. Frozen typed contracts retain exact bounded inputs, model policies,
specialized fit payloads, period results, CAR, uncertainty, metrics, quality, comparisons
and run manifests. All prior public schemas remain **1.0**.

## D. Model specification

`CounterfactualModelSpec` declares one of five model types, estimation/event/CAR windows,
return semantics, event build/content, raw study/input fingerprints, dataset pins,
benchmark/control/factor universe, fit method/cutoff/minimum count, intercept,
contamination, matching, uncertainty and conditioning policies, and model/code/schema
versions. Domain-separated SHA-256 identities include these retained inputs and policies.
Changed retained input/specification creates a new identity; semantic IDs are not random.

## E. Estimation windows

Explicit negative offsets and exclusion gap reuse elapsed UTC or EIE-8 supplied trading
session clocks. The estimation end must precede the event window by the declared gap.
Every fitted period, observation availability and calendar availability strictly precedes
the fit cutoff/event anchor. Estimation history can extend beyond the raw study slice.
Pairs require identical period start/end; missing pairs are counted and dropped without
filling. Overlap, insufficient calendar coverage and UTC underflow fail explicitly.

## F. Market-adjusted model

Expected return equals supplied market benchmark return; abnormal return equals target
minus benchmark. The unit-coefficient baseline applies no fitted intercept. Prior paired
history is retained as a coverage diagnostic; no regression uncertainty is invented.
Missing benchmark periods preserve observed target returns and unavailable residuals.

## G. Sector-adjusted model

The same explicit unit-coefficient calculation uses a caller-declared sector benchmark.
Timeframe, adjustment, price/total-return type, simple/log method, supplied calendar basis
and exact paired periods must agree. EIE does not infer sector membership. Incompatible
metadata is rejected; missing sector input is recorded as unavailable.

## H. Market model

Unweighted prior OLS estimates `Rtarget = alpha + beta * Rmarket + residual`, with an
explicit intercept policy (default enabled) and default minimum 30 paired observations.
The fit retains coefficients, residuals, residual variance, R², coefficient SE/covariance,
degrees of freedom and conditioning. Insufficient, constant, singular or nonfinite fits
remain unavailable with typed reasons; there is no fallback benchmark selection.

## I. Factor model

`FactorSeries` retains factor ID, source, dataset/hash/version, compatible semantics,
exact period values and availability. Up to eight factors use the same prior small QR
OLS, with a minimum of the configured count and five times parameter count. Historical
availability, missing factor pairs and singular/near-singular designs are checked.
Daily factors cannot silently enter an intraday fit. No factor retrieval or implicit
risk-free/excess-return conversion is added.

## J. Matched controls

Prior feature snapshots and eligible prior market periods determine selection. Target,
missing/incompatible data, missing/mismatched features, configured contamination and
unresolved unadjusted action risk have retained exclusion reasons. Eligible prior feature
population standard deviations, with an explicit scale floor, define standardized
Euclidean distance. Top K sorts by distance then instrument ID. Equal and inverse-distance
weights are explicit; exact zero-distance matches share all weight. Features, matching
time, scales/population, distances, weights and exclusions are retained. Missing post-event
returns do not trigger rematching. Bounds are 1,000 candidates, 16 features and K ≤ 100.

## K. Abnormal return

`AbnormalReturnPoint` retains exact adjacent close-to-close period, offset, observed,
expected, abnormal, model ID, source hashes, quality, design and justified intervals.
The raw EIE-8 reference-based cumulative curve remains unchanged. Complete compatible
periods are required; event-spanning bars, active gaps and known unadjusted splits cannot
produce fabricated residuals. Declared session closures are the only permitted bridges.
Matched controls also support comparable-window realized volatility and quoted-spread
expectations as weighted control metrics when every selected member has the measurement.

## L. Cumulative abnormal return

Each `CARWindow` is evaluated independently as `SUM_PERIOD_ABNORMAL_RETURNS`. Simple
returns produce an arithmetic event-study CAR; log returns produce accumulated log
residuals. There is no implicit compounding or simple/log conversion. Complete period
chains must cover the declared boundaries. Missing periods, boundaries inside periods
or unavailable residuals make CAR unavailable. Overlapping windows never add their
cumulative results together. At most 100 sorted unique windows are retained per model.

## M. Uncertainty

Explicit confidence level (default 95%) uses a deterministic normal OLS approximation.
Expected variance is `x' V x`; abnormal prediction variance is `s² + x' V x`. CAR variance
is `N*s² + (sum x)' V (sum x)`, retaining shared coefficient covariance. Assumptions are
fixed observed regressors, homoskedastic residuals, independent event residuals for CAR
and sufficiently large samples. Default uncertainty minimum is 30 even if the fit minimum
is lower. Small samples or invalid calculations yield unavailable intervals. Adjusted and
control baselines receive no invented interval. No Student-t, robust SE, bootstrap or
p-value calculation is claimed.

## N. Diagnostics

Categorical quality and specialized fit payloads expose insufficient history, missing
pairs, low R², residual scale, degeneracy/singularity/conditioning, missing event data,
poor/fewer-than-K matches, control/target contamination, gaps, coarse time, reconstruction
limits, actions, excluded fit contamination and numerical/uncertainty unavailability.
Compatibility errors are hard rejections. There is no opaque confidence or materiality score.

## O. Contamination sensitivity

EIE-8 target contamination propagates unchanged. `INCLUDE` retains estimates with flags;
`EXCLUDE_EVENT` retains observations while suppressing expected/abnormal/CAR/secondary
estimates. `contamination_sensitivity` recalculates both variants. Explicit prior-known
`FitExclusion` intervals can remove contaminated fitting pairs and record their count.
Supplied control events are filtered by historical selection cutoff/event span and
excluded or flagged under the declared matching policy. Event completeness is not inferred.

## P. Multi-model impact set

`EventImpactSet` retains 2–100 distinct declared results of the same raw study and
compatible return semantics. `SensitivityResult` records both IDs/values and right-minus-left
CAR differences for equal declared windows, with unavailable/comparison reasons.
Alternative benchmarks, estimation windows, model versions and contamination policies
remain visible. No estimate is collapsed into a single true impact or ranked after the event.

## Q. Dataset provenance

Pinned historical build/content and raw study include selected event fact/revision and
reconstruction qualification. Dataset pins cover exact target, benchmark, factors,
controls and feature snapshots, with IDs/hashes/versions. Exact projected observations
and input fingerprints permit recalculation. Dataset hashes/adjustment/vintage assertions
remain supplied provenance claims, not independent authentication or completeness proofs.

## R. Replay and causality

Strict prior fitting and feature scaling are enforced independently of event-response
availability. Out-of-cutoff observations and future feature snapshots are projected out.
Repeated calculations, reordered admissible inputs, different hash seeds and database
restart preserve canonical bytes/fingerprints. Future event corrections cannot replace
the frozen historical study/result. As-of queries require both complete-result availability
and anchor strictly before T. Reconstructed availability is an evidence cutoff, not a
claim that EIE performed the computation at that historical time.

## S. Persistence and migrations

Ordered additive migration 4 raises store **1.2 → 1.3**. Indexed `impact_models` stores
spec/fit; `impact_results`, `impact_sets` and `impact_runs` store completed artifacts.
Foreign keys link original studies/builds; update/delete refusal triggers protect all
four tables. Atomic/idempotent append, run and set operations verify historical study
provenance and roll back on failure. Reads verify every scalar index, separate spec/fit,
fingerprint, recalculation and exact raw-study/member references. Previous migration DDL,
manifests, study fingerprints and live/session records remain intact.

## T. Query API

`ImpactEngine` provides typed single/batch calculation; `ImpactQueries` exposes `impact`,
`abnormal_curve`, `car`, `fit_diagnostics`, `sensitivity` and `impacts_as_of`. Indexed
repository filters cover build/event/instrument/model, with ordered strict-prior results.
Run lookup verifies membership/counts. Batch calculation is bounded to 10,000 requests.
This phase adds no REST/service layer or live acquisition entry point.

## U. Validation

Pure `validate_impact` recalculates fits, controls, expected/abnormal curves, CAR,
uncertainty, diagnostics and metadata. Optional `DatasetReader` additionally verifies
the original historical fact/revision and study. Structured statuses are `VALID`,
`VALID_WITH_WARNINGS`, `INVALID`. `validate_set` checks every member and recomputes
comparisons. Corrupt stored indexes, fit/spec/results and set/run references are refused.
Standalone validation cannot prove that supplied universes or event lists are complete.

## V. Numerical safeguards

Finite bounded immutable inputs; centered/scaled twice-orthogonalized QR; triangular
solves and covariance transformation without naive matrix inversion; scaled triangular
1-norm condition limit; exact constant-column detection; singular/tiny denominator,
overflow, nonfinite and nonzero residual-variance underflow refusal. Matching rejects
nonfinite population scales and distances; inverse weights use ratios to avoid reciprocal
overflow. Return/prediction/CAR/interval reductions fail closed. Shared covariance is
retained, and only insignificant roundoff in a quadratic variance can be clamped to zero.

## W. Performance

Matching costs a bounded feature scan/sort plus prior-period eligibility checks. Fits
operate on bounded small factor matrices and exact period maps. At most 200,000 aggregate
input observations and 10,000 fit exclusions are retained per request; serialized impacts
and sets are capped at 30 MB and 50 MB. Run/query paths materialize selected results.
The profile separates full model calculation, fitting, CAR, control matching, validation,
persistence and indexed query cost. Repeated retained inputs and historical revalidation
are measurable storage/time costs; no distributed or deduplicated warehouse is introduced.

## X. Security

Static allowlisted typed JSON extends the existing codec. Unknown stored types are refused.
All supplied market/factor/control data passes typed finite/bounds checks. SQL values are
parameterized and filter columns fixed. No supplied string controls imports, execution,
network requests or storage paths. The impact runtime uses no pickle, eval, shell execution,
credential handling or network side effects. Immutable artifacts and atomic migrations
are tested against corrupted/untrusted inputs.

## Y. Documentation

Added [counterfactual impact](counterfactual-impact.md), [abnormal returns](abnormal-returns.md),
[matched controls](matched-controls.md) and this report. Updated README, master specification,
architecture, event studies, market alignment, replay/causality, dataset manifests,
data completeness and persistence. Added deterministic offline
[example](../examples/counterfactual_impact.py) and [profile](../benchmarks/counterfactual_impact.py).
The example fits alpha ≈ 0.00005003 and beta ≈ 1.500054 from 230 prior pairs, reports
market/factor/sector/control results, 18 CAR comparisons and two contamination variants,
and verifies byte-identical persistence/restart. Market post CAR is approximately 0.009993;
the normal OLS interval is approximately [0.009834, 0.010152]. These are synthetic values.

## Z. Tests added

**95 additional collected cases** across six new suites and shared helpers. Coverage
includes model identities/policies, exact window/gap/cutoff causality, market and sector
compatibility, analytical OLS coefficients/residuals/variance/R²/SE, multifactor/singular
designs, factor availability, control eligibility/scaling/top-K/weights/contamination,
simple/log AR and independent CAR, shared-fit CAR uncertainty, sensitivity/disagreement,
historical corrections, hash-seed/restart determinism, migration preservation/rollback,
atomic persistence, immutability, corrupted metadata/payloads and extreme numerics.
All **1,064 existing tests** remain; existing test changes update version assertions only.

## AA. Full pytest result

Final full suite: **1,159 passed in 25.13 s**. The branch-instrumented, socket-blocked full suite
also passed **1,159 tests in 73.63 s**, with **zero network connection attempts**.
The standalone README example passed, including fingerprint and byte-identical restart
checks. Tests use deterministic supplied data and existing fixture transports.

## AB. Coverage

Combined statement/branch overall coverage **96.62718%**: 11,633/11,914 statements and
3,866/4,126 branches covered. New `impact` package combined coverage **97.05694%**:
1,148/1,175 statements and 369/388 branches. The configured ≥95% combined gate passes.
Branch collection is enabled; no impact coverage exclusions were added.

## AC. Ruff

Repository lint passes. Format check passes **259 Python files**. No new lint suppression.

## AD. Mypy

Strict mypy passes **170 source files**, zero issues.

## AE. Diff check and review

Full modified/new source, tests, examples, benchmark and documentation reviewed.
`git diff --check` and new-file whitespace scan pass. Final status contains only the
EIE-9 additions, integration/version changes and documentation updates. Changes remain
unstaged/uncommitted at `4eebe82`; no EIE-10 implementation was added.

## AF. Secret and unfinished-marker review

Credential/private-key pattern scan over **277 files** found **zero secrets** and no trailing whitespace.
The only two existing marker matches are documentation references to mandatory scan
gates in the EIE-5 report and master specification. No actionable unfinished implementation
marker was introduced. Final scan includes tracked and nonignored new files.

## AG. Benchmark and profile

Offline workload: **1,000 distinct synthetic events**, 230 paired prior daily periods
per model, two CAR windows, supplied generic instruments and one continuous synthetic
calendar. All 1,000 market models ran; 100 sector and 100 matched-control models used a
sample with three candidates and K=2. **100 market results** were persisted/reloaded.
Separate fitting/CAR stages repeat a retained representative model 100 times; validation
checks 100 distinct results against their full frozen historical event build. Fixture
construction is outside these stage timings. There is no CI timing threshold.

| Measurement | Local result |
|---|---:|
| Raw study generation | 0.3556 s |
| 1,000 complete market models | 33.2149 s / 30.11 models per second |
| 100 sector models | 3.2885 s |
| 100 control matching/estimation calculations | 6.6738 s |
| 100 isolated fitting repetitions | 1.2121 s / 82.50 fits per second |
| 100 isolated two-window CAR repetitions | 0.002977 s |
| 100 build-aware validations | 41.2588 s |
| Atomic persistence of 100 market results | 5.3464 s |
| Indexed one-event lookup with result verification | 0.05306 s |
| SQLite bytes including history and WAL at sample | 65,034,272 |
| Every stored run member/fingerprint verified | Identical |

Run `PYTHONPATH=src:. python benchmarks/counterfactual_impact.py --events 1000`.
The runner accepts 1–10,000 events; this report claims the measured 1,000-event run,
with the disclosed bounded storage/model sample. Results are local observations.

## AH. Engine, schema and store versions

Engine/package **1.0.0**; impact schema **1.0**; event-study and every existing public
quantitative/runtime/historical schema **1.0**; SQLite store **1.3**. Prior durable-session
layout and schema remain unchanged. Earlier completed phase reports retain their versions.

## AI. Dependencies

**Zero runtime dependencies.** Python 3.11+ and standard-library math/statistics/SQLite
are sufficient. No new development dependency or sibling environment installation/edit.
Existing development tools were reused read-only.

## AJ. Remaining limitations

Supplied hashes, historical vintages, adjustment histories, calendars and contamination
coverage need audited sources. The engine cannot certify omitted controls/features/events
or infer issuer-to-instrument relationships. Thin/nonsynchronous observations are not
repaired. Baseline OLS does not implement robust/autocorrelation-adjusted SEs, Student-t,
Scholes-Williams/Dimson, bootstrap, p-values or causal identification. Simple CAR is not
compounded buy-and-hold abnormal return. Control secondary metrics are comparable-window
weighted expectations, not basket realized volatility or a general predictive liquidity
model. Large runs repeat retained slices; matching/query/run materialization and repeated
historical validation have bounded but meaningful cost. Replaying older engine-pinned
artifacts requires matching processing code or explicit new-version reprocessing.

## AK. Exact proposed EIE-10 scope

**Entity Relationship & Exposure Graph**: issuer/security/entity relationships;
parent/subsidiary; supplier/customer; competitor; index/ETF membership; sector/industry
linkage; sourced ownership/exposure edges; edge provenance; valid-time and knowledge-time
graph history; traversal; event propagation candidates. No trading signals.
**EIE-10 remains unimplemented. Work stops after EIE-9.** No relationship/evidence graph,
unscheduled NLP, novelty/materiality, analogues, collision model, final service,
MCE/AME integration, portfolio or execution functionality was added.
