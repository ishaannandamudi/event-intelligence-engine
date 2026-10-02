# Supplied matched controls

Controls are a declared supplied universe. EIE does not fetch a universe, infer industry
membership or optimize a synthetic portfolio. `FeatureSnapshot` pins instrument,
timestamp/availability, 1–16 sorted numeric features, source and dataset/hash/version.
Caller-defined bucket encodings/scales need domain justification; EIE does not impose a
distance on arbitrary categorical labels. `MatchingPolicy` declares K (1–100), equal or
inverse-distance weights, contamination exclusion, a poor-match distance threshold,
scale floor and minimum prior market periods (default 30).

## Prior eligibility

The target is always excluded. Market data must have compatible timeframe, return type,
adjustment and supplied calendar basis. Feature names must exactly match the target;
timestamp and availability must be strictly before the event and fitting cutoff.
Future feature snapshots are projected out. Missing market/features, incompatible
semantics, insufficient prior market periods, configured control-event contamination and
unresolved unadjusted corporate-action risk have explicit exclusion reasons.

Market eligibility uses **prior estimation periods only**. A missing post-event control
return does not cause rematching to another candidate. It makes the affected expected
return/CAR unavailable. Controls with supplied overlapping events are flagged or excluded
according to the declared policy. Flags are filtered by the raw study's historical
selection cutoff and actual event window. EIE cannot prove that a supplied event list
is complete or authenticate an omitted issuer-to-instrument relationship.

## Distance and scaling

The eligible prior feature population supplies each feature's population standard
deviation, floored at the explicit `minimum_scale` (default `1e-12`). The target is not
part of the scaling population. Zero-scale features remain transparent through the
recorded floor. No event-window returns, future snapshots, full future-universe
statistics or fitted return objective enter feature scaling or distance.

`distance = sqrt(sum(((control_feature - target_feature) / scale) ** 2))`.

Candidates are sorted by `(distance, instrument_id)` and the first K are retained.
Fewer than K or a distance above `maximum_distance` is a poor-match flag, with selected
controls preserved. No undocumented threshold excludes a candidate after seeing its
return. `ControlFit` records selected IDs/distances/weights/exact features, prior matching
time, scales, scaling members and all exclusions, including when no match is available.

Equal weights are `1 / selected_count`. Inverse distance uses normalized reciprocal
distance; ratios to the minimum distance avoid overflowing reciprocals. If any selected
distance is exactly zero, zero-distance members divide all weight equally and other
selected weights are zero. There is no optimization. Expected return is the explicit
weighted control mean; numerical overflow is unavailable, never a zero baseline.

## Interpretation and performance

Control estimates can disagree with market, sector and factor estimates. Sets preserve
that disagreement. A good feature match does not establish parallel trends, eliminate
selection bias, or prove causal identification. Comparable-window control volatility
and quoted-spread expectations are optional descriptive secondary metrics. No uncertainty
band is invented without a defensible fitted residual distribution.

Each request is bounded to 1,000 candidates and 16 features. Selection scans the supplied
universe once and sorts eligible distances (`O(C*F + C log C)`, plus bounded period
eligibility checks). Large-universe indexing, automatic bucketing, optimized synthetic
controls and propagation graphs are deferred. See [counterfactual impact](counterfactual-impact.md).
