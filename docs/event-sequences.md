# Event sequences and state transitions

`EventSequence` retains compact event row/revision references, its subject or context
identifier, range, knowledge cutoff, ordering basis, timestamp layers, unordered
members, positive primary elapsed gaps, signed secondary clock differences and quality.

## Two clocks and partial order

Occurrence chronology uses the retained effective/announcement/publication timestamp
or an EIE-11 resolved event-time assertion. Schedule chronology uses explicit source
schedules. Source availability uses retained raw acquisition clocks; knowledge time
uses canonical revision availability, including extraction delay. None is silently
substituted for another. Retroactive disclosure can reverse occurrence and information
orders. `SequenceStep.event_gap` and `knowledge_gap` are signed nanosecond differences;
`elapsed_ns` is positive under the sequence's declared primary basis.

Exact timestamps form ordered layers. Members of a same-time layer remain tied, with
no within-layer edge. Missing and coarse occurrence values are explicitly unordered.
Two date-only events cannot supply precise chronology even when their intervals overlap.
Stable IDs are used only for deterministic layer serialization. Precise singleton
neighboring layers can provide typed PRECEDES steps; source/evidence references remain
available through retained rows. Public sequence queries filter exact entity membership
and optional event codes before constructing the artifact. A context also retains
separate occurrence and knowledge sequence views over its bounded candidate universe.

```python
from eie.interactions.queries import InteractionQueries
from eie.interactions.schema import OrderingBasis

sequence = InteractionQueries(repository).sequence(
    dataset,
    issuer_id,
    start_ns,
    end_ns,
    cutoff_ns,
    policy_id,
    OrderingBasis.KNOWLEDGE_TIME,
    event_resolution_id=alias_run_id,
)
```

## Structured lifecycle

State transitions require one explicit stable thread and sourced structured state.
Canonical `corporate_id` or `interaction_thread` facts identify a thread; a company
name or shared issuer alone does not. Supported domain states are closed categories:

- M&A: RUMORED → ANNOUNCED → APPROVED → CLOSED.
- Guidance: ISSUED → REVISED → WITHDRAWN.
- Management: DEPARTURE_ANNOUNCED → DEPARTURE_EFFECTIVE → REPLACEMENT.

The generic canonical lifecycle uses the existing EIE-0 transition validator. Invalid
domain progression and mixed-family threads fail. Simultaneous state records cannot
be selected by stable ID as a prior state for a later progression. Structured
`interaction_state`/corporate status is consumed from the canonical owner; no headline
or model inference supplies a lifecycle state.

Corrections use CORRECTION_OF; retractions/cancellations use RETRACTION_OF. Neither
means real-world completion. Transition references pin source revisions, knowledge
time, effective precision and from/to state. `state_transitions(dataset, thread,
cutoff, policy)` returns bounded immutable artifacts. It does not alter any source
claim, confirmation state, prior event revision or earlier sequence.
