# Counterfactual impact estimates — EIE-9

EIE estimates how unusual observed movement was under a **declared expected-return
baseline**. Abnormal return is a model-implied residual associated with the event
window. It is not proof that an event caused a movement. EIE-5 information surprise,
EIE-8 raw market response and EIE-9 abnormal response are separate immutable dimensions.

## Architecture and contracts

`ImpactInputs` retains a validated `EventStudyResult`, supplied target estimation
history, and model-specific benchmark, factors or candidate controls. All inputs are
immutable typed contracts with bounded finite fields. `ImpactEngine.impact_event`
projects them to the study data cutoff, creates `CounterfactualModelSpec` and runs a
pure calculation. `calculate(inputs, spec)` supports an explicit audited specification;
`impact_events` handles up to 10,000 distinct requests. No data is fetched.

The impact package separates contracts (`inputs`, `models`), `estimation`, `regression`,
`fitting`, `controls`, `abnormal`, `uncertainty`, `diagnostics`, `sensitivity`, `engine`,
`queries`, `repository`, and `validation`. Impact schema is **1.0**, engine **1.0.0**,
store **1.3**. Other public schemas remain **1.0**. Python 3.11+, zero runtime dependencies.

## Explicit models

| Declared model | Expected period return |
|---|---|
| `MARKET_ADJUSTED` | Supplied market benchmark return |
| `SECTOR_ADJUSTED` | Supplied sector benchmark return |
| `MARKET_MODEL` | Prior OLS alpha + beta × market return |
| `SUPPLIED_FACTOR_MODEL` | Prior OLS alpha + sum(beta × supplied factor) |
| `MATCHED_CONTROLS` | Explicit weighted mean of selected supplied controls |

Benchmark roles are declarations by the caller; EIE does not infer sector membership,
instrument associations or market universes. Identities are generic, including supplied
index/rates/futures instruments for macro studies. M&A target/acquirer studies remain
separate. There is no optimizer, causal model, trading interpretation or model ranking.

## Estimation, cutoff and provenance

`EstimationWindow(start_offset, end_offset, gap_ns, session_policy)` uses the EIE-8 anchor
and elapsed or supplied trading-session clock. Both offsets must be negative. Its end
must be at/before `-event_pre_duration - gap`. Estimation/event overlap is rejected;
this baseline provides no override. Trading offsets use supplied history sessions
known at the event. UTC underflow or insufficient session coverage fails explicitly.

Every fitted period and its market/factor availability must be **strictly before**
`fit_cutoff_ns <= event_anchor`. The selected window can finish earlier than the cutoff.
Event periods and future bars never enter a fit. Paired periods must have exactly
identical start/end clocks. Missing pairs are dropped and counted; returns are never
forward filled. Default minimum is 30 observations; factor models also require at least
5 × parameter count, with at most eight factors. Intercept is explicit and defaults on.

The model pins build/content hash, selected raw study ID/fingerprint, fact/revision and
reconstruction quality (through the study), input fingerprint, target/benchmark/factor/
control/feature dataset IDs, hashes and versions, universe, windows, all policies,
fitting method, model/code/schema versions. Changing any retained input or policy
produces a new model/result ID. Out-of-cutoff market/factor data and future control
features are projected out. The source's declared dataset hash is an audited assertion;
EIE additionally fingerprints the exact retained inputs.

## Diagnostics and uncertainty

The centered/scaled, reorthogonalized small QR OLS reports count, coefficients, residuals,
residual variance, R², parameter covariance/SEs, degrees of freedom and a condition
diagnostic: the 1-norm condition number of the scaled QR triangular design. It is not
the unscaled design's spectral condition number. Zero/tiny unusable denominators,
singularity, excessive condition, overflow, nonfinite calculations and underflowed
nonzero residual variance fail closed. No silent fallback model is chosen. Failed fits
preserve status, counts and reasons; failed control selection preserves exclusions.

Normal OLS intervals use an explicit confidence level (default 95%) and at least 30
observations even when the fit minimum is reduced. They assume homoskedastic residuals,
fixed observed regressors, and a large-sample normal approximation. They are **not**
Student-t intervals or robust/autocorrelation-adjusted econometric inference. Point
expected-return SE uses parameter covariance; abnormal-return SE adds residual variance.
CAR retains shared fitted-parameter covariance and assumes independent event residuals.
Small samples leave uncertainty unavailable. Adjusted/control baselines have no fitted
residual distribution and therefore no invented intervals or p-values.

## Contamination and disagreement

