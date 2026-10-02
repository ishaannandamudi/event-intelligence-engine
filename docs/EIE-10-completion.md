# EIE-10 — Entity Relationship & Exposure Graph

Date: 2026-10-02. Repository: `event-intelligence-engine`.
Starting state: clean working tree at `a680f45`, engine `1.0.0`, store `1.3`,
1,159 passing tests. Changes remain in the current working tree. No commit or push.

## A. Architecture

Configured structured source → raw observation → immutable provider relationship
claims → deterministic categorical canonical resolution → indexed two-time queries
→ bounded graph snapshots/paths → separate related-entity event candidates.
Raw/source-item/claims/canonical revisions/checkpoint commit atomically. Relationships,
sourced exposure, derived paths and measured impact are independent concepts.

## B. Package structure

`src/eie/graph` has 13 modules: schema entry point, taxonomy, nodes, exposure, claims,
models, history, queries, traversal, propagation, engine, repository and validation.
`src/eie/providers/relationships` adds provider/batch contracts, static bounded adapter
and normalization. A runnable example, realistic profile, seven test suites, shared
fixtures and four graph guides accompany the code.

## C. Graph schema

Relationship graph schema `1.0`. It is independent of the event, entity, source,
configuration, runtime, ingestion, historical, study and impact schemas.

## D. Node model

Ten explicit node types cover issuer, legal entity, security, listing, ETF, index,
sector, industry, fund and government agency. Foundation nodes reuse exact EIE-3
contracts/IDs and CIKs. Graph-specific nodes use stable sourced authority/identifiers.
Primary identifiers preserve identity when aliases are added. No ticker or label identity.

## E. Edge model

Immutable `RelationshipEdge` pins canonical direction/endpoints, selected provider
claim, all applicable alternatives, qualifiers, categorical resolution policy,
confirmation count, conflicts and descriptive quality. Exposure is selected sourced
data and remains unknown when missing. Provider claims retain independent raw provenance.

## F. Relationship taxonomy

All 15 requested families plus verified issuer/security/listing links are supported.
Parent/subsidiary, supplier/customer, ownership/owned-by, successor/predecessor and
security/listing inverses normalize to one direction. Competitor/peer edges are
explicitly symmetric and sorted; classification membership does not create peers.

## G. Edge identity

Semantic edge identity hashes canonical endpoints/type and sourced qualifiers.
Dates, weights and corrections create new revisions of the same semantic edge;
endpoint/classification corrections can move a provider claim thread to a new edge.
Provider claim identity is source/resource/key; all revision and artifact IDs are
deterministic versioned fingerprints. No UUIDs or implicit wall clock.

## H. Valid-time semantics

Half-open effective intervals, exact snapshot points and unknown validity are explicit.
Open end retains uncertainty without inventing termination. Unknown start does not enter
exact point-in-time adjacency. Measurement reference time is independent of validity.

## I. Knowledge-time semantics

Knowledge starts at actual ingestion. Later visible revisions derive knowledge ends;
future revisions cannot leak their boundaries into an earlier search. Graph imports
follow knowledge order. Writes advance past completed source-item clocks and sealed
snapshot cutoffs, preventing later additions from changing already completed views.

## J. Revisions and corrections

Initial/update/correction/termination and retraction preserve raw source and prior claim
chains. Retroactive dates do not backdate knowledge. Endpoint corrections preserve
earlier wrong claims and graph views. ETF weight closures preserve the old weight in
its earlier interval rather than assigning the new weight backward.

## K. Source claims and conflicts

Existing source classes/precedence are reused. Source-specific alternatives, state/
exposure/quantity disagreement, validity disagreement and equal-precedence conflict
remain explicit. Agreement counts distinct providers. Deterministic tie ordering does
not erase alternatives. Optional conflict exclusion is part of traversal policy.

## L. Exposure model

Finite typed fractions, exact points or inclusive/exclusive ranges, reference/source,
estimated flag and explicit ordinary/signed domains. Percent conversion is explicit.
Ownership/index ordinary fractions validate `[0,1]`; signed portfolio semantics require
provider opt-in. Holding shares/value/currency are separate. Unknown is never zero.

