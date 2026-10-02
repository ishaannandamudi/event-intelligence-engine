# Canonical claim resolution

`CanonicalClaimResolution` is a field result, not an entire event winner. It pins a
semantic claim, exact dataset, knowledge cutoff, optional valid-time point, policy,
selected observation/value when justified, all visible alternatives, active assertions,
evidence/link IDs, typed conflicts, reasons and descriptive summary.

## Selection

Existing categorical `SourcePrecedencePolicy` is reused. Fact overrides use controlled
predicate names such as `SCHEDULED_FOR`, `ACTUAL` or `STATUS`. The most authoritative
applicable source class is considered first. Compatible exact time can refine a coarse
date under a pinned policy. Equivalent values have stable observation-ID ordering.
Equal-precedence incompatible assertions produce `UNRESOLVED` with no selected value.
Recency across unrelated providers does not silently settle equal-credibility conflict.

An explicit later provider update/correction replaces that thread's active assertion.
Prior alternatives and evidence remain in history. A retraction or withdrawal disables
that thread's applicable assertion and preserves the retraction. It cannot disable an
independent primary source. If all applicable assertions are retracted, the state is
`RETRACTED`; no applicable assertion is `ABSENT`. An exclusively unknown time remains
`UNRESOLVED`. Unknown time cannot contradict or corroborate an exact time.

## Temporal and numeric interpretation

Optional valid intervals are half-open. Resolution first selects the latest visible
applicable revision within each source thread. Explicit interval state changes can retain
different earlier valid states; knowledge time still excludes future revisions. Status
updates validate the existing lifecycle or the corporate lifecycle domain. Corrections
may correct a previously wrong status. Reschedules/refinements are explicit updates.

Numeric claims pin measurement context, period/stage/seasonal adjustment and canonical unit.
Converted reported quantities compare canonical values and units. Incompatible dimensions
are rejected before becoming competing assertions. Typed conflict vocabulary includes
value, time, status, entity, unit, scope and revision; identity/period/unit incompatibilities
are also caught at normalization/relationship/matching boundaries. No float-to-string
comparison, inferred unit or fabricated missing value.

Canonical selections preserve lower-precedence conflicts. A schedule field can favor an
official source while an actual-value override favors another configured source; each
result pins its own selected assertion and policy. Corroboration counts agreeing distinct
source families. Conflicting or syndicated alternatives are retained without inflating
independence. Descriptive counts never become a confidence or trading score.

## Persistence and checks

Migration 6 adds normalized immutable indexed claim/evidence/resolution tables. Foreign
keys connect raw evidence, acquisitions, assertions, references, relationships, canonical
events, candidate inputs and resolution memberships. Scalar indexes and payload references
are checked together. Resolutions recalculate from exact retained inputs. Automatic ingestion
artifacts record the evidence committed at their transaction stage; multiple provider commits
at the same clock can create different stages. Current queries include every committed stage.
Use `EvidenceEngine.freeze` for a completed cutoff that rejects later additions at that cutoff.
