# EIE-8 completion report

Completed 2026-10-02 at engine **0.9.0**. Initial working tree was clean at
`83ff2e0` (`Add historical backfill and dataset construction`). The full master
specification, EIE-8 phase requirements, EIE-7 report, foundation package contracts,
market-data boundary and relevant persistence/replay tests were inspected. All 898
baseline tests passed before changes. No sibling repository source/project edits,
commit, push, live market requests or EIE-9 implementation occurred.

## A. Architecture

Finalized EIE-7 build + explicit revision cutoff → supplied indexed market reader →
explicit anchor/session/window alignment → descriptive raw measurements and contamination
→ immutable indexed study storage → strict-prior distributions and influence/decay.
Exact bounded inputs are retained for pure recalculation and audit.

## B. Package structure

New `studies` package contains immutable `models`/`market` contracts, `checks`, `windows`,
`alignment`, `returns`, `volatility`, `liquidity`, `options`, `contamination`, `aggregation`,
`decay`, `numerics`, `engine`, `repository`, `queries` and `validation`. Benchmark comparison
uses the engine's same measurement pipeline, ensuring matching calculation semantics.

## C. Event-study schema

Study schema **1.0**. Existing domain/config/observation, runtime/ingestion, durable-session
and historical/backfill/archive/dataset schemas remain unchanged at **1.0**.

## D. Market-data contracts

Immutable supplied bars, quotes, options snapshots, sessions, gaps and split boundaries.
Generic instrument/category, source, dataset ID/hash/version, explicit timeframe,
adjustment and price/total-return semantics. Finite OHLC/numerical checks, nonnegative
volume/sizes, causal availability, ordered nonoverlapping bars and unique snapshots.
Indexed offline provider; external historical readers implement the same typed protocol.
No market-data fetcher or vendor dependency.

## E. Event anchors

Publication, linked SEC acceptance, exact scheduled time, reconstructed source availability
or actual EIE acquisition availability. Exact anchors are verified against selected
revision/evidence. Missing timestamps fail; coarse anchors cannot carry exact timestamps.
Native SEC acceptance with source reconstruction qualification is tested.

## F. Event windows

Deterministic window identity includes semantics, pre/post durations, explicit timeframe,
session clock, offsets and coarse proxy policy. Responses preserve target and actual
observation times. Windows must finish by the explicit market cutoff.

## G. Market-session handling

Elapsed UTC or supplied trading-session durations. Trading offsets skip only declared
closures; insufficient calendar coverage fails. Future calendar metadata is excluded.
BMO/AMC/date-only proxies require explicit session endpoints and retain coarse quality.
Market-closed and partial-session conditions are flagged; no calendar is fabricated.

## H. Alignment

Latest completed or first beginning policy, explicit lag strictly below one bar interval.
Pre bars complete/are available at the anchor; event-spanning bars are excluded. Post
bars start at/after anchor. Reference is pre close, first post open or first aligned post
close. Post reference cannot create pre/zero-offset response values. No interpolation.

## I. Return calculations

Simple raw ratio or log-price difference, explicit reference-based cumulative curve,
separate pre-window open-to-close return and post reference-to-close return. Missing
endpoints, invalid references and nonfinite outputs remain unavailable. Return/adjustment
semantics cannot silently mix. Unadjusted series carry action risk; known split windows
suppress price/volatility values.

## J. Realized volatility

Unannualized square-root sum of squared adjacent eligible log returns, stable `hypot`
reduction, pre/post counts, difference and guarded ratio. Active data gaps are not bridged;
supplied closures can be bridged. Minimum observations are explicit. No annualization or
predictive model is claimed.

## K. Volume response

Raw and cumulative window volume; median prior matching session-offset/daily baseline,
count and relative volume. Baseline completion and availability strictly precede anchor;
matching observations also precede the target bar. No future days or fabricated intraday
calendar buckets. Missing/zero baselines and cumulative overflow remain explicit.

## L. Liquidity response

Optional supplied pre/post spread, post relative spread and quoted depth, midpoint return.
Pre quote availability is causal; tolerance and zero/overflow handling are explicit.
No order-flow imbalance, microstructure model or execution functionality.

## M. Options response

Optional matched expiry/tenor/bucket IV/skew and supplied volume/OI changes. Prior surface
state must be available at the anchor. Missing fields stay unavailable. No pricing engine.

## N. Sector/index response

Supplied market, sector, industry or ETF-proxy raw curves, volatility and descriptive
asset-minus-benchmark endpoint difference. Missing benchmark preserves the asset study.
Return type, adjustment, timeframe and session bounds must be compatible. Provider identity
drift during asset or benchmark retrieval fails. These differences are not abnormal returns.

## O. EventStudyResult