Raw-study same-time and overlapping-event metadata is retained and flagged as target
contamination. Coverage follows the raw study's declared contamination policy; callers
must configure that policy to cover their intended analysis span. `INCLUDE` reports
estimates with warnings; `EXCLUDE_EVENT` preserves
observed periods and leaves expected/abnormal/CAR/secondary estimates unavailable.
`contamination_sensitivity(result)` explicitly returns both variants. It does not remove
particular return periods and pretend to estimate an uncontaminated event.

Optional supplied `FitExclusion` intervals require prior availability and declared
instrument/event/source. Excluding fit contamination removes overlapping prior paired
periods and records a count. EIE does not invent an event's historical severity or
automatically link issuer events to instruments. Controls have their own supplied
contamination policy and retained flags/exclusions.

`impact_set(results)` retains 2–100 declared model variants of the same raw study, with
pairwise CAR differences where windows/return semantics match. Alternate benchmarks,
estimation windows, fitting/intercept policies and contamination choices remain visible.
Different models may disagree; none is selected from event-window performance.

## Secondary metrics

Matched models can report expected **comparable-window realized log volatility** and
**quoted spread** as weighted means of the selected controls' EIE-8 measurements, with
target observed value, deviation, method and source hashes. Every selected control must
have the corresponding measurement. This is a cross-sectional metric expectation,
not the realized volatility of the weighted return basket. No price-level path, implied
order-flow/depth model or generic prior-period volatility forecast is manufactured.

## Persistence, queries and validation

Migration 4 adds indexed immutable `impact_models` (spec + specialized fit),
`impact_results`, `impact_sets` and `impact_runs`. Prior migrations/records, study IDs,
historical manifests and durable sessions remain intact. Appends are atomic/idempotent;
UPDATE/DELETE are refused. A missing raw study is persisted through EIE-8's historical
fact/build checks inside the same transaction. Runs seal sorted IDs, model/data pins,
result hashes, failure and contamination counts. Sensitivity sets retain exact members.

`ImpactQueries` exposes typed `impact`, `abnormal_curve`, `car`, `fit_diagnostics`,
`sensitivity` and `impacts_as_of`. Completed-result availability and anchor must be
strictly before an as-of query. Reconstructed availability is an evidence cutoff,
not a backdated EIE computation timestamp. There is no service layer or new live CLI.

`validate_impact` recalculates fit, selection, curve, CAR, uncertainty and diagnostics;
optionally it validates the original study against its `DatasetReader` and historical
revision cutoff. Statuses are `VALID`, `VALID_WITH_WARNINGS`, `INVALID`, with structured
reasons. `validate_set` recalculates members and comparisons. Stored reads verify scalar
indexes, separate spec/fit payloads, exact raw study and result fingerprints.

## Limits

Supplied adjustment, calendar, dataset completeness and historical factor/feature vintages
need audited provenance. Missing observations/thin trading are not repaired; nonsynchronous
trading, bid-ask effects, Scholes-Williams/Dimson adjustments, robust SEs, bootstrap and
causal identification are not implemented. Current factors cannot silently stand in for
unavailable historical vintages. Standalone validation cannot prove omitted control
events/features or universal contamination completeness. Changing code requires its
matching version for replay. Large runs retain repeated input slices and can use substantial
storage; matching scans at most 1,000 candidates, 16 features, K ≤ 100, no massive universe.

Run [the example](../examples/counterfactual_impact.py) and
[the profile](../benchmarks/counterfactual_impact.py). See [abnormal returns](abnormal-returns.md)
and [matched controls](matched-controls.md). EIE-10 is described below.

## EIE-10 relationship boundary

Relationships, sourced exposure, graph-derived related-entity candidates and measured
impact are separate artifacts. EIE-10 does not import impact estimates to choose graph
weights, infer supply/ownership, predict direction or rank candidates. A retained path
may identify an entity to evaluate, but does not establish causal attribution of its
abnormal return. Existing EIE-9 schemas and numerical behavior remain unchanged.
See [exposure](exposure-graph.md) and [event propagation](event-propagation.md).

## EIE-13 impact attachments

`IntelligenceRepository.attach_impact` accepts actual retained same-event EIE-9
results with verified IDs/fingerprints and availability. A later result creates a
separate immutable ex-post audit. EIE-9 fit/contamination/data diagnostics populate
only impact uncertainty; original ex-ante novelty, static importance, materiality
and extraction quality remain unchanged. No reaction scalar is fabricated. See
[intelligence replay](intelligence-replay.md).
