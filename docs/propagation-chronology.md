# Propagation chronology

EIE-10 provides structural graph candidates. EIE-15 describes subsequent related
canonical events along retained root paths. A later customer event after a supplier
event is an observed chronology candidate; the path and timestamps establish no
causal mechanism or price prediction.

## Root, path and horizon

The root is a retained canonical event with direct scope entities. A query pins an
EIE-10 snapshot rooted in exactly those entities, valid at precise root occurrence
time or root knowledge time when occurrence is coarse/absent. Snapshot knowledge must
be visible at the query cutoff. Graph identity is never supplied by untrusted text.

A `PathReference` preserves snapshot ID/fingerprint, original path ID, ordered nodes,
exact edge revision IDs, relationship types, hops, valid time, knowledge time and graph
qualification. Full edge/source/exposure payloads remain in EIE-10. Future relationships
and edges terminated before the root cannot supply a path. Unsupported relationships
and paths beyond policy hop limits are excluded explicitly by the declared filter.
Truncated graph snapshots are refused.

`InteractionPolicy` declares source-availability, knowledge or precise event chronology,
a finite nanosecond horizon (minutes/hours/days) or a supplied finite session horizon,
allowed relationship types, maximum hops and entry limits. Default depth is two;
the hard maximum is four. The horizon is finite, with no price-based extension.

`PropagationChronology` pins root and later event rows, path, both clocks, primary gap
and qualified descriptive status `SUBSEQUENT_RELATED_EVENT`. The later event must be
known at cutoff and strictly later under the chosen precise basis. Same-time root/event
pairs are collisions, with no later-event claim. Related events outside the horizon
are excluded. Already-direct root entities, root aliases and the root itself do not
become propagated candidates.

## Chains and snapshots

One bounded `PropagationChain` per root groups chronology entries into timestamp layers;
within-layer entries are tied. Each entry retains its direct root path, so multi-hop
structure stays separate from temporal sequence. Simple EIE-10 paths prevent cycles.
This phase performs no recursive generation from an already-derived candidate.

At T1, a saved context may contain only the root; T2 can add one related event and T3
another. The T1/T2 bytes and memberships stay frozen. Later revisions, graph changes,
EIE-11 splits and schedule corrections require new snapshots. Ordinary reads validate
small retained references and recalculation. Deep validation re-collects pinned inputs
and checks exact canonical sources and retained graph paths.

```python
from eie.interactions.queries import InteractionQueries

entries = InteractionQueries(repository).propagation(query)
```

The query's immutable policy declares horizon, relationships and hops; there are no
hidden unlimited defaults. Market reactions, abnormal returns, materiality and reaction
surprise never decide whether an entry exists. Those artifacts remain separately
queryable in EIE-8/9/13/14.
