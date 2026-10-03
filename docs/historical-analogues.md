# Historical analogues (EIE-14)

Engine 1.5.0 adds descriptive, transparent historical comparison under schema 1.0.
`AnalogueEngine.analogues(AnalogueQuery)` freezes a strictly prior candidate pool,
then selects up to the declared K by explicit distance. No response prediction,
trade instruction or sibling-engine integration is part of this package.

## Inputs and clocks

`AnalogueRepository.index_features` reads a retained, verified EIE-13 ex-ante
artifact once. A small immutable `FeatureRow` pins its event/revision/fingerprint,
dataset/build, source cutoff, actual availability, intelligence reference,
measurement IDs, visible named features, source/version context and graph snapshot.
Its feature cutoff cannot follow the event revision's availability. Recomputing
intelligence after the event cannot supply an ex-ante feature row.

Candidates require event availability **strictly less than T**, and the required
EIE-8/EIE-9 response must be complete and available **strictly less than T**.
Same-time events are excluded. No ordering exception is inferred. Target identity,
all known aliases and revisions are excluded using pinned EIE-11 resolution and
EIE-13 alias references. Candidate revisions and known aliases contribute one
sample. Ambiguous feature vintages fail closed. The latest fully eligible revision
is selected; a revision without a compatible complete response does not replace a
completed eligible vintage. Visible prior alias evidence can still identify duplicate
older samples when that alias row lacks a response. Supported connected memberships
are canonicalized across rows; a pinned EIE-11 run overrides older memberships for
its covered identities, including explicit splits. No EIE-11 grouping rule is changed.

Historical EIE-7 source reconstruction is explicitly qualified. Source cutoff
and local build/extraction availability remain separate: a retrospective result
can describe a prior source view, but its actual availability cannot predate the
build or target intelligence. As-of queries require both clocks. This does not
claim that the local system produced a result at the original source timestamp.

## Blocking and bounds

Policy has EXACT, SAME_FAMILY or an explicit ALLOWED_SET of types. Default sector
conditioning is REQUIRE_SAME; unavailable sector returns an unavailable pool.
UNCONDITIONED explicitly permits cross-sector comparison. Same issuer is optional;
canonical company-scope IDs and exact EIE-13 identities are reused. Missing company
metadata, issuer mappings and sector classifications are never inferred.

Graph categories require an exact historical valid/known snapshot. Truncated
snapshots and unsafe unresolved/conflicted/reconstructed edges cannot establish
unique context. Open-ended validity and other retained graph qualifiers remain
explicit on the feature row; exposure missingness does not invent an exposure value.

SQLite indices block on dataset/policy/type/family, issuer, sector and explicit
context categories before materializing rows. Measurement, industry, materiality,
response metric, knowledge time and run/target indices are also retained. Narrowing
criteria are fingerprinted. The scan bound defaults to 10,000, selected bound 100,
feature bound 64, context bound 32, response bound 1,000, reason bound 64 and output
bound 10 MB. A bound overflow fails; it never chooses an arbitrary prefix of rows.

Pool diagnostics expose total prior event count, blocked rows, eligible unique
rows, complete responses, selected count, reason counts, nearest/median distance,
requested context coverage and nested median diagnostics. Type/sector/context
blocking removes rows before scoring; blocked-out rows are reflected in total
versus blocked counts. Future rows are filtered by the index and never serialized
into a historical result. Exclusions on collected rows retain typed reasons.

## Frozen result and replay

Selected candidates expose feature values, per-dimension distances, missing
reasons, weights, scales, numerator, comparable weight, penalties, response value,
source availability, contamination and quality. The exact bounded pool is retained
as IDs/fingerprints, alongside robust scale membership fingerprints. Whole historical
intelligence, event and market-data payloads are not copied into results.

Collection, calculation, persistence and cutoff sealing share one SQLite transaction.
Late backdated feature/response insertion into that policy's frozen cutoff is refused.
Future events/context/corrections append new lineage and cannot alter old results.
An existing query returns its retained result. Changed target revision, context,
feature/distance policy or response cohort creates a different identity.

`AnalogueQueries` exposes `analogues`, `analogues_as_of`, `analogue_distribution`,
`analogue_uncertainty`, `candidate_explanations`, `reaction_surprise` and `run`.
Validation rechecks indexed blocking, exact pinned references, scaling, distances,
selection, distributions and uncertainty. Deep validation additionally reconstructs
feature rows from their retained owners and re-extracts actual study/impact metrics.
Ordinary retrieval does not rerun historical EIE-13 calculations.

See [distance](analogue-distance.md), [reaction comparison](market-reaction-surprise.md),
[uncertainty](analogue-uncertainty.md), and the offline
[example](../examples/historical_analogues.py).


## EIE-15 interaction consumer

EIE-15 event collision/chronology is independent of analogue distance, selected responses and reaction surprise. EIE-14 frozen results remain unchanged. No market response is queried to construct EIE-15 membership, and no interaction score is added to historical distances.
