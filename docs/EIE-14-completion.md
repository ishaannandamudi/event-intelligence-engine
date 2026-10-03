# EIE-14 completion report

EIE-14 is complete. All required gates below pass. Changes remain in the working tree.

## A. Architecture

Retained EIE-13 ex-ante intelligence and supplied historical context produce small,
immutable feature rows. Indexed blocking precedes transparent distances, bounded
selection, response distributions and eight independent diagnostics. A later target
response is compared with the exact frozen result. EIE-11 remains the identity and
cross-source resolution owner; EIE-8/EIE-9 remain response/impact owners.

## B. Package structure

`src/eie/analogues` contains fourteen modules: schema, models, features, responses,
pool, distance, retrieval, uncertainty, reaction_surprise, repository, engine,
queries, validation and the package schema version. Provider parsing and prior
phase calculations are reused, with no sibling-engine import.

## C. Analogue schema

Historical-analogue schema 1.0 introduces typed categorical policies, named feature
kinds/sources, missing/zero-scale policies, response metrics, exclusions, result
states, eight uncertainty kinds and deterministic validation states. Prior public
schemas remain unchanged.

## D. Query/result models

Frozen contracts pin target event/revision, dataset/build, feature row, cutoff,
actual availability, policy, response cohort, K and optional EIE-11 resolution.
Results retain exact target/policy/input fingerprints, bounded pool references,
prior scales, selected feature/distance components, response distribution and
diagnostics. Immutable run manifests pin multiple results.

## E. Candidate pool

Candidate rows and compatible completed response observations are read from the
index. Revisions and supported connected EIE-11 memberships contribute one observation
per underlying event. A pinned current EIE-11 run overrides older memberships,
including explicit splits. Prior alias evidence without a response can deduplicate
completed older samples; future/same-time evidence cannot. The latest fully eligible
vintage is retained. Ambiguous competing feature vintages fail.

## F. Strict-prior semantics

Candidate event availability, feature cutoff and response completion/availability
must be strictly before T. Current event, all known aliases/revisions, same-time
and future events are excluded. Live feature availability must precede T;
qualified EIE-7 historical projections retain separate actual build/extraction
availability and require it to be available at the retrospective query time.

## G. Event/type conditioning

EXACT is the default. SAME_FAMILY and an explicit ALLOWED_SET are separate policies.
There is no automatic type expansion or response-driven cohort selection.

## H. Information magnitude features

Named EIE-13 numeric novelty, materiality amounts, company-scale and guidance
metrics can be requested by dimension/metric and measurement. Normalized EIE-5
information surprise requires an explicit measurement and its retained owner.
Absent, ambiguous or incompatible semantics remain missing; values are not blended.

## I. Entity/sector conditioning

Exact retained entity and canonical company-scope identities are reused. Same
issuer is optional. REQUIRE_SAME sector is the default and missing sector cannot
silently broaden the pool. UNCONDITIONED explicitly permits cross-sector use.
Historical EIE-10 snapshots can supply unambiguous sector/industry categories.

## J. Volatility/liquidity/options/macro/context

Provider-neutral context supports explicitly supplied volatility, spread/depth,
liquidity/ADV, IV/skew/term structure, rates/inflation/growth/policy, macro proximity,
company-scale buckets and graph/custom categories. Every input has source/version,
unit, observation/availability and optional validity bounds. Future labels fail.

## K. Timing features

Session, day/week, publication/effective and proximity dimensions use named supplied
context with visible clocks. No exact timing, session category or future schedule
knowledge is inferred from incomplete evidence.

## L. Feature model

Each feature names its source, selector, categorical/numeric/ordinal/boolean kind,
weight, missing treatment, scaling and zero-scale policy. Values preserve semantic
signatures and source IDs. Opaque vectors and learned weights are absent.

## M. Distance model

Categorical/boolean distances are 0/1; ordinals use normalized declared-level gaps.
Numeric distance is absolute difference divided by strictly prior raw MAD or IQR.
The target never contributes to scaling. Total distance retains weighted numerator,
configured and comparable weights, missing penalties and the normalization rule.

## N. Missing/zero-scale handling

REQUIRE and EXCLUDE_CANDIDATE refuse missing dimensions. IGNORE_WITH_PENALTY retains
an explicit penalty, never a zero-valued imputation. Minimum comparable count and
weight fraction are enforced. Zero scale uses declared exact-match or unavailable
behavior; no epsilon is added. Nonfinite arithmetic remains explicitly unavailable.

