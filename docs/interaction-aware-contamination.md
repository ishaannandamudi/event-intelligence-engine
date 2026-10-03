# Interaction-aware contamination

EIE-15 `InteractionContext` retains collision, sequence, transition, propagation,
chain and graph-path references, quality and descriptive category counts. It is a
separate historical artifact. Existing EIE-8 study payloads and EIE-9 impact results
are never rewritten.

The mandatory categories are ISOLATED, SAME_SOURCE_BUNDLE, SAME_ISSUER_OVERLAP,
MACRO_OVERLAP, GRAPH_RELATED_OVERLAP, MULTIPLE_OVERLAPS and ORDERING_AMBIGUOUS.
Additional typed categories distinguish SAME_SECURITY_OVERLAP, MARKET_OVERLAP,
SECTOR_OVERLAP and WINDOW_OVERLAP. Counts describe sets of interactions, with no
severity rank. ISOLATED means no collision among the indexed, policy-eligible events;
it makes no claim about source coverage, extraction success or total real-world events.

## Independent dimensions

- Co-occurrence is overlapping information/reaction context.
- Sequence is declared-clock ordering, qualified by precision.
- Structural propagation candidates belong to EIE-10.
- Subsequent related events belong to EIE-15 chronology.
- Empirical reaction and counterfactual residuals remain EIE-8/9 artifacts.
- Information novelty/materiality and analogue reaction surprise retain EIE-13/14 owners.

No sum, maximum or weighted aggregate of colliding-event materialities is supplied.
Individual HIGH importance, MAJOR materiality and other source-derived categories stay
per event. Bundle movement is not allocated to earnings versus buyback versus CEO
changes. No causal assertion or trading direction follows from the category set.

The offline [example](../examples/event_interactions.py) uses real EIE-11 retained
sources and independent EIE-13 materiality results, a scheduled macro event and a
supplier/customer path. It demonstrates extended chronology and byte-identical restart.
The [profile](../benchmarks/EIE-15-profile.json) measures indexed candidate collection,
pair/sequence/chronology calculation, persistence, validation and ordinary restart reads.