## M. Parent and subsidiary

Organization endpoints are validated. Sourced ownership/voting/economic measurements
are permitted. Iterative hierarchy validation rejects cycles at every valid-time
partition under the ingestion resolution policy, including retroactive intervals.

## N. Supplier and customer

Canonical supplier → customer direction, incoming supplier and outgoing customer
queries, sourced revenue/purchase share and bounded structured qualifiers. Unresolved
customer identity remains a claim. Supply cycles are allowed and traversal is bounded.

## O. Competitor and peer

Symmetric sourced relations have one deterministic edge identity. Provider-defined
peers are retained explicitly; same classification does not infer competition.

## P. Sector and industry

Separate typed membership edges and stable provider/taxonomy/version identity. Later
classification changes are new source revisions; labels do not merge taxonomies.

## Q. Index membership

Issuer/security/ETF → stable index node, exact or carried snapshot semantics, optional
sourced index weight, revisions and explicit effective/removal dates. Index snapshots
share the conservative composition completeness/removal policy.

## R. ETF holdings

ETF → security/ETF with supplied portfolio weight and optional shares/market value.
Default point-only policy avoids indefinite holdings truth. Explicit flagged carry and
exhaustive removal preserve weight history. A nested ETF's incoming holdings are not
closed by a snapshot of its own outgoing portfolio.

## S. Ownership, spinoff and successor

Sourced ownership/control and historical spinoff/successor edges are supported with
existing entity IDs and optional corporate event/revision qualifiers. Deal announcements
or generic completion are not automatically converted into control. Structural succession
does not imply current ownership. No corporate prose extraction is added.

## T. Snapshot semantics

Composition scope names owner/type/effective point and completeness. Partial snapshots
cannot remove missing members. Exhaustive removal requires both a source-item request
and configured provider permission under carry semantics. Unresolved full compositions
fail removal; failures roll back raw observation, all edges and checkpoint together.

## U. Graph queries

Explicit knowledge/valid point queries, neighbors, parents/subsidiaries, suppliers/
customers, competitors, index and ETF queries. Provider/type/entity/exposure filters are
typed; bounded interval searches return retained revisions with cutoff-aware knowledge
ends. Inverse taxonomy filters also retain searchable unresolved inverse claims.

## V. Traversal

Sorted breadth-first local indexed adjacency and a bounded per-call projection.
Unique-node mode returns discovery tree paths/edges; simple-path mode preserves distinct
paths and never repeats a node on a path. Explicit node/edge/path/adjacency/serialized-byte
truncation; depth ≤8 and conservative component accounting prevent path explosion.

## W. Paths and exposure composition

Only forward compatible ownership fractions multiply into a separately labeled derived
path. The example's 0.8 × 0.5 = 0.4 pins exact input edge revisions. Range bounds are
conservative; unknown stays unknown and underflow cannot become zero. Voting, supply,
holdings, index, reverse and mixed measurements do not compose. Alternate paths are not
summed into total exposure or promoted into canonical facts.

## X. Event propagation candidates

Original canonical direct scope remains unchanged. Exact entity or historical symbol
bindings seed bounded traversal; candidates deduplicate entities while retaining all
paths, source event/revision, hop count and exact graph snapshot/cutoff. Direct roots
are excluded from candidate lists. Macro/market/filer scopes are explicitly unexpanded.
No predicted movement, sign, rank, recommendation or trade is generated.

## Y. Persistence and migrations

Additive transactional migration 5 raises SQLite `1.3` → `1.4`. Twelve graph tables have
foreign references, required adjacency/history/exposure/snapshot indexes and update/delete
refusal triggers. Tests retain old study/impact payloads and dataset manifests exactly
and prove failed DDL rollback. Source-item receipts double as immutable construction/
checkpoint lineage. Snapshots retain exact node/edge revision membership.

## Z. Replay and determinism

Independent raw fixture rebuilds reproduce receipt/snapshot/propagation bytes. Different
Python hash seeds reproduce the example. Corrections do not alter prior knowledge views;
verified listing changes/reuse cannot rebind old endpoints. Persisted snapshots and
candidates recalculate byte-identically after restart.

