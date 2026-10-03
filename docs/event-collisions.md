# Event collisions (EIE-15)

Engine 1.6.0 introduces event-interaction schema 1.0. A collision describes distinct
known events sharing a potentially relevant information/reaction window. It establishes
co-occurrence, with no causal attribution, reaction allocation or combined severity.

## Inputs and immutable identity

`InteractionRepository.index_event(dataset, event_id, known)` projects the visible
canonical revision and retained EIE-11 timing claims into a small immutable row.
The row pins event/revision IDs and fingerprint, issuer/security scope, source bundle
observation IDs, occurrence/schedule precision, acquisition availability, EIE knowledge
availability and timing assertion references. No title, source document, numeric claim,
materiality or market-response payload is copied into a collision.

The dataset is an explicit interaction index namespace. Indexing is a trusted ingestion
step; a canonical event and its raw observations must already exist. The layer does not
create event existence or resolve company names. EIE-7 source projections must first
enter the retained canonical/evidence owner with their existing reconstruction lineage;
this phase does not introduce a second backfill reader or rewrite that lineage.

EIE-11 retained event resolution runs own aliases. A query can explicitly pin a run or
select the latest stored run visible at its cutoff. Its exact groups/fingerprint are
retained. Same-event revisions are selected before time filtering. Alias groups count
once; later splits affect separate snapshots. With no stored run, only canonical event
identity is excluded; callers should provide EIE-11 resolution for cross-source dedupe.
There is no fuzzy match, transitive merge or severity-based identity rule.

## Window policy

`InteractionWindow` pins a named ordering basis, pre/post nanoseconds, elapsed or
supplied trading-session clock, a coarse-time permission and a version. Type overrides
precede family overrides; each can replace durations and the timestamp basis. For an
issuer occurrence beside a scheduled CPI release, explicitly use a `MACRO` override
with `SCHEDULED_TIME`. This retains occurrence and anticipated schedule as distinct clocks.
Unknown time has no temporal window; a shared source bundle can still establish a
bundle interaction.

Exact windows intersect inclusively. Date-only/session values supply a UTC date
interval only when coarse overlap is permitted. A date/session label supplies no exact
time, time difference or within-day order. Supplied calendars are ordered,
nonoverlapping and bounded; session offsets reuse EIE-8 trading-time arithmetic.
Historical calendar metadata must already be available at the relevant anchor.

## Typed outputs

Categories: SAME_ISSUER, SAME_SECURITY, SAME_SOURCE_BUNDLE, MACRO_ISSUER,
SECTOR_ISSUER, MARKET_ISSUER, GRAPH_RELATED, SAME_TIMESTAMP, OVERLAPPING_WINDOW and
SEQUENTIAL_RELATED. One pair can carry several categories. Pair IDs canonicalize the
symmetric left/right row references. Precise same-basis pair relations may additionally
say PRECEDES/FOLLOWS; differing bases provide no directional relation. Identical
precise timestamps are tied. Stable IDs serialize the tied set only.

Macro/central-bank and broad-market overlap requires existing typed canonical events.
No event is invented from prices. Sector/industry and graph overlaps require a pinned
EIE-10 path at the root's valid time and a visible knowledge cutoff. Exact edge revisions
are retained as compact references. Shared bundles preserve distinct earnings, buyback
and management events, common acquisition availability and indivisible contamination.

## Atomicity, limits and query

```python
from eie.interactions.engine import InteractionEngine
from eie.interactions.models import InteractionQuery

context = InteractionEngine(repository).interaction_context(
    InteractionQuery(dataset, event_id, cutoff_ns, policy_id, graph_id)
)
```

Time range, entity adjacency, scope/macro family and source-bundle indices block
candidates before pair construction. The layer compares each target only with its
bounded candidates; it never compares all historical pairs. Pair, sequence, graph path,
chronology, hop, chain, run and serialized-byte limits fail explicitly. No arbitrary
prefix is presented as complete. Collection, calculation, child persistence, context
membership and cutoff sealing share one owned SQLite transaction.

Identical requests return the frozen result. A new cutoff, graph snapshot, explicit
alias run or policy yields separate lineage. Backdated index additions at or before a
sealed dataset cutoff are refused; new historical input requires a new index namespace.
