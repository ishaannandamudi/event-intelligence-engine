# Market-reaction surprise

Three independent descriptive objects remain separate:

| Object | Comparison |
| --- | --- |
| EIE-5 information surprise | Actual information against prior expectation |
| EIE-8/EIE-9 empirical impact | Observed market response and deviation from an explicit counterfactual model |
| EIE-14 market-reaction surprise | Observed response against an exactly frozen historical analogue distribution |

## Responses and compatibility

A `ResponseSpec` declares RAW_RETURN, ABS_RAW_RETURN, ABNORMAL_RETURN,
ABS_ABNORMAL_RETURN, CAR, REALIZED_VOL_CHANGE, RELATIVE_VOLUME, IV_CHANGE or
SPREAD_CHANGE. Values come from retained real EIE-8/EIE-9 artifacts. Missing supplied
quotes/options/baselines remain unavailable.

Raw returns use the specified curve offset. Abnormal returns use that exact period's
EIE-9 abnormal point. CAR uses an explicit named CAR window with the declared end
offset. Volatility difference, final relative volume, IV change and absolute quoted
spread difference use the full study window, and reject partial-offset requests.
Spread change is `post_spread - pre_spread`, in the study's quote-price units.

The study cohort pins build/dataset, window, timeframe, adjustment, return type,
return method, study policy and schema. Impact cohorts also pin the complete model
configuration, period semantics, calendar source/version and common benchmark/factor
identities. Each observation independently pins its exact study fingerprint,
impact fingerprint and exact per-event model specification ID. Event-specific
input hashes and calendar dates are not treated as common model configuration;
fit coefficients and target outcomes never determine cohort similarity. Market,
sector, factor and control models cannot be blended into one distribution.

## Distribution and comparison

Selected raw values, count, mean, median, raw MAD, type-7 quantiles (0.1, 0.25,
0.5, 0.75, 0.9), sign frequencies, median absolute response and extrema are retained.
Minimum sample defaults to five. Missing history yields unavailable statistics
with retained sample IDs/values. No trimming or winsorization is performed.
Historical mean and median are descriptive comparisons, not forecasts.

Ex-post comparison requires a persisted frozen result and an exact target response
for its target row/revision and response cohort. Availability must be strictly after
both target response completion and its availability, and cannot precede the frozen
result's actual availability. The comparison never retrieves a new analogue set.

Signed percentile uses `(count(less) + 0.5 * count(equal)) / N`, a deterministic
midrank empirical convention in [0,1]. Magnitude percentile applies the same rule to
absolute values and is separately named. Signed residual is `observed - median`.
Robust standardized residual is that residual divided by raw MAD. Zero MAD supplies
no standardized number; no epsilon or Gaussian calibration is introduced.

Absent target response, absent/insufficient distribution and numeric overflow have
explicit states/reasons. A later target response revision can create another
comparison to the same frozen set, without changing members or distances. Information
surprise references remain on the target feature row; counterfactual impact retains
its independent EIE-9 owner. There is no combined score or directional trade label.