Example: 8 nodes, 8 edges, 9 paths, 7 related candidates, derived ownership 0.4;
historical snapshot unchanged and restart byte-identical. Snapshot fingerprint:
`3ffe6a3fb8b274440eb5868324b91ccc57af8db09df8199613fc63500bbd8998`.

## AA. Validation

Pure checks cover claim chains, exposure, temporal applicability, canonical resolution,
path membership/direction/composition, event binding and candidate consistency. Repository
checks add raw/source/parser/dataset/item provenance, scalar-index/payload agreement,
references, exact snapshot membership, counts and complete indexed historical recalculation.
Malformed inputs, forged artifacts and incomplete historical readers fail closed.
Validation and snapshot/candidate sealing share one write transaction. A second SQLite
connection with zero busy timeout proves it cannot insert between validation and sealing.

## AB. Performance

The profile uses 2,000 foundation nodes, 6,200 semantic edges, 6,500 provider revisions
and two source items in a cyclic supply network with a sourced ownership chain. It measures
100 adjacency queries, 100 depth-two traversals, one ownership path, 20 propagations,
validation, artifact storage and fully checked restart. It is representative local scale,
not a claim of 100,000-node throughput. No CI timing threshold.

## AC. Security

Static allowlisted typed JSON; no provider-driven import, executable deserialization,
shell, filesystem access or network in the graph adapter. Payload/item/identifier/qualifier/
adjacency/path/result bounds apply. Trust/capabilities/carry/removal/signed-weight permission
come from configuration. Raw observations retain hashes/metadata, not automatic native
vendor archives. Normalized-field retention still requires caller entitlement under
existing acquisition/archive policies. No runtime dependency added.

## AD. Documentation

Created [relationship graph](entity-relationship-graph.md), [exposure](exposure-graph.md),
[graph replay](graph-replay.md) and [event propagation](event-propagation.md). Updated
README, master specification, architecture, entity identity, replay/causality, dataset
manifests, source policy, corporate events, counterfactual impact and persistence.
The example and benchmark are linked and runnable from the repository root.

## AE. Tests added

Added **186 test cases**. Seven graph suites cover contracts, two-time history/conflicts, composition snapshots,
identity/listing reuse, traversal/propagation, storage/migration/corruption and independent
safeguards/rebuild. They exercise every relationship family, percentage/range/unknown
exposure, cyclic paths, all truncation modes, atomic failures, retrospective corrections,
sealed clocks, exact build linkage and all twelve graph-table immutability triggers.
Existing tests change only expected engine/store version strings.

## AF. Full pytest

**1,345 passed in 35.24 s**, including all 1,159 existing cases.
Socket-blocked branch-coverage run: **1,345 passed in 95.99 s**, with **zero network
connection attempts**. The migration formatting issue discovered during review was fixed
before these final runs; all gates were rerun after the transaction race fix.

## AG. Coverage

Branch-enabled combined coverage: **96.47%** (17,664 covered statement/branch opportunities
out of 18,311), exceeding the 95% gate. New graph and relationship-provider group:
**95.36%** (2,157/2,262). Claims, exposure, taxonomy, queries, traversal and adapter are
fully covered; history, snapshot/conflict/causality and validation paths have strong
coverage. No new coverage suppression is introduced.

## AH. Ruff

Lint passes. Format check passes for all 292 checked files. No lint suppression added.

## AI. Mypy

Strict mypy passes all 187 source files. Public graph/provider contracts and the SQLite
reader use explicit types; no untyped graph dictionary or runtime dependency is introduced.

## AJ. Diff review

`git diff --check` passes. Reviewed the tracked diff plus every new implementation,
fixture, example, profile, test and guide (21 modified files and 34 new files).
Earlier migration statements remain unchanged; prior test edits only update version
expectations. Git HEAD remains `a680f45`; no commit/push or sibling repository edits.

## AK. Secret and unfinished-marker review