Frozen definition, bounded exact market/benchmark inputs, anchor/window/reference, return
curve, pre/post metrics, volume/liquidity/options/benchmark response, contamination,
quality and reconstructed completed-input availability. Typed JSON round trips are stable.

## P. Study identity

Domain-separated SHA-256 over selected canonical state/fact, historical build/content,
anchor, window, market identity, policy, conditioning, cutoffs and code/schema versions.
Separate full result fingerprint includes exact retained observations and all outputs.
Changed content under the same declared identity is an immutable collision.

## Q. Quality flags

Missing pre/post data, coarse time, market closure, partial session, missing benchmark/
quotes/options, contamination, gaps, low counts, incomplete event history, unadjusted
corporate-action risk, numeric unavailability and absent volume baseline. No confidence
score. Optional-data absence does not prevent core asset studies.

## R. Dataset provenance

One finalized event build/content fingerprint plus selected fact ID, revision and
reconstruction quality. Market dataset/source/version and exact bounded input bytes.
Semantic event gaps propagate. Actual historical acquisition clocks remain in EIE-7;
study availability is a reconstructed evidence/replay cutoff, not historical EIE execution.

## S. Contamination

Indexed same-entity, macro/central-bank, market-wide, co-occurrence, source-bundle and
scheduled-overlap flags, with event/revision IDs, timestamp basis and availability.
Selection is fixed to the chosen historical cutoff. Unknown event times are disclosed.
Order invariance is tested. No reaction attribution or interaction model.

## T. Historical aggregation

Strict-prior event, anchor and complete-study cutoffs; explicit current-event exclusion.
Compatible build/window/reference/method/timeframe/category/adjustment/return cohorts.
Multiple samples for one event fail unless explicitly selected before aggregation.
Raw values/IDs, count, mean, median, type-7 linear quantiles, unscaled MAD, median absolute
response, sign frequencies and median volatility change. Explicit minimum samples;
no outlier removal. Validation recomputes descriptors/selection from supplied history.

## U. Surprise/context conditioning

Existing typed EIE-5 normalized surprise, measurement identity and explicit numeric
boundaries; equality enters the right bucket. Canonical event binding and anchor cutoff
are enforced. External context labels carry dimension/value/source/version/observation/
availability; no future label is admitted. No regime model or external-engine coupling.

## V. Influence curves

Absolute sampled raw-return magnitude with available/missing offsets and counts.
Input study quality and result fingerprints remain attached. No causal claim.

## W. Decay estimation

Single-event and event-class scopes are distinct. Earliest peak and first sampled
fraction-of-peak crossing, default one-half, measured from peak. Three available points
required; incomplete, zero or uncrossed responses receive reasons. Class curves use
strict-prior compatible medians with per-offset minimum samples and quality propagation.
No forced fit, interpolation or sustained-normalization claim.

## X. Persistence/migrations

Ordered migration 3 raises store **1.1 → 1.2**: separate definitions/results/run tables,
lookup indexes and immutable update/delete triggers. Atomic append/run, idempotence,
restart, damaged-index refusal and failed-DDL rollback are tested. Appends verify the
selected historical fact/revision/quality and manifest. Existing 1.1 manifests survive
migration with original fingerprints; original live/session tables remain intact.

## Y. Study queries

Python APIs for single/batch studies, indexed strict-prior views, response curves,
distributions, single/class decay and immutable run lookup. Filters cover event type,
family, issuer, anchor range, surprise, reconstruction quality and contamination.
No final API/service or additional CLI is introduced.

## Z. Replay/determinism

Identical inputs produce byte-identical typed outputs and fingerprints after restart.
Original revisions remain associated with original studies. Future bars do not affect
pre metrics; future studies/current events do not enter earlier distributions. Same-time
ordering cannot remove contamination. Pure validation recalculates retained inputs;
build-aware validation additionally verifies event, fact, anchor and contamination history.

## AA. Numerical safeguards

Finite contracts, guarded zero/log references, log differences, `hypot`, stable median
midpoints, finite reductions, deterministic linear interpolation, guarded ratios/depth
and missing optional fields. Input observations/metadata are capped at 100,000 per series,
response offsets at 1,000 and persisted result bytes at 20 MB. No unsafe float serialization.

## AB. Security

Existing static allowlisted typed JSON extended with study contracts. No pickle, eval,
dynamic imports from provider strings, shell execution, credential handling or network
side effects. SQL query values are parameterized; filter columns are fixed. Provider data
cannot control storage paths or mutable historical namespaces. Inputs are bounded and
completed artifacts immutable. Dataset fingerprints are provenance claims, not signatures.

## AC. Documentation

Added [event studies](event-studies.md), [alignment](market-data-alignment.md),
[influence/decay](event-influence-decay.md) and this report. Updated README, master spec,
architecture, replay/causality, dataset manifests, data completeness, expectations,
corporate events and persistence. Offline runnable example and local profile supplied.