## O. Candidate blocking

SQLite blocks by dataset/policy, type/family, time range, optional issuer, sector and
explicit context categories before materialization. Measurement, industry,
materiality, response, target and run indices are retained. Excess bounded scans
fail rather than returning an arbitrary prefix.

## P. Top-K selection

K defaults to 20. Candidates exceeding the declared maximum distance are excluded;
ties use distance, stable event ID, revision and row ID. Fewer than K is valid.
Insufficient history prevents distribution statistics without inventing samples.

## Q. Response metrics

Real retained EIE-8/EIE-9 artifacts supply raw/absolute returns, abnormal/absolute
abnormal returns, named CAR, realized-volatility change, relative volume, supplied
IV change and quoted spread change. Cohorts pin compatible build/window/return and
model semantics. Each response separately pins its exact study/impact/model IDs.
Missing data, contamination and reconstruction remain explicit.

## R. Reaction distribution

Unweighted selected raw values and IDs, count, mean, median, type-7 quantiles, raw
MAD, sign frequencies, median absolute response and extrema are retained. Minimum
history defaults to five. There is no automatic trimming, weighting or forecasting.

## S. Frozen ex-ante analogue set

Collection, calculation, persistence and cutoff sealing share one owned SQLite
transaction. The target response is excluded from retrieval input. Later response
attachment never reruns retrieval or changes members/distances.

## T. Reaction surprise

Signed and magnitude empirical percentiles use deterministic midrank ties.
Residual is observed response minus historical median; standardized residual uses
raw MAD. Zero MAD supplies no standardized value. Exact target row/cohort and
strict post-completion availability are required.

## U. Information-vs-reaction surprise separation

EIE-5 information surprise compares actual information with prior expectations.
EIE-14 compares observed response with a frozen historical response distribution.
The example exposes both objects; metadata-only fixtures have explicitly missing
numeric actual/consensus information rather than fabricated surprise.

## V. Impact-vs-reaction surprise separation

EIE-9 counterfactual impact and model specification remain separate immutable
owners. Their compatible abnormal/CAR response can supply a distribution, but
reaction surprise never replaces impact or becomes a combined score.

## W. Analogue uncertainty

All eight independent components are retained: pool size, feature coverage,
distance quality, response coverage, context coverage, contamination,
reconstruction and distribution stability. States are OBSERVED/LIMITED/UNAVAILABLE;
there is no master confidence or calibrated probability.

## X. Pool/distance/coverage diagnostics

Diagnostics expose total prior events, blocked rows, eligible unique events,
complete responses, selected count, typed exclusions, nearest/median distance,
requested context coverage and nested top-5/10/20 response medians. Instability
threshold is explicit in response units. Numeric overflow remains unavailable.

## Y. Persistence/migrations

Additive transactional migration 9 raises store 1.7 to 1.8 with fifteen immutable
tables and their indices. UPDATE/DELETE triggers, source fingerprints, membership
checks, exact recalculation and rollback tests protect artifacts. Earlier DDL and
payloads remain intact; prior version expectations advance to the current store.

## Z. Replay/causality

Future events, intelligence/context/graph corrections and target responses cannot
alter old results. Changed target/context/policy/cohort creates separate lineage.
Late backdated index insertions into sealed dataset/policy cutoffs are refused.
Concurrent future insertion and restart preserve the exact frozen snapshot.

## AA. Query APIs

`analogues`, `analogues_as_of`, `analogue_distribution`, `analogue_uncertainty`,
`candidate_explanations`, `reaction_surprise` and `run` expose retained artifacts.
As-of queries enforce source and actual availability. Row counts and aggregate
serialized output are bounded; ordinary queries do not rebuild EIE-13 history.

## AB. Validation

Validation rechecks blocking, pool references, aliases/counts, scales, components,
selection, distribution, uncertainty and fingerprints. Deep validation rebuilds
features from retained owners and extracts responses from actual study/impact
artifacts. Reaction reads recalculate from the exact frozen distribution.

## AC. Performance/storage

Immutable prevalidated feature/response rows keep ordinary retrieval independent
of repeated full EIE-13 construction. Results retain references and small values,
not entire event/intelligence/study/market payloads. The profile retained 250 rows
across ten families, ten results and 20 selections per result; indexed type blocks
materialized 25 rows per query. Ten result payloads totaled 630,123 bytes.

## AD. Security

