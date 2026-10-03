# Entity relationship graph

EIE-10 adds a source-backed, bitemporal graph in `eie.graph`, with relationship schema
`1.0`. A relationship is a retained structured assertion. Its presence does not establish
that an event causes a price move. Exposure, propagation candidates and measured EIE-9
impact remain separate contracts.

## Nodes and identity

`GraphNodeType` includes issuer, legal entity, security, listing, ETF, index, sector,
industry, fund and government agency. `foundation_node` preserves existing EIE-3 issuer,
security and listing IDs and their verified foundation contracts. Securities require a
known issuer; listings require a verified security/issuer link. A security or issuer
cannot be rebound to another issuer or CIK. Later labels and exact identifier aliases
are new node revisions.

Other nodes use a deterministic hash of type, identifier authority and sourced stable
identifier. An explicit `identity_identifier` keeps a graph-specific node stable when
new aliases sort before its original identifier. Ticker, symbol and display name are
rejected as identity authorities. Classification provider, taxonomy and version must
be represented in the supplied node identifier/edge qualifiers; identical labels never
merge two taxonomies.

Endpoint resolution accepts explicit node ID, normalized CIK, audited provider ID or an
exact historical symbol plus exchange. Ambiguity yields an unresolved claim. Symbol
resolution pins its valid time and uses retained EIE-3 listing revisions; an optional
SEC registry must agree. There is no fuzzy matching or extraction from prose.

## Relationship taxonomy and direction

| Canonical stored direction | Accepted inverse or meaning |
| --- | --- |
| `PARENT_OF`: parent → child | `SUBSIDIARY_OF`: child → parent |
| `SUPPLIER_TO`: supplier → customer | `CUSTOMER_OF`: customer → supplier |
| `COMPETITOR_OF`, `PEER_OF` | Symmetric; endpoints sorted by stable ID |
| `SECTOR_MEMBER_OF`, `INDUSTRY_MEMBER_OF` | Entity → classification node |
| `INDEX_MEMBER_OF` | Issuer/security/ETF → index |
| `ETF_HOLDS` | ETF → security/ETF |
| `OWNS_EQUITY_IN` | Owner → organization/security; inverse `OWNED_BY` |
| `SPINOFF_FROM` | Spun entity → original entity; no current ownership implied |
| `SUCCESSOR_OF` | Successor → predecessor; inverse `PREDECESSOR_OF` |
| `ISSUER_ISSUES_SECURITY` | Verified issuer → its security |
| `SECURITY_HAS_LISTING` | Verified security → listing; inverse `LISTING_REPRESENTS_SECURITY` |

Inverse inputs normalize to one canonical edge. Query direction is relative to the
canonical direction, including when a filter uses an inverse spelling. Symmetric
relations appear in both incoming/outgoing neighbor searches. Shared industry does
not generate competitors. An M&A announcement does not establish ownership/control.
Corporate structural relations require an explicit source/effective assertion and may
carry an existing corporate event/revision ID as a sourced qualifier.

## Claims, canonical edges and revisions

`ProviderRelationshipClaim` has a source/resource/key identity and an append-only revision
chain. It retains the original typed item, raw observation, source class, exact resolved
endpoints, dataset identity, knowledge clock and predecessor. Unresolved claims remain
queryable but do not create canonical edges.

`RelationshipEdge` has a semantic ID from canonical endpoints, relationship type and
sourced qualifiers. Exposure and dates do not change that ID. Its revision fingerprint
pins selected claim, ordered alternatives, policy, conflicts, descriptive confirmation
count and quality flags. Endpoint or classification corrections can move the same claim
thread to a different semantic edge while retaining both historical versions.

Source precedence reuses `SourcePrecedencePolicy`, including `graph.<relationship>`
overrides. Within a class, source/resource/key ordering is deterministic. Distinct
providers agreeing on state/exposure/quantity contribute confirmations; dates and
state/exposure disagreements remain explicit. No source is averaged into a score or
numeric weight. An inactive preferred claim may retain an active conflicting alternative;
relationship searches expose that state, while adjacency uses active canonical edges.
`exclude_conflicted_traversal` permits a conservative traversal policy.

## Two clocks and source snapshots

