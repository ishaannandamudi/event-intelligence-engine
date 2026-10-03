# Empirical source diagnostics

EIE-16 keeps source class, empirical reliability, information latency, claim/content
uncertainty and empirical market impact independent. There is no master confidence score,
provider ranking, trading direction or automatic change to source precedence.

Construct `ReliabilityRepository` over the existing SQLite store. `index_claim`,
`index_event` and `index_observation` verify retained EIE-11/canonical/raw owners once,
then save immutable compact rows. Claims retain exact semantic ID, source family and
revision actions. Source bodies, claim payloads, intelligence and market artifacts are
not copied. The index defines the explicitly retained diagnostic universe.

`source_reliability(ReliabilityQuery(...))` requires dataset, concrete provider/resource,
cutoff and `SourceReliabilityPolicy`. Exact event-type, predicate and scope filters are
optional explicit grouping dimensions; omitted filters mean all retained rows in that
named resource and subject kind. Claims/events/raw deliveries are separate sample kinds.
A claim-field revision is one sample, not a whole multi-field document. Each component
retains its own denominator and counts. Concrete source classes remain categorical metadata.

Metrics use actual local source availability strictly **before** the cutoff.
Later diagnostic frames retain the original source sample clock; frame availability
also must be strictly prior. Multiple frames of one source revision count once. Same-time
observations do not influence the prior baseline. Reconstructed source availability does
not move a local acquisition into an earlier sample. ALL_PRIOR, TRAILING_DAYS and
TRAILING_OBSERVATIONS are explicit policies; the last includes the whole timestamp tie at
its boundary, or fails the configured bound. No stable-ID choice breaks a semantic tie.
Immediate predecessors can be collected outside a rolling window for stability comparison;
they do not enter the period sample. Policy version/minimum count/quantiles/bounds/definitions
and an optional pinned coverage-universe ID all participate in result identity.

Behavior rates separately retain ORIGINAL, UPDATE, ERROR_CORRECTION, RETRACTION,
EXPECTED_REVISION, SCHEDULE_CHANGE and SUPERSESSION. EIE-11 CORRECTED/RETRACTED/WITHDRAWN
statuses remain authoritative. Revised-previous numeric semantics and explicit macro
revision actions qualify expected revisions. Legitimate rescheduling is a separate action.
Value differences never infer correction or error. Cancellation is an update, not proof
of a retracted source item. Identical retained deliveries/indexing are idempotent.

Consistency components retain self-revision rate, same-field value stability, required
clock schema completeness, publication timestamp stability and identifier stability.
Changed timestamps/identifiers are descriptive diagnostics, not a reliability verdict.
Timeliness retains separate distributions described in [latency](information-latency.md).
Coverage stays a separate explicitly scoped [universe](source-coverage.md) artifact.

Corroboration/conflict use exact EIE-11 claim IDs and source-family exclusions. Peers are
latest visible source-thread revisions at the strict cutoff, excluding retracted states.
Same syndicated family is never independent confirmation. Compact canonical value hashes
reuse EIE-11 numeric/unit and audit-metadata compatibility; coarse/exact time compatibility
uses EIE-11 directly. Compatible refinement does not become disagreement. Cross-source
conflict establishes no ground truth. Source-family defaults remain qualified as ambiguous.

Results retain input/sample/peer references and independent LOW_SAMPLE,
INCOMPLETE_UNIVERSE, SOURCE_FAMILY_AMBIGUITY, CONFLICT_GROUND_TRUTH_UNKNOWN and
RECONSTRUCTION_LIMITATION reasons. `history` returns retained results at explicit cutoffs;
`compare` exposes signed component deltas between explicit provider/resource or period
results without re-ranking trust. `run` saves a bounded atomic manifest.

Normal collection uses provider/resource/field/type/scope/availability indices and compact
rows; it does not reconstruct every upstream observation or resolution. Deep validation
rechecks exact owners and pinned historical selection. Output/count/revision/group bounds
fail closed. Migration 11 advances store 1.9→2.0 transactionally with immutable rows,
artifacts, request mappings, assessments and seals. Prior tables and payloads are unchanged.