## AD. Tests added

**166 new deterministic offline cases**, across seven suites and shared study helpers.
All **898 existing cases** pass; five existing test files update version expectations only.
Coverage includes market contracts/timeframes/adjustments, anchors/coarse session policies,
completed/spanning/missing bars, raw/log returns, volatility, prior volume, quote/options
state, sector/market comparisons, contamination/order invariants, prior aggregation,
surprise/context, sampled decay, persistence/restart/corruption/migration and determinism.

## AE. Full pytest result

Full pytest: **1,064 passed in 7.59 s**. Branch-instrumented full pytest:
**1,064 passed in 20.65 s**. Socket-blocked full pytest: **1,064 passed in 7.43 s**,
**zero network connection attempts**. All sources use supplied synthetic data or fake
transports, including native SEC history fixtures.

## AF. Coverage

Combined statement/branch overall coverage **96.58%** (10,735 statements, 3,738 branches).
New `studies` group **97.08%** (1,389 statements, 460 branches). The 95% configured gate
passes; the denominator combines statements and branches. No new exclusions were added.

## AG. Ruff

Repository lint and format checks pass. Final source, tests, examples and benchmark are
formatted; no unfinished implementation is suppressed.

## AH. Mypy

Strict mypy passes **155 source files**, zero issues.

## AI. Diff check/review

Full modified/new source, tests, docs and final status reviewed. `git diff --check` and
new-file whitespace scan pass. All EIE-8 changes remain unstaged/uncommitted at `83ff2e0`.

## AJ. Secret and unfinished-marker scan

Credential/private-key patterns find no secrets. No actionable unfinished markers remain.
Existing documentation-only references to review gates are not implementation debt.

## AK. Benchmark/profile

Offline synthetic 10,000-event workload: 30,020 supplied one-minute bars per instrument,
asset and sector series, two alternating pre/post windows, 5,000 compatible strict-prior
samples. One supplied continuous synthetic session; no exchange timing claim. Volume
baseline is disabled for this throughput workload; optional quotes/options are absent.
Fixture/history construction is outside measured study stages. No CI timing threshold.

| Measurement | Result |
|---|---:|
| Alignment | 0.0476 s / 210,248 windows per second |
| Study generation with sector comparisons | 6.3032 s / 1,586.49 studies per second |
| Atomic run persistence | 62.6194 s |
| Prior-only distribution | 0.6009 s / 5,000 samples |
| Individual + class decay | 4.1509 s |
| Indexed one-event query | 0.00223 s |
| SQLite bytes including event history and WAL at sample | 567,924,104 |
| Stored output fingerprint verification | Identical |

Run `PYTHONPATH=src:. python benchmarks/event_studies.py`. Results are local observations,
not production throughput promises. The profile also reloads and verifies every run member.

## AL. Engine/schema/store versions

Engine/package **0.9.0**; event-study schema **1.0**; store **1.2**. All existing
quantitative/runtime/historical schemas and durable-session layout remain **1.0**.

## AM. Dependencies

**Zero runtime dependencies.** Python 3.11+, standard-library math/SQLite/serialization.
Existing development tools reused read-only; no sibling dependency installation or edits.

## AN. Remaining limitations

Supplied dataset/source hashes and adjustment claims require audited inputs; this phase
does not verify a vendor's historical completeness or license. There is no market fetcher,
calendar model, automatic instrument association or warehouse. Calendar and corporate-action
completeness cannot be inferred from absence. SEC vintages remain assumption-qualified.
Benchmark clocks must align with the asset. Exact studies exclude coarse sources; coarse
endpoint studies are proxies. Volume normalization needs supplied matching buckets;
annualization is omitted. Baseline/window retrieval and aggregate queries materialize
bounded slices/selected cohorts; run construction materializes requested results. Large
runs retain repeated exact inputs and therefore use substantial storage. No alignment cache,
raw-input deduplication, distributed scheduler, exponential fit, interpolation, trimming,
causal/counterfactual model or collision model is added. Standalone result validation
cannot prove contamination completeness without its event build. Earlier engine-pinned
archive rebuilds still need matching code or explicit new-version reprocessing.

## AO. Exact proposed EIE-9 scope

Abnormal Return + Counterfactual Impact Estimation: explicit prior estimation windows;
market-model expected returns; sector-adjusted and supplied factor residuals; matched
controls where defensible; abnormal and cumulative abnormal returns; justified volatility/
liquidity counterfactuals; uncertainty bands, fit/model diagnostics and contamination
sensitivity. Preserve all EIE-8 raw observations and provenance. No causal-proof claims,
trading logic or expansion into later graph/NLP/analogue/service phases. **EIE-9 remains
unimplemented; stop after EIE-8.**
