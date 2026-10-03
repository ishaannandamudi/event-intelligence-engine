# EIE-13 completion report

Engine `1.4.0`; event-intelligence schema `1.0`; SQLite store `1.7`.
The EIE-13 implementation and all required verification gates are complete.
No commit or push is part of this phase.

## A. Architecture

Existing sourced event/evidence/extraction/graph inputs feed a reference manifest,
six novelty dimensions, static importance, event materiality and eight independent
uncertainty components. EIE-5 information and real EIE-9 impact remain separate.

## B. Package structure

Fourteen `eie.intelligence` modules separate schema, models, policy, transient inputs,
transactional collection, numeric comparisons, novelty, materiality, uncertainty,
engine, repository, queries and validation. No sibling-engine imports or new transport.

## C. Event-intelligence schema

New immutable schema `1.0` has closed enums, bounded collections, explicit unavailable
metrics and content-addressed inputs/results/audits/runs. Prior public schemas are unchanged.

## D. Intelligence result model

`EventIntelligenceResult` pins event/revision, dataset/build, cutoff/availability,
policy and exact input fingerprints. All six dimensions and eight uncertainty
components are required. An explicit mode separates ex-ante intelligence from ex-post audit.

## E. Document novelty

Original/normalized hashes, comparable segment recurrence and accepted-field hashes
distinguish exact repeats, partial repeats and new documents. Exact repeats contribute
zero new-segment fraction. Source bodies are retained only by their existing owner.

## F. Factual novelty

Semantic slots preserve new/updated facts, corrections, confirmations, restatements,
retractions and unresolved fields. Earlier proposition vintages explain revisions;
EIE-13 does not promote model-only proposals to factual evidence.

## G. Numeric novelty

Compatible prior reporting periods supply absolute/relative change, bound displacement,
median, MAD, count and standardized MAD. Repeated periods count once; competing period
values are excluded. Missing points, near-zero scale, zero MAD and overflow remain explicit.

## H. Guidance novelty

Same-issuer/metric/period/stage/seasonality/unit company guidance supplies low/high,
midpoint/width and relative displacement. Initiation and withdrawal have no invented
zero comparison. Analyst consensus is not a company-guidance baseline.

## I. Entity novelty

Exact stable IDs compare with prior sourced claims and an explicitly pinned prior graph.
Unresolved names and roles stay unresolved. Graph snapshot IDs remain in prior evidence references.

## J. Cross-source novelty

Source families preserve syndicated repeat, independent confirmation, contradiction,
correction and primary/secondary information states. EIE-11 retains field resolution
and conservative event grouping authority.

## K. Static importance

Explicit versioned family defaults and exact type overrides produce LOW/MODERATE/HIGH/CRITICAL.
Changing this policy creates new lineage without changing event materiality or source facts.

## L. Materiality model

Source-grounded contributions produce UNKNOWN/MINOR/MODERATE/MAJOR/VERY_HIGH with a
named maximum-available amount/scale/guidance/exposure rule. Missing contributions are qualified.

## M. Magnitude components

Currency amounts, explicitly sourced compatible company scale and company-guidance
displacements retain values/bounds/units and reasons. Open ranges and ranges crossing
thresholds stay unknown. Missing or near-zero denominators never become zero exposure.

## N. Scope/status/exposure

Supplied scope, exact entity count, epistemic/status qualification and calendar-only
duration remain independent contributions. Direct supplied graph exposures retain type,
bounds, estimates, conflicts and truncation. No inferred financial weights or FX fetch.

## O. Uncertainty decomposition

Eight categorical components retain distinct evidence and reasons. No master confidence,
calibrated probability, prediction, portfolio sizing or trade instruction is introduced.

## P. Source uncertainty

Trust classes, EIE-11 corroboration/conflicts, source families and retractions remain separate
from entity, classification and numeric diagnostics.

## Q. Entity uncertainty

Exact identities, unresolved names/roles and absent context have distinct states.
Source trust cannot resolve an unsupported identity.

## R. Classification uncertainty

Deterministic/model-assisted methods, accepted candidates, partial extraction and failures
remain visible. Failed extraction does not establish that no event occurred.

## S. Numeric uncertainty

Typed sourced quantities, ranges, conflicts and unresolved resolution are separately
qualified. Supported units/values cannot be replaced by hallucinated numeric attachments.

## T. Event-identity uncertainty

Pinned EIE-11 merge/separate/unresolved/coarse-time/truncation decisions are retained.
Later merges and splits do not mutate earlier intelligence snapshots.

## U. Materiality uncertainty

Missing company scale/exposure, incompatible units, category-crossing ranges, non-final
status and qualified graph inputs remain explicit reasons. UNKNOWN is not a zero score.

## V. Analogue readiness

Minimal identity/evidence readiness reports INSUFFICIENT_INPUTS or READY_FOR_ANALOGUE_SEARCH.
This is readiness only; no analogue retrieval or analogue confidence calculation exists.

## W. Impact uncertainty

Actual retained EIE-9 IDs/fingerprints supply fit/data/contamination/model diagnostics
only in impact uncertainty or a separate audit. Uncomputed impact remains NOT_COMPUTED.

## X. Prior-only baselines

Knowledge is strictly before the cutoff; same-time/future events and current/aliased
events are excluded from independent distributions. Numeric periods must also be earlier
and compatible. Operational and historical sample bounds fail closed.

## Y. Replay/causality

One owned SQLite transaction collects, calculates and appends the snapshot before sealing
its evidence cutoff. Historical source clocks remain distinct from local availability.
Future correction, extraction reprocessing, graph updates and identity changes retain earlier bytes.

