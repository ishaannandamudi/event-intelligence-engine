# Graph-derived event propagation candidates

`propagate` generates related entities to evaluate. It preserves the supplied canonical
event's literal scope and creates a separate `PropagationResult`; it never rewrites
direct applicability. A candidate is not a prediction, expected price direction,
recommendation or trading instruction.

## Binding the direct scope

Company, security, index, sector and industry scopes bind exact known graph nodes of
the corresponding type. A security scope may bind a verified security or ETF. Symbol
scopes require exact historical listings plus exchange and retain the listing evidence
in the binding. If an optional EIE-3 registry is supplied, it must agree. Unknown or
ambiguous entity scopes fail instead of guessing.

Macro-global, macro-country, market and SEC-filer scopes return an explicit unexpanded
result. They are not automatically mapped to every company. A future sourced macro
exposure policy would require its own typed evidence and is outside EIE-10.

The event revision must already be available at the graph knowledge cutoff. Both
`known_as_of_ns` and `valid_at_ns` are explicit. Research at an event cutoff must use
the historical knowledge cutoff, not a later corrected graph. Event effective time
and the appropriate graph valid time remain choices made explicitly by the caller.

## Paths and candidate meaning

Each candidate retains the source event/revision, entity ID, exact graph snapshot ID,
ordered paths, minimum hop count and descriptive reason
`GRAPH_RELATED_ENTITY_FOR_EVALUATION`. Paths pin every sourced edge revision, its
quality/conflicts and any compatible derived ownership exposure. One candidate combines
all retained paths to that entity. Direct roots remain in `bindings`/`direct_entities`
and are excluded from the candidate list even when another root can reach them.

Traversal selects relationship types, canonical direction, depth, unique-node versus
simple-path mode, conflict policy and bounded node/edge/path/adjacency/byte limits.
Depth defaults to two. A supplier search does not silently expand through unrelated
holdings or industry peers. Cycles cannot revisit a node on a path. Truncation is carried
from the snapshot to the result, so omitted candidates are not represented as absence.
The complete propagation artifact has a 50 MB serialized cap; oversized root evidence
or a complete result that cannot fit fails explicitly.

## Example and persistence

Run `PYTHONPATH=src python examples/entity_relationship_graph.py` from the repository
root. The synthetic example ingests supply, parent, classification, security, index and
ETF facts, queries an earlier cutoff, ingests a later termination correction, proves
the earlier graph unchanged, calculates a sourced 80% × 50% ownership path, generates
related candidates, persists artifacts and verifies byte-identical restart.

`GraphRepository.append_propagation` validates the complete historical snapshot and
scope/candidate recalculation before atomic persistence. It retains the supplied canonical
event in the immutable artifact; it does not fetch events, replace their evidence, or
claim external event-store authentication. `validate_propagation(result, repository)`
checks the exact source scope, cutoff, candidate uniqueness and path consistency.

Observed raw studies and model-dependent abnormal returns remain in the separate EIE-8
and EIE-9 packages. They are not imported to select graph edge strength or direction.
No impact-derived weights, causal attribution, automatic aggregate exposure or trading
functionality are added.


## EIE-15 interaction consumer

EIE-15 now records subsequent canonical events along pinned EIE-10 root paths under explicit finite horizons and hop/entry limits. Structural paths and temporal event chronology are separate. Ties remain ties, cycles use bounded simple paths, and direct parties/root aliases are excluded. See [propagation chronology](propagation-chronology.md).
