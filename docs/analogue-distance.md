# Transparent analogue distance

Every `AnalogueFeature` names its source, selector, kind, weight, missing policy,
penalty, scale method and zero-scale policy. Named values preserve their input
references and semantic/unit signature. Incompatible signatures are missing
comparisons; they are never numerically blended.

## Comparisons

- CATEGORICAL and BOOLEAN: exact match 0, mismatch 1.
- ORDINAL: absolute position gap divided by `(number of declared levels - 1)`.
  The explicit ordered levels are part of policy identity.
- NUMERIC: `abs(target - candidate) / prior_scale`. The scale is raw MAD or
  type-7 interquartile range from unique, eligible, strictly prior candidate rows.
  The target is never a scaling member. Default minimum scale history is five.

Raw MAD here has no Gaussian consistency multiplier. EIE-5 normalized information
surprise retains its own independent normalization convention. These are different
objects and neither replaces the other.

Zero scale uses EXACT_MATCH (0 for equality, 1 for difference) by default. The
alternative UNAVAILABLE treats the dimension as missing. There is no epsilon
substitution. Insufficient scale history and unrepresentable finite arithmetic
remain explicit unavailable comparisons.

## Missing features and normalization

REQUIRE and EXCLUDE_CANDIDATE reject a candidate missing that dimension.
IGNORE_WITH_PENALTY retains a missing component with no numeric comparison and adds
`weight * missing_penalty`; no zero value is imputed. The retained total is:

```text
distance = (sum(weight * component_distance) + sum(missing_penalties))
           / total_configured_weight
```

Comparable weight is separately reported, together with count and fraction.
Default minimum comparable count is one and fraction 0.5. A penalty never makes a
missing feature comparable. Feature weights are positive, explicit and versioned;
there is no learned weighting or use of future outcomes.

K defaults to 20 within configured bounds. Maximum acceptable distance is explicit
(default 2). Candidates exceeding it are excluded, even when fewer than K remain.
Ties order by stable event ID, revision and feature-row ID. An all-distant pool
returns typed unavailability and its measured nearest-distance diagnostic.

There is no embedding, anonymous float array, inferred regime, automatic outlier
removal or hidden type expansion. Optional response weighting is deferred; all
retained response samples are unweighted.