Credential/private-key patterns across 311 files find no secrets. No new actionable unfinished markers
or trailing whitespace. Two existing documentation matches describe the mandatory
review gate and the EIE-5 historical completion scan; neither is unfinished work.
Graph source contains no executable deserialization, provider-controlled imports,
transport, shell calls or impact-driven weights. Local documentation links resolve.

## AL. Benchmark profile

Recorded offline sample: [machine-readable profile](../benchmarks/EIE-10-profile.json).
Timings are local measurements, with no CI threshold or scale extrapolation.

| Operation | Measured time |
| --- | ---: |
| 2,000 node inserts | 0.214 s |
| Atomic ingestion/storage of 6,500 claim revisions | 154.984 s |
| 100 point-in-time adjacency queries | 0.574 s (mean 5.745 ms) |
| 100 depth-two traversals | 8.348 s |
| Compatible two-hop ownership path | 0.0125 s |
| 20 propagation results | 3.971 s |
| 20 snapshot + 20 propagation validations | 5.391 s |
| 20 propagation artifacts + ownership snapshot persistence | 11.247 s |
| Restart and fully validated queries/receipt reloads | 83.747 s |

Database plus WAL at the open-store sample: **194,440,192 bytes**. All reloaded
fingerprints verified. The source-item fixture has 2,000 nodes, 6,200 semantic edges,
6,500 claim revisions and two source items. Full evidence retention/revalidation is
the dominant cost; adjacency/traversal remain indexed and local.

## AM. Versions

Engine `1.1.0`; relationship graph schema `1.0`; SQLite store `1.4`. All earlier public
schemas remain `1.0`. Python 3.11+ remains required. Existing stored payloads are not
rewritten when the processing version advances.

## AN. Dependencies

Zero runtime dependencies. Standard-library SQLite, immutable dataclasses, enum, hashing
and deterministic JSON are sufficient. Existing pytest/coverage/Ruff/mypy development
tools are reused. No graph database, networkx, market fetcher or downstream integration.

## AO. Remaining limitations

- Source truth, completeness, entitlement and external dataset fingerprints are supplied
  assertions. Native SEC relationship extraction, vendor connectors and graph polling
  are not implemented.
- Imports must merge histories in knowledge order; completed item clocks and persisted
  snapshot cutoffs reject backdated writes. A new retrospective reconstruction needs
  a separate ordered ledger and lineage.
- Pure validation checks retained evidence; complete repository validation also checks
  retained historical adjacency. Neither authenticates an external event store or proves
  real-world source completeness.
- Parent validation is bounded and relative to ingestion resolution policy. Consumers
  changing source precedence should validate the resulting hierarchy under that policy.
- Carry is an explicit assumption. Point-only sources remain point-only. Missing or
  ambiguous identities require a later explicit revision.
- Unique-node traversal returns discovery tree edges, not all induced edges. Simple-path
  mode is bounded; truncation does not certify absence.
- Ownership products are derived path calculations. No total exposure aggregation,
  portfolio look-through, empirical edge weighting or predictive interpretation.
- Immutable full evidence is stored redundantly for audit/recalculation. Large source
  receipts and exhaustive restart validation cost substantially more than local queries.

## AP. Exact proposed EIE-11 scope

EIE-11 is **not implemented**. Proposed next phase: Multi-Source Deduplication and
Claim/Evidence Graph, limited to structured, source-backed evidence:

1. Stable claim identities and claim-level evidence references/spans.
2. Supporting/contradicting claim relationships and source-item links.
3. Multi-source event duplicate candidates and deterministic resolution.
4. False-merge/false-split safeguards with explicit unresolved/conflicting cases.
5. Canonical claim resolution retaining all alternatives and categorical precedence.
6. Source corroboration without an opaque confidence score.
7. Correction/retraction/update/confirmation relationships with prior history preserved.
8. Immutable persistence, dataset lineage, two-time replay, deterministic validation and
   extensive offline tests for those contracts.

Unscheduled NLP, LLM extraction, novelty/materiality, analogues, collision models,
final service/API, MCE/AME integration, signals, portfolio construction and execution
remain outside this scope. Stop after EIE-10.
