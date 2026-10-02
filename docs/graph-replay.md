# Bitemporal graph replay

Every point query supplies two UTC nanosecond timestamps:

```python
edges = GraphQueries(repository).relationships_as_of(
    known_as_of_ns=30,
    valid_at_ns=10,
    node_id=issuer_id,
)
```

The small timestamps are synthetic logical clocks; production timestamps use the
existing nonnegative signed-64-bit UTC nanosecond convention.

## Validity and knowledge

A relationship effective in 2024 but ingested in 2026 is absent from a 2025 knowledge
view even when querying 2024 validity. A later termination or correction adds a new
revision with a new ingestion clock. The latest claim revision available at the requested
cutoff is selected first, then its validity/state is evaluated. Future corrections do
not supply earlier knowledge end boundaries.

An ended or retracted claim remains available as inactive canonical state when applicable,
so conflicts are inspectable. It is excluded from adjacency and propagation. A snapshot
point is applicable only at its exact reference point. Unknown valid start does not
become ingestion time. Coarse/reconstructed source semantics must retain their quality
labels and must not be represented as proven historical observations.

`claim_history` returns a bounded ordered provider chain; `relationship_history(edge_id,
known_as_of_ns)` returns its bounded semantic-edge history across sources. An endpoint
correction may move one provider claim to a new semantic edge. `search_history` with
`GraphHistoryQuery` exposes overlapping half-open valid/knowledge intervals and the
derived knowledge end visible at the cutoff.

## Append ordering and sealed cutoffs

The graph ledger accepts globally nondecreasing knowledge clocks across nodes, claims
and source items. Within a node/claim/resource stream, revisions must advance strictly.
After a source item commits, all new graph writes must advance past that item's knowledge
clock; another provider cannot extend its already completed cutoff at the same timestamp.
An import must merge source/node histories in knowledge order; imported foundation
metadata must be available before its edges. A local acquisition cannot be backdated
to a publication/effective time.

Persisting a `GraphSnapshot` seals its knowledge cutoff. New node/claim/source-item
writes must be strictly later than the maximum sealed cutoff, including empty source
items. Existing identical writes remain idempotent. This prevents a late backdated
addition from changing the inputs used to recalculate a frozen snapshot. Later updates
and retrospective queries remain supported. For a historical reconstruction with a new
parser or dataset, build a separate ordered ledger instead of inserting older clocks
into a sealed one.

## Historical symbols and builds

Symbols require an exact exchange and retained EIE-3 listing revisions at both times.
A renamed ticker or reused ticker keeps its original issuer/security/listing IDs in old
claims. Already resolved claims do not get rebound by a later ticker map. Ambiguous or
unavailable mapping remains unresolved until an explicit later claim revision.

`GraphDataset` pins dataset ID, version and fingerprint. An optional EIE-7 `build_id`
must reference an existing immutable manifest with the same content fingerprint.
External dataset fingerprints are supplied assertions. The structured adapter does not
infer source historical knowability from native SEC submissions or current holdings.
See [dataset manifests](dataset-manifests.md) and [completeness](data-completeness.md).

## Determinism, restart and validation

Semantic IDs and revision IDs use versioned fingerprints; no random identity or implicit
wall clock is involved. A snapshot includes sorted roots, nodes, exact canonical edge
revisions, ordered paths, both timestamps, source/traversal policies, bounds, truncation,
schema and processing version. Propagation pins that exact snapshot and source event
revision. `snapshot_diff` reports added, removed and changed semantic edge revisions.

Raw observation, all normalized claims, selected canonical revisions and source-item
checkpoint commit in one SQLite transaction. A failure leaves no partial graph batch.
Receipts pin the original typed source item, provider/parser/dataset/policy, unresolved
and conflict counts and immutable generation. Rebuilding the same typed observations
with the same versions and policy reproduces receipt, edge, snapshot and candidate bytes.

Pure validation checks retained claims, paths, temporal semantics and source resolution.
Repository reads additionally check scalar indexes, foreign references, snapshot
membership and complete historical recalculation. Returned scalar-index disagreement,
missing persisted members and forged payload references fail validation. SQLite
update/delete protection covers all graph tables. Artifact validation and sealing share
the same write transaction, preventing a second connection from inserting between them.
Neither validation mode authenticates a vendor's real-world completeness or proves
that a supplied canonical event had been committed to an external event dataset.
