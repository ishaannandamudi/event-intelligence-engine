# Explicit source coverage

A `CoverageUniverse` pins dataset, provider, resource, scope, half-open interval,
knowledge time, expected slots and denominator reference/fingerprint. Slots retain
expected key, due time, event type and entity/scope. This is a configured expected universe,
not a discovered global denominator.

Append immutable `CoverageAssessment` records under each slot. States remain distinct:

- OBSERVED: pins a matching locally available source row.
- KNOWN_EMPTY: explicit sourced successful-empty assessment.
- MISSING: explicit evidence of a missing expected item.
- UNKNOWN: insufficient information or no assessment.
- NOT_APPLICABLE: explicit exclusion from the applicable denominator.

No item, failed fetch or failed extraction automatically becomes known empty or missing.
Denominator and assessment evidence references are caller-supplied trusted coverage
contracts. Observed assessments additionally verify dataset/provider/resource/type/scope
and row availability. Expected rows due at or after cutoff remain unknown.

`coverage(universe_id, known_as_of_ns)` selects the latest strictly prior assessment per
slot. Same-time contradictory assessments fail. All counts and assessment IDs are saved.
The observed ratio is observed / (expected − not applicable), including unknown slots
in the denominator. Known empty is counted separately and does not mean an observed event.
An empty applicable denominator or absent universe yields no ratio. Unknown slots and an
explicitly incomplete universe qualify the result as INCOMPLETE_UNIVERSE.

For the stated 100-item universe, 95 observed / 3 missing / 2 unknown gives ratio 0.95;
it does not assert that all global releases have been found. New assessments and denominator
versions cannot change old saved bytes. Queries and provenance remain bounded.