Validity uses half-open intervals `[valid_from, valid_to)`, exact snapshot points, or
explicit unknown validity. Open end means no sourced end; it is flagged and may be used
under the declared interval policy. Unknown start is preserved without inventing a date
and is excluded from exact point-in-time edges. Knowledge begins at EIE ingestion, never
at a retroactive effective date. Knowledge ends are derived from later visible claim
revisions rather than updates to old rows. See [graph replay](graph-replay.md).

Composition snapshots default to `POINT_ONLY`. Explicit `CARRY_UNTIL_NEXT` treats weights
as carried intervals and marks that assumption. A later listed member closes its prior
weight episode. Missing members close only with an exhaustive snapshot, `remove_missing`
and provider permission together. Partial snapshots preserve omitted members. ETF
removal is restricted to the owner's outgoing holdings, including nested ETF cases.
Each episode preserves its prior weights and valid interval; each termination is a new
knowledge revision. Unresolved exhaustive compositions cannot imply removal.

## Queries and traversal

`GraphQueries` provides `relationships_as_of`, `neighbors`, `parents`, `subsidiaries`,
`suppliers`, `customers`, `competitors`, `index_memberships` and `etf_exposure`. Both
`known_as_of_ns` and `valid_at_ns` are explicit. Relationship searches filter node, type,
provider and exposure. A provider filter deliberately selects that provider's claims.

`GraphHistoryQuery` searches retained revisions by node/type/provider/exposure and
optional overlapping valid/known intervals. It returns claims with their cutoff-aware
knowledge ends; it does not present those revisions as one canonical current truth.
Unknown validity is excluded when a valid interval filter is supplied. Snapshot points
match a requested half-open interval only when the point lies inside it.

Traversal uses indexed local adjacency and a bounded per-call cache. `UNIQUE_NODES`
returns the first deterministic breadth-first discovery path and its tree edges;
it is not an induced subgraph. `SIMPLE_PATHS` retains distinct paths without repeating a
node on a path. Multiple roots are sorted; direct roots are excluded from candidates.
Adjacency and outputs are sorted by stable IDs. Parent cycles are rejected at every
historical valid-time boundary under the ingestion resolution policy. Real supply and
ownership cycles are allowed and terminate safely during traversal.

Depth is at most 8. Hard caps are 10,000 nodes, 20,000 edges, 20,000 paths and 100,000
adjacent input claims. Default limits are smaller. A conservative serialized-component
budget bounds result bytes (default 20 MB, maximum 50 MB). Exhausting a node/edge/path/
adjacency/byte budget produces an explicit truncation reason. Roots whose evidence cannot
fit fail instead of returning an invalid snapshot. Depth is a requested scope, not a
truncation warning. Bounded history searches fail when a complete answer cannot fit.

## Boundaries

The structured adapter accepts audited data shaped as `EIE_RELATIONSHIPS_V1`; native
SEC relationship extraction, commercial connectors, document NLP and live graph polling
are not implemented. Completeness and source truth are assertions, not independently
authenticated facts. Pure artifact checks prove internal consistency; repository checks
also recalculate from retained historical claims. See [exposure](exposure-graph.md),
[propagation](event-propagation.md) and [persistence](persistence.md).

## EIE-11 claim/evidence boundary

The exposure graph and claim/evidence graph retain separate models and traversal semantics.
Evidence claims can reference existing relationship edge/node IDs and typed exposure values;
this does not add inferred relationships, compose exposures or alter direct event scope.
Source-family corroboration and field-level unresolved resolution belong to the evidence
layer. EIE-10 behavior remains stable. See [claim/evidence](claim-evidence-graph.md).

## EIE-13 graph context

Entity novelty accepts an explicitly pinned graph snapshot strictly before the
intelligence cutoff. Materiality accepts an available current snapshot and only
direct active edges touching event entities. Exposure type, bounds, estimates,
conflicts and graph truncation remain explicit. Missing exposure is unknown; no
indirect propagation weight or permanent relationship is inferred from text.
Future disclosures cannot alter a saved intelligence result. See
[materiality intelligence](materiality-intelligence.md).

## EIE-14 supplied graph conditioning

An explicitly requested retained snapshot can supply exact sector/industry/relationship categories at the candidate event cutoff. Snapshot knowledge cannot follow that cutoff, and valid time must equal it. Conflicted, ambiguous, unresolved and truncated memberships cannot establish a category. Other retained graph qualifiers remain visible on the feature row. No new relationship inference or sibling integration is introduced. See [historical analogues](historical-analogues.md).
