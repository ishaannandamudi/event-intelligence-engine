# Period abnormal returns and CAR

The raw `EventStudyResult.curve` measures movement from the declared EIE-8 reference.
It remains attached unchanged. EIE-9 derives **adjacent close-to-close period returns**
from exact supplied bars with the same timeframe, adjustment, price/total-return type
and simple/log method. It never sums those raw cumulative curve points.

For each adjacent bar pair, the period is `(previous_close_time, current_close_time]`.
The first supplied bar provides a reference only. Full intraday bars are required;
daily bars use their explicit daily frequency (session duration may be shorter).
Adjacent session-close/open intervals can bridge only a supplied declared closure;
gaps during active trading invalidate the period. A period spanning the event anchor
is excluded. Pre-event observations must be available at the anchor; fitting observations
must be available strictly before the fit cutoff. End-of-day factors cannot explain
minute returns. Calendar geometry and exact paired periods must match.

Simple period return is `close_t / close_previous - 1`; log period return is
`log(close_t) - log(close_previous)`. Expected return is the declared benchmark,
regression or control-basket value in those same units. `AbnormalReturnPoint` retains
start/end, offset, observed/expected/abnormal values, model identity, period input hashes,
design and quality flags, plus justified expected/abnormal uncertainty. Missing operands
leave abnormal return unavailable. Known unadjusted splits invalidate crossing periods;
all unadjusted targets retain corporate-action risk. Unresolved unadjusted controls are
excluded. Unknown action completeness cannot be certified by absence of split records.

## Model definitions

Market- and sector-adjusted models use a unit benchmark coefficient with no fit or
intercept applied to event returns. Market OLS uses `alpha + beta * Rmarket`; supplied
factor OLS uses `alpha + sum(beta_k * factor_k)`. The caller supplies factor vintage,
source, dataset/hash/version, exact period and availability; no factor is fetched and
no excess-return/risk-free conversion is assumed. An optional omitted intercept is
declared in the specification. OLS is unweighted. Matched expected return is the
declared weighted mean of selected control period returns.

## Independent cumulative windows

`CARWindow(label, start_offset, end_offset)` declares `(start,end]` using the raw study's
elapsed/trading clock. Common `[0,+1d]`, `[-1d,+1d]`, `[0,+5d]` labels describe the
boundary span; only complete periods contained inside it are summed. Labels are sorted
unique. A boundary inside a period leaves CAR unavailable, as do missing endpoints,
gaps, missing factors/benchmark/control returns or a failed fit. No partial sum masquerades
as a complete CAR. Overlapping windows are evaluated independently, never summed together.

The only baseline aggregation is `SUM_PERIOD_ABNORMAL_RETURNS`:

`CAR = sum(observed_period_return - expected_period_return)`.

For simple returns this is an arithmetic event-study statistic, **not** compounded
buy-and-hold abnormal return. For log returns it is the difference of accumulated log
returns; EIE does not exponentiate it into a simple-return value. The method/type remain
pinned. No simple/log mixing, sparse endpoint summation or implicit compounding occurs.

## Analytical uncertainty

Let `V` be the fitted coefficient covariance and `s²` the OLS residual variance.
Point expected variance is `x' V x`; abnormal prediction variance is `s² + x' V x`.
For N CAR periods, variance is `N*s² + (sum x)' V (sum x)`. The second term preserves
shared coefficient uncertainty; adding N individual point variances would omit this
covariance. Intervals center on the observed abnormal value and use the explicit normal
quantile. Residual independence/homoskedasticity and fixed regressors are assumptions,
not a demonstration of causality. At fewer than 30 fit observations uncertainty remains
unavailable, even if an explicitly smaller fit minimum permits a descriptive estimate.

Fit payloads expose observation/target-period/missing/excluded counts, coefficients,
residuals/variance, R², SEs/covariance, degrees of freedom and scaled design condition.
Singular, degenerate, missing and nonfinite fits have explicit status/reasons and no
fallback. Categorical quality flags report limitations; no opaque confidence score,
statistical significance claim, economic materiality score or trade direction is produced.
