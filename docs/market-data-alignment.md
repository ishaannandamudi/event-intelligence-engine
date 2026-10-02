# Market-data alignment

EIE consumes supplied typed historical data. Explicit identities pin timeframe,
source, dataset fingerprint/version, generic instrument category, adjustment and return
semantics. Timeframe is never inferred from observed spacing. Intraday bars can be shorter
than their declared timeframe and are flagged partial; a daily bar can represent a supplied
session. Overlapping/unsorted bars and reversed clocks fail validation.

## Completed observations

A bar covers `[start,end)` and becomes available no earlier than its completion. Eligible
pre bars end at/before the anchor and are available at/before it. Thus a close at the anchor
belongs to the just-ended interval. Post bars start at/after the anchor and complete within
the post window. A bar spanning the anchor is excluded from both sides; no partial OHLC is
synthesized from a future completed bar. Retained observations are bounded by data as-of.

`LAST_COMPLETED` picks the latest eligible close at/before the offset target.
`FIRST_BEGINNING` picks the first eligible bar beginning at/after it, preserving its actual
completion time. Tolerance is explicit and strictly less than one declared bar interval;
a missing expected completed bar cannot be carried forward as a flat zero response.
Pre points cannot select post-event bars. No interpolation is performed.

Reference policy is explicit:

| Policy | Reference |
|---|---|
| `PRE_CLOSE` | Last causally available completed pre close within tolerance |
| `FIRST_POST_OPEN` | First eligible post bar open within tolerance |
| `ALIGNED_CLOSE` | First eligible completed post bar close within tolerance |

A future post reference never produces a pre/zero-offset curve value. The independent
pre-return calculation remains based entirely on pre-event bars. Missing/zero references,
nonpositive log prices and nonfinite calculations produce unavailable values.

## Session clocks and coarse schedules

Elapsed windows advance UTC duration. Trading windows consume only supplied open/close
intervals and skip supplied closures. Session metadata has source, version, availability
and stable labels; future metadata is not used. Insufficient calendar coverage fails closed.
No exchange calendar, timezone approximation or holiday model is invented.

Coarse BMO/AMC/date-only source facts cannot supply an exact timestamp. Exact-time studies
reject them. Under explicit `SESSION_OPEN`/`SESSION_CLOSE`, a dated source label maps to
exactly one supplied session endpoint, while the original anchor remains date/session
quality. BMO requires open, AMC requires close; a date-only event requires an explicit
endpoint choice. These are proxy anchors, not recovered publication times. During-market
coarse labels still require an explicitly chosen endpoint policy and remain coarse.

An anchor outside a supplied open interval is flagged market closed. Trading offsets may
reach later sessions; elapsed offsets remain elapsed. Windows, policies and response
points preserve these different meanings. Known active gaps, event-spanning bars, partial
sessions and insufficient endpoints are reported. An absent calendar is not evidence of
an always-open exchange; only elapsed calculations are available without one.

## Corporate actions and return compatibility

Series declare unadjusted, split adjusted, or split/dividend adjusted prices. They also
separately declare price-return or total-return semantics. Total-return input must declare
split/dividend adjustment. EIE trusts the supplied transformation/version, pins its dataset
and does not rebuild a market-data adjustment engine.

Unadjusted series always carry corporate-action risk. A supplied split boundary, or a
visible matching EIE split/reverse-split event with an effective time in the window,
suppresses price/volatility returns rather than presenting the split jump as a response.
Missing action knowledge is not proof that the unadjusted interval is safe. Adjusted data
is preferable for these studies. Raw ex-dividend price movement is distinct from total
return. Asset/benchmark comparisons and aggregations reject incompatible adjustment or
return semantics; log-vs-simple method is independently pinned in the policy/cohort.
