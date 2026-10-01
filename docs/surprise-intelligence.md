# Surprise intelligence (EIE-5)

Surprise is a descriptive component-level difference. No sign is interpreted as
bullish, bearish, a probability, an expected return, or an order instruction.
See [expectations and consensus](expectations-and-consensus.md) for measurement,
unit, provenance, provider selection, and availability contracts.

## Actual, previous, and revised previous

`ActualObservation` preserves an immutable context, quantity, source evidence,
publication time, revision chain, quality and schema. `ActualRole.ACTUAL` describes
the measured release. `PREVIOUS` and `REVISED_PREVIOUS` are independent streams
with an explicit previous reference period. Neither overwrites the other and
neither substitutes for consensus. Later revisions appear only after their
availability. Publication cannot follow provider observation.

Actual selection uses `actual.<lowercase_measurement_id>` source precedence,
latest available knowledge among equally ranked claims, then stable stream ID.
Competing actual provider values remain in `actual_claims` and are conflict-flagged.

## Frozen release cutoff

For a given event/context, consider original actual records known by query time.
The release anchor is the earliest original publication; original availability
is the earliest original actual availability. `cutoff = min(release, availability)`.
This conservatively prevents delayed delivery or a correction from admitting an
estimate after the initial release. A later actual correction uses the original
anchor and only changes the later actual view.

Default `STRICT_BEFORE` selects consensus at `cutoff - 1` UTC nanosecond, requiring
estimate availability **strictly before** cutoff. `AT_OR_BEFORE` selects at cutoff
and is an explicit opt-in for justified provider timing. At cutoff zero, strict
selection admits no estimate. Estimate vintage must not follow publication, and
provider observation/ingestion/availability remain independent causal clocks.
If actual is absent, the query can expose current as-of consensus but surprise is
missing. No scheduled BMO/AMC time is invented or substituted for publication.

The T1/T2/T3/T4 example is decisive: A reports 2.40 at T1, 2.45 at T2, actual 2.50
arrives at T3, and A later delivers a retrospective 2.47 at T4. T3 consensus stays
2.45 and raw surprise stays approximately 0.05. Earlier consensus and surprise
bytes remain identical even after T4 ingestion. Exact-boundary and delayed-arrival
cases are tested for both policies.

## Raw and optional relative surprise

`raw_surprise = actual.canonical_value - consensus.value` requires equal measurement
context, reference period, stage/adjustment, canonical dimension, currency, and
point-in-time provenance. Missing actual/consensus or incompatible inputs return
missing with explicit reasons. Stale/conflicted estimates remain descriptive
values carrying quality flags. Arithmetic overflow returns `NUMERIC_OVERFLOW`.

Relative surprise is disabled by default. Opt-in supports diluted EPS and revenue
only, in the corresponding monetary dimensions:
`relative = raw / abs(consensus)`. Denominators below the configured absolute
minimum produce `RELATIVE_CONSENSUS_NEAR_ZERO`; other components are ineligible.
Negative earnings consensus uses its absolute magnitude. No epsilon replacement
or silent clipping is performed.

EPS and revenue are separate queries, as are macro headline/core, MoM/YoY,
unemployment, payrolls, wages, PCE and GDP. NFP reported in thousands is normalized
to counts. CPI reported in percent is normalized to fractional rates. GDP stage
and seasonal adjustment remain explicit and cannot silently share a scale.

`SurpriseObservation` retains actual and competing claims, previous/revised
previous, the complete frozen consensus snapshot, cutoff/publication/original
availability clocks, raw/relative/normalized values, scale artifact, flags,
reasons, configuration fingerprint, and deterministic result fingerprint.

## Strict-prior history and normalization

`surprise_history(measurement, as_of_ns, ...)` returns ordered immutable
`HistoricalSurprise` rows reconstructed from observations known by the requested
time. Each row has context, event type, issuer, canonical unit, release/availability
time, consensus provider/method, raw surprise, and the source surprise fingerprint.
Rows also have their own domain-separated fingerprint. Query origin/method are
used consistently when reconstructing each event; no cache depends on call order.

For a current release at cutoff C, the engine reconstructs historical surprises
at `C - 1`. Eligible rows must have **both release and actual availability < C**,
exclude the current event, and match measurement, event type, issuer, canonical
unit/currency, release stage, seasonal adjustment, and consensus method.
Same-time releases are excluded together, independent of event traversal order.
Future releases, later revisions, and delayed actuals unavailable at C cannot
enter the scale. Historical actual corrections known before C can participate;
corrections arriving after C cannot rewrite the scale for that release.

Default history uses the same selected consensus provider. `EXPLICIT_POOL` accepts
the selected historical provider across events, records `POOLED_PROVIDER_HISTORY`
and degraded quality, and never averages multiple provider claims within an event.
It is an explicit methodology choice. Expanding history is the default; a configured
rolling window selects the last N eligible events ordered by release and event ID.

The default robust scale is
`1.4826 * median(abs(historical_raw - median(historical_raw)))`.
Sample standard deviation is available as an explicit alternative. Both require
the minimum number of prior observations (default five, minimum three). Insufficient
history, zero scale, and non-finite scale produce missing normalized surprise with
`INSUFFICIENT_HISTORY`, `ZERO_SCALE`, or `NON_FINITE_SCALE`. No epsilon is forced.
`normalized = current_raw / historical_scale`; the historical center is preserved
for audit and is not subtracted from raw surprise. Normalized overflow fails closed.

`SurpriseScale` records current context, cutoff, method, provider scope, center,
positive finite scale or missing, all selected historical rows, derived count,
reasons, configuration fingerprint and result fingerprint. The included history
provides exact start/end event IDs/times and complete calculation evidence.
It rejects future/self history and unordered/duplicate events.

## Guidance

`GuidanceObservation` has stable issuer, event and target guidance period,
`GUIDANCE.*` measurement, publication/knowledge clocks, raw source evidence,
revision, and optional low/high/point quantities. At least one bound or point is
required. Units must agree; reversed ranges and points outside bounds are rejected.
Both bounds derive a midpoint, retained separately from the original range. A
point alone supplies a midpoint; one bound alone does not.

`guidance_change(new)` compares with the latest strictly earlier company guidance
from the same provider, issuer, target period, measurement, stage and adjustment.
It reports midpoint, low and high changes separately. `guidance_vs_consensus(new)`
uses matching target-period market consensus strictly before new guidance publication.
These are separate comparison types with separate fingerprints and missingness
reasons. Missing priors, incompatible units, incomplete midpoint and overflow
never become zero changes. Company guidance is not automatically analyst consensus.

## Replay and limits

Canonical JSON preserves explicit missing values, sorts keys/immutable collections,
and uses domain-separated SHA-256 fingerprints. Histories and scale evidence are
reconstructable from the append-only ledger. Runtime latency/failure counters do
not enter result fingerprints. Failed ingestion changes diagnostic health only;
successful batch commits remain atomic.

This phase adds synthetic offline expectations only. It has no live acquisition,
restart/durable storage, FX, analyst identity reconciliation across vendors,
event studies, text extraction, directional scoring, trading or downstream service
integration. Next phase is live provider transport and ingestion infrastructure.