Source/context text remains inert data. There is no source-driven import, execution,
filesystem action, URL fetch or external model/tool call. Closed typed persistence
and parameterized queries are reused. Fake instructions/URLs/credential strings,
unsupported inputs, corruption and bounds are exercised. Socket results are below.

## AE. Documentation

README and master status/boundary are updated. New historical-analogues,
analogue-distance, market-reaction-surprise and analogue-uncertainty documents
describe formulas, clocks, limits and qualifications. Architecture, replay,
novelty, materiality, uncertainty, studies, impact, graph and persistence reference
the new consumer. The offline example passes and restarts with identical bytes.

## AF. Tests added

Ten analogue test modules and mathematical helpers add 359 cases covering contracts,
pool/blocking/prior clocks, features/graph, explicit distances, zero/missing values,
response compatibility, percentiles/residuals, frozen leakage, uncertainty,
concurrency, deterministic order/hash seeds, corruption, migration and restart.

## AG. Full pytest result

Full pytest: **2,847 passed in 169.51 s**. Final focused analogue suite:
**359 passed in 35.71 s**. The starting EIE-13 baseline had 2,488 passing tests.

## AH. Coverage

Full socket-blocked suite: **2,847 passed in 462.36 s**, with **zero network
connection attempts**. Branch measurement was enabled. Combined coverage:
**96.09%**, above the 95% gate; statement coverage 97.35%, branch-only coverage
92.40%. Counts: 20,372 statements and 6,978 branches. Coverage reporting passes
the configured 95% threshold.

## AI. Ruff

Lint passes; format check passes for all **438 Python files**.

## AJ. Mypy

Strict mypy passes for all **264 source files**, with no errors.

## AK. Git diff check

`git diff --check` passes. Changed prior-phase code is limited to additive codec
registration, migration/version declarations and current-version expectations;
the original EIE-13 migration test still explicitly starts from store 1.6.
Tracked and new source, examples, profile, documentation and tests were reviewed.

## AL. Secret/unfinished-marker scan

461 text files scanned: no secret findings or trailing whitespace. No new
unfinished markers. The two existing marker references remain in
`docs/EIE-5-completion.md:51` and `docs/EIE_MASTER_SPEC.md:1618`.

## AM. Benchmark/profile

`benchmarks/historical_analogues.py --count 250` completed using actual historical
source/intelligence/study owners; measurements are in `benchmarks/EIE-14-profile.json`.
Instrumented totals: feature indexing 20.40 s; ten blocked retrievals 0.387 s;
scaling 0.0265 s; distances 0.117 s; top-K 0.000173 s; distributions 0.00240 s;
result persistence 1.346 s; reaction comparison/persistence 0.0780 s; deep validation
3.396 s; restart/query 0.878 s. SQLite size was 113,078,272 bytes, including the
existing source/intelligence/study owners. EIE-13 construction dominated at 306.03 s.
These are local cProfile measurements taken alongside verification, with no CI
timing threshold or 100k-row claim.

## AN. Engine/schema/store versions

Engine 1.5.0; historical-analogue schema 1.0; store 1.8. All prior public schemas
retain 1.0. Existing version expectations and historical migration tests are updated.

## AO. Dependencies

Runtime dependencies remain empty. Implementation uses the standard library and
existing EIE contracts. No numerical/ML/vector database or sibling dependency is
introduced. Existing local development tools were used without changing a sibling.

## AP. Remaining limitations

Context is supplied or explicitly pinned from retained graph state; there is no
automatic regime discovery. No approximate neighbors, learned weights or optional
response weighting are implemented. Changed current alias resolution must be
explicitly pinned from EIE-11; otherwise supplied historical memberships are used.
Historical projections remain qualified and do
not claim original local availability. New backdated history requires new immutable
dataset/policy lineage once a cutoff is sealed. Deep validation and EIE-13 construction
remain more expensive than ordinary retrieval. Performance beyond 250 events is
unmeasured. Collision/interaction models, service and trading logic remain deferred.

## AQ. Exact proposed EIE-15 scope

Event Collision + Interaction + Propagation Chronology: overlapping events,
same-issuer and macro/issuer collisions, graph-related chains, temporal propagation,
interaction-aware contamination, non-additive combined-event handling, sequence/state
transitions and propagated-event timing. No naive severity summation and no trading
recommendations. This is proposed scope only; EIE-15 is not implemented.

No commit or push was performed. Work stops after EIE-14.
