# Analogue uncertainty

Every result contains all eight categorical components with reasons and separate
numeric diagnostics. There is no master confidence, calibrated probability or
return-direction interpretation.

| Component | Retained diagnostic |
| --- | --- |
| POOL_SIZE | Eligible and selected counts against explicit minimum history |
| FEATURE_COVERAGE | Mean selected comparable-weight fraction; missing dimensions remain visible |
| DISTANCE_QUALITY | Best measured and median selected distance against a declared threshold |
| RESPONSE_COVERAGE | Blocked candidate and completed compatible response counts |
| CONTEXT_COVERAGE | Requested dimensions present in the historical blocked pool |
| CONTAMINATION | Count of retained EIE-8 contamination flags, including excluded history |
| RECONSTRUCTION | Count of qualified source/build reconstruction rows |
| DISTRIBUTION_STABILITY | Top-5/10/20 response medians and their range |

OBSERVED means the stated diagnostic was observed under policy, not that outcomes
are predictable. LIMITED preserves a small/sparse/distant/contaminated/reconstructed
or unstable comparison. UNAVAILABLE means the corresponding diagnostic could not be
computed or no such context was requested.

Contamination policy is INCLUDE_WITH_FLAG, EXCLUDE (default), or SEPARATE_COHORT
with an explicit clean/contaminated cohort. Existing study quality is reused; no
collision model is introduced. Gap/coarse-time/reconstruction rejection is explicit.

Nested median range is compared with a policy threshold in response units (default
0.02). It is descriptive sensitivity to selected neighbor count, not a statistical
stability test. Fewer than two nested subsets leaves stability unavailable. The
threshold is part of the policy fingerprint and should be supplied appropriately
for volume, spread and volatility units.

Two results can have the same nearest distance and different response coverage,
context availability, contamination and reconstruction components. Those differences
remain intact. No component is converted into a trading confidence score.