## Z. Policy versioning

Mathematical policies and versions determine identities; operational bounds do not.
Event-resolution selection, including its absence, remains pinned during validation.

## AA. Persistence/migrations

Additive migration 8 supplies thirteen immutable indexed `ei_*` tables. Existing DDL,
raw/event bytes and historical schemas survive migration; failed DDL rolls back.
Results, policies, input manifests, component memberships, information, audits and runs are verified.

## AB. Query APIs

Bounded historical/as-of intelligence, novelty, materiality, uncertainty and prior-distribution
queries filter event, family/type, entity, measurement, source family, policy, categories and time.
Queries enforce both baseline cutoff and actual availability.

## AC. Validation

Allowlisted typed decoding, fingerprints and scalar/member indexes are checked.
Exact recollection/recalculation returns VALID, qualified WARN or INVALID.
Corruption, unsupported fields, missing references and altered indexed clocks fail closed.

## AD. Performance/storage

The measured 10,000-event history spans ten families and yields thirteen intelligence
results including actual EIE-12 document joins. Source ingestion took 202.27 s;
profiled prior collection took 378.32 s, novelty 2.54 s, materiality 0.012 s,
uncertainty 0.005 s, checked assembly 2.51 s and persistence 64.10 s.
History queries took 16.41 s; restart plus two full recalculations took 43.22 s.
SQLite occupied 344,371,200 bytes; thirteen serialized results occupied 71,990,120 bytes.
Reference collection, validation and serialization dominate. Profiling overhead,
duplicate staged-equality checks and concurrent test load were present; these are
workload measurements, not latency guarantees or CI targets.

## AE. Security

Source text remains inert; fake system/tool/shell/URL/credential prompts cannot issue actions.
Only accepted source-backed EIE-12 fields enter calculations. Unsupported information
values/units/identities/clocks fail verification. No eval, exec, pickle, data-driven imports,
network calls or source-driven filesystem actions were added. Multi-event documents stay multi-event.

## AF. Documentation

Added [novelty](novelty-intelligence.md), [materiality](materiality-intelligence.md),
[uncertainty](uncertainty-decomposition.md) and [replay](intelligence-replay.md).
Updated README, master specification, architecture, causality, evidence, extraction,
studies, impact, graph and persistence boundaries. The offline example verifies separate
EIE-5 surprise and a real persisted EIE-9 impact audit, historical clocks and restart bytes.

## AG. Tests added

293 deterministic EIE-13 tests cover component states/contracts, compatible periods,
range/overflow/scale handling, source-grounded information, real impact attachments,
multi-event text, inert prompts, reprocessing, graph context, identity merge/split,
hash seeds, insertion order/concurrency, migrations, immutability, corruption and bounds.
Existing tests retain their assertions; current engine/store version expectations were updated.

## AH. Full pytest result

Full pytest: **2,488 passed in 152.47 s**, including all 2,195 baseline tests and
293 new EIE-13 tests. The earlier current-store-version expectation failure was
corrected to `1.7`; no baseline behavior assertion was weakened.

## AI. Coverage

Full suite with branch measurement and sockets blocked: **2,488 passed in 354.92 s**;
**zero network connection attempts**. Overall branch-enabled coverage is
**96.1195%**, above the required 95% gate (18,520/19,024 statements and
6,052/6,540 branch outcomes covered). Standalone statement coverage is 97.35%;
standalone branch-outcome coverage is 92.54%. Coverage has no new exclusions.

## AJ. Ruff

Lint and format checks passed; 405 Python files are formatted.

## AK. Mypy

Strict mypy passed for all 250 source files.

## AL. Diff review

Whitespace check passed. Reviewed tracked and new source, tests, documentation,
example and benchmark changes. Prior migrations and quantitative logic remain intact.

## AM. Secret and unfinished-work scans

No secret findings or trailing whitespace. The two existing historical documentation
marker references remain; no unfinished implementation markers were added.

## AN. Benchmark/profile

Ran [the offline profile](../benchmarks/event_intelligence.py) with 10,000 persisted
historical events. [Recorded measurements](../benchmarks/EIE-13-profile.json) include
dominant cumulative functions, storage counts and successful byte-identical restart.

## AO. Engine/schema/store versions

Engine `1.4.0`; new event-intelligence schema `1.0`; SQLite store `1.7`.
Text extraction and every earlier public schema remain unchanged.

## AP. Dependencies

Runtime dependencies remain empty. Existing development tools were reused; no new
package, embedding service, model transport, graph database or sibling dependency was added.

## AQ. Remaining limitations

History is materialized and deeply validated per cutoff; reference manifests and repeated
serialization have substantial cost at 10,000 events. Thresholds are explicit descriptive
policy defaults, not empirical calibration. Missing scale/exposure and disputed ranges
stay unknown. There is no FX lookup, inferred exposure, embedding comparison, indirect
exposure composition, analogue retrieval or reaction prediction. These are documented boundaries.

## AR. Exact proposed EIE-14 scope

Causal historical analogue retrieval conditioned on event type, information magnitude,
company/sector characteristics and explicitly supplied MCE volatility/liquidity/options/
macro/timing context; transparent prior-pool selection and distances; historical reaction
distributions and separate observed-versus-expected market-reaction percentiles/residuals.
Preserve information surprise, analogue uncertainty and empirical impact as separate outputs.
No trading recommendations, portfolio/execution logic or implicit sibling integration.
EIE-14 is proposed only and was not implemented. Stop after EIE-13.
