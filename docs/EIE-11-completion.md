# EIE-11 completion — Multi-Source Deduplication + Claim / Evidence Graph

Implemented and verified on 2026-10-02 from the existing EIE-10 working tree.
Engine **1.2.0**, claim/evidence schema **1.0**, SQLite store **1.5**. Every prior
public schema remains **1.0**. Changes are left uncommitted and unpushed. EIE-12
and subsequent phases have not been implemented.

## A. Architecture

Raw source metadata, actual acquisition lineage, evidence references, semantic claims,
provider assertions, canonical events and resolutions have separate identities. The evidence
layer augments existing events and retains every source alternative. EIE-10 relationship
and exposure models remain separate. See [architecture](architecture.md).

## B. Package structure

`src/eie/evidence/` contains 15 modules: vocabulary, typed claims, references, content
verification, relationships, matching, deduplication, resolution, history, models, queries,
repository, validation, engine and package version. `src/eie/providers/evidence/` adds four
modules for bounded structured interchange and existing-domain normalization. The runnable
example and representative profile are separate from runtime code.

## C. Claim-evidence schema

The new schema is `1.0`. Frozen dataclasses, controlled enums, validated timestamps and
stable typed IDs replace arbitrary triples. Domain-separated SHA-256 fingerprints pin
semantic identity, source revisions, policies, evidence locators and exact resolution inputs.
Deserialization uses the existing explicit type allowlist; source strings cannot add types.

## D. Claim model

Twelve families cover occurrence, time, status, entity identity, relationship, corporate
action, filing metadata, earnings schedule, actual, estimate, guidance and macro values.
Subjects reuse existing stable IDs. Claim IDs identify proposition slots independently of
asserted values. Numeric identity includes EIE-5 measurement context, period/stage, canonical
unit and supplied actual/prior or estimate/contributor semantics. Missing guidance bounds
remain missing. Event/corporate subjects must identify the appropriate occurrence or period.

## E. Provider claim observations

Immutable assertions pin the semantic claim, provider/class, configured source family,
dataset/resource, stable record key, raw observation, acquisition, parser, typed value,
evidence links, action status, prior revision and knowledge/valid times. Provider/resource
keys distinguish records across source items. Corrections preserve earlier assertions;
conflicting inputs for one revision number fail closed in storage and pure resolution.
Identical repeated inputs do not inflate alternatives.

## F. Evidence references

References retain raw/item IDs, content and fragment hashes, parser/adapter versions and
precise field, JSON pointer, CSV row/column, static XML or SEC metadata locations. Optional
half-open byte/character spans carry explicit encoding semantics and content length.
Supplied source bytes verify whole-content and fragment hashes in memory. Duplicate JSON
fields, duplicate CSV headers, ambiguous XML paths and unsafe spans fail. References contain
no raw bodies or fragment text and support normalized many-to-many assertion links.

## G. Claim relationship graph

All nine relationships are distinct: `SUPPORTS`, `CONTRADICTS`, `CONFIRMS`, `CORRECTS`,
`UPDATES`, `RETRACTS`, `SUPERSEDES`, `DUPLICATE_OF`, `POSSIBLE_DUPLICATE_OF`.
Validation checks dataset, exact endpoints, causal availability and semantic compatibility.
Automatic support/conflict links have canonical pair ordering. Unknown time cannot support,
confirm or contradict a time assertion. Bounded traversal returns IDs with truncation.

## H. Corrections and retractions

Changes point to the same provider thread's immediate predecessor and advance knowledge
time. Updates validate lifecycle domains; explicit corrections can fix a wrong semantic
binding. Retractions/withdrawals deactivate their source thread while preserving history.
They cannot retract another provider. Resolution distinguishes absent, fully retracted,
unresolved and resolved propositions. Earlier cutoff views exclude future changes.

## I. Source independence

Configured origin families are separate from provider name/class. Primary releases and
syndicated mirrors can share a family. Agreeing repetitions or later revisions contribute
one independent supporting family; distinct families retain separate evidence. Entitlement,
family correctness and underlying source authenticity remain configured assertions.

## J. Corroboration

`EvidenceSummary` exposes agreeing independent source families, source classes, additional
independent confirmations, contradictions, corrections, retractions, unresolved conflicts
and relevant assertion times. Counts are descriptive; no confidence score or predictive
calibration is introduced. Conflicting alternatives remain available even when precedence
selects a value.

## K. Canonical claim resolution

Each semantic field resolves separately under a fingerprinted categorical precedence
policy, including controlled predicate overrides. Compatible exact times can refine coarse
times. Equivalent observations use stable ID ordering; incompatible equal-precedence
assertions remain unresolved with no invented winner. `event_field_resolutions` resolves
claims linked to all alias-group members without assigning one provider every field.

## L. Conflict model

Typed conflict vocabulary includes value, time, status, entity, unit, scope and revision.
Canonical conflict pairs retain both sourced alternatives. Period, unit, role and identity
incompatibility is also rejected or separated at normalization/matching boundaries.
Same-unit quantity comparisons reuse canonical conversions; source references alone do
not make otherwise equivalent filing metadata or exposure values conflict.

## M. Duplicate candidates

Candidates pin exact descriptor revisions, both retained source event IDs, decision,
match/separation reasons, exact time delta when supplied and policy. Decisions are `MERGE`,
`KEEP_SEPARATE` or `UNRESOLVED`. Neither a shared source-item bundle nor uncertain proximity
forces a merge. Every decision can be recalculated from retained inputs.

## N. Event blocking and matching

Blocks use existing event identity, scoped provider/resource key, SEC accession, EIE-4
corporate identity, exact issuer/type/period, official release identity and adjacent UTC date
buckets. A bounded per-call index avoids unrestricted universe-wide pair comparison.
Tolerances are explicit by event-type code, with zero tolerance for unconfigured codes.
Indexed block projections are retained and checked on read.

## O. False-merge safeguards

Hard differences in event type, known entities, reporting period, accession, corporate
identity, action date, parties, release identity/stage or incompatible terminal lifecycle
run before positive matching. M&A needs target/acquirer roles and strong identity.
Tests distinguish earnings from management departure, same-day separate filings, nearby
dividends, different counterparties and different macro series. Complete-link grouping
prevents A/B and B/C matches from overriding an A/C separation or missing comparison.

## P. False-split safeguards

Exact semantic/accession/corporate/provider identity and appropriate issuer/type/period
or official macro release identity can establish an event match. Tests cover multi-provider
earnings, date-to-exact refinement, mirrored filings, normalized corporate identities,
macro reports and reschedules. A time conflict does not destroy otherwise stable semantic
identity; its field disagreement remains separately visible.

## Q. Canonical event groups

Groups are immutable alias partitions with sorted member event IDs and a pinned policy.
The representative is a stable retained member, not an assertion that it owns every field.
Original canonical events/revisions remain intact. Conflicting hard identities supplied
under one existing canonical event ID fail closed and require correct distinct source IDs.
Candidate and group exhaustion is explicit and cannot silently imply completeness.

## R. Merge/split history

Append-only descriptor revisions and saved resolution runs retain exact inputs, candidate
links and group memberships. Tests exercise separate→merge→split and preserve earlier
serialized views after later corrections. Group membership can change without deleting
source events, raw observations, evidence or previous groups.

## S. Two-time replay

Claims accept independent knowledge cutoffs and optional half-open valid intervals. Only
visible applicable revisions participate; knowledge ends are derived from visible history.
Event aliases replay visible descriptor revisions with supplied event timing. Completed item
clocks cannot backdate. Equal-clock independent commits are allowed until
`EvidenceEngine.freeze` atomically persists complete views and seals that cutoff.

## T. Query APIs

APIs cover claims by subject/predicate, claim history, supporting assertions, contradictions,
references, canonical fields, claims as of a cutoff, source-item audit, event claims/sources,
duplicate candidates, groups/aliases, event-resolution history and bounded traversal.
Datasets and knowledge cutoffs are explicit. Compound canonical calculations run under
one SQLite transaction; frozen views retain exact inputs for restart validation.

## U. Raw/evidence/claim ingestion

One source-item transaction commits raw metadata, acquisition, normalized references/links,
claims/revisions, relationships, optional canonical events/descriptors, field resolutions,
event groups and an immutable checkpoint receipt. Late failures roll back the whole item.
Identical delivery reuses the receipt, but supplied bytes are still checked. Changed raw
metadata requires a new item version; acquisition clocks may differ for a shared raw ID.

## V. Dataset/live lineage

Evidence datasets can pin a completed EIE-7 build and exact manifest fingerprint.
Historical lineage pins the exact fact and linked acquisition ID, parser/adapter/resource,
qualified source availability and actual local acquisition/ingestion clocks. Live copies
cannot pretend to have reconstructed historical availability. Coarse/unknown reconstruction
uses actual ingestion. Live/backfill copies can share raw/reference identity while keeping
separate acquisition/assertion lineage. Queries cannot silently mix dataset builds.

## W. Persistence and migrations

Additive migration 6 raises store `1.4` to `1.5`, adding 19 normalized indexed tables,
foreign keys, 38 update/delete refusal triggers and linked-raw update protection. Evidence
is stored once with assertion links; artifacts retain IDs and normalized values. The original
five migrations have identical AST/SQL literals. Migration tests preserve prior payload
bytes and prove complete rollback on failed DDL. Earlier persistence behavior remains tested.

## X. Concurrency and atomicity

The existing owned transaction and `BEGIN IMMEDIATE` serialize writers. A real two-connection
race retains both conflicting providers, both receipts and identical restart resolution.
A second writer cannot enter between calculation and cutoff sealing. Same-clock intermediate
ingestion artifacts remain exact stage snapshots; current queries include all committed
evidence, and freeze establishes a completed immutable cutoff.

## Y. Validation

Read checks compare typed payloads with scalar indexes and normalized memberships, then
recalculate resolutions/candidates. Full bounded restart validation checks foreign keys,
build/acquisition lineage, references, revisions, groups, seals and incomplete item
checkpoints. Corruption fixtures cover 18 index/membership targets. Source authenticity
and licensing are not independently authenticated by these consistency checks.

## Z. Performance and storage efficiency

Raw bodies/fragments are not copied into evidence records. Reference and link normalization
allows reuse across claims and acquisitions. Indexed claim/source/time/block lookup and
bounded deterministic candidates limit work. The profile stores 2,000 propositions,
2,040 assertions and 4,080 references, including 20 conflicting propositions and 20
corrections. Its 1,000-descriptor matching corpus produces 2,000 candidates and 200 groups.
See AJ for measurements and AM for scale limits.

## AA. Security

Structured source content is inert. No dynamic imports, source-issued tool instructions,
filesystem paths, network transport, executable serialization or model calls are added.
Decode/record/query/span bounds are explicit. XML declarations/entities and unsafe/ambiguous
locations fail. Normalized retention defaults to unknown and requires configured permission;
optional raw-byte verification does not archive content or grant an entitlement.

## AB. Documentation

Added [claim/evidence](claim-evidence-graph.md), [deduplication](multi-source-deduplication.md),
[claim resolution](claim-resolution.md), [replay](evidence-replay.md) and this report.
Updated README, architecture, master status, event contracts, source policy, causality,
dataset manifests, relationship graph, corporate events, expectations and persistence.
The README executable example passes and verifies exact fragments, replay and restart.
All 225 repository-local Markdown file links resolve in the offline review.

## AC. Tests added

**212 additional collected cases** across six suites cover contracts, resolution,
deduplication, persistence, historical lineage and adversarial safeguards. They include
all nine relations, corrections/retractions, source independence, field-specific selection,
false merges/splits, merge/split replay, structured fragment checks, corruption, migration,
atomic failure, two-connection concurrency, bounds and independent-process hash seeds.
The 1,345 prior tests remain passing; existing test edits only update version expectations.

## AD. Full pytest result

**1,557 passed in 48.05 seconds** on the final normal run. The complete socket-blocked run
also passed: **1,557 tests in 121.06 seconds, zero connection attempts**. All fixtures
are offline; no new runtime network path is present.

## AE. Coverage

Branch collection is enabled. Combined statement/branch coverage is **96.46064%**
(20,086 covered opportunities / 20,823), passing the configured **95%** gate.
Statements: 15,030 / 15,411; branches: 5,056 / 5,412. Across the new evidence/provider
packages, combined coverage is **96.40431%** (2,413 / 2,503). Relationship/history models
reach 100%; field resolution reaches 98.92%; deduplication reaches 98.42%.

## AF. Ruff

Lint passes. All **325 checked files** pass the format check.

## AG. mypy

Strict typing passes across **206 source files**. New models use concrete typed values,
immutable collections and explicit optional fields. No runtime dependency is needed.

## AH. Diff review

`git diff --check` passes. Tracked changes and all new implementation, test, example,
profile and documentation files were reviewed. New files also pass the whitespace scan.
Git status contains the intended EIE-11 changes; no commit or push was made.

## AI. Secret and unfinished-marker scan

Scanning 345 repository files found **zero secret findings** and no actionable
unfinished implementation markers. Two existing documentation matches describe review
gates in the master and historical EIE-5 completion report. They are intentional prose.
No new dependencies were installed and no sibling repository was modified.

## AJ. Benchmark/profile

The runnable [profile](../benchmarks/evidence_profile.py) and retained
[measured results](../benchmarks/EIE-11-profile.json) use 102 source items, 2,000 semantic
claims, 2,040 provider assertions, 4,080 references and an independent 1,000-descriptor
matching workload. Measured wall times on this development machine:

| Operation | Seconds |
| --- | ---: |
| Atomic ingestion | 12.418 |
| Resolution of all claims | 3.081 |
| 100 evidence lookups | 0.266 |
| Historical replay (1,020 claims) | 1.228 |
| Blocked candidate generation | 2.129 |
| Fully validated restart | 7.837 |

Checkpointed SQLite size: **26,292,224 bytes** (25.07 MiB). Restart output is byte-identical.
The matching workload is measured in memory; this SQLite profile stores the claim ledger,
not that descriptor corpus. Persisted candidates/groups are covered by integration tests.
Measurements ran alongside validation jobs; no fragile CI timing threshold or production
throughput claim is made.

## AK. Versions

| Contract | Version |
| --- | --- |
| Engine/package | 1.2.0 |
| Claim/evidence | 1.0 |
| SQLite | 1.5 (migration 6) |
| Every prior public schema | 1.0, unchanged |
| Python requirement | 3.11+ |

## AL. Dependencies

**Zero runtime dependencies.** No new runtime or development dependencies were added.
Existing pytest, coverage, Ruff and mypy tooling was reused read-only. There is no graph
database, embedding/NLP/vector package or model SDK.

## AM. Remaining limitations

- Source truth, document completeness, licensing and source-family assignment remain
  configured assertions. Fragment authentication needs supplied bytes; reference-only
  ingestion verifies retained links and hashes, not external authenticity.
- Ingestion is structured and offline. There are no native new vendor connectors, prose
  extraction, fuzzy identity, embeddings or model-assisted extraction.
- Semantic subjects must identify the correct event/period. Alias groups do not silently
  rehash independently scoped measurement claims or physically merge canonical events.
- Knowledge is ordered within a ledger. Backdated reconstruction needs its own dataset;
  completed equal-clock waves should be frozen. Parser/settings reprocessing creates new
  lineage under a forward clock or separate compatible build, preserving old artifacts.
- Defaults bound items, queries, revisions, relationships and candidates; oversized work
  fails or exposes truncation. Field resolution caps 256 active assertions and 10,000
  alternatives. Larger ledgers need partitioning or explicit bounded configuration.
- Immutable audit history and full restart validation consume storage and computation.
  The moderate profile does not establish distributed or warehouse-scale performance.
- No calibrated confidence, novelty/materiality, trading inference, execution or external
  engine integration has been added.

## AN. Exact proposed EIE-12 scope — unimplemented

**Unscheduled Event / News Intelligence + Structured NLP Extraction**:

- Untrusted text ingestion and source/document normalization.
- Deterministic preprocessing; event/entity/numeric/time extraction.
- Schema-constrained model-assisted extraction where appropriate.
- Evidence spans and isolation of source text.
- Prompt-injection resistance; no source-issued tool instructions.
- Extraction confidence separate from source reliability.
- Numeric verification; failed extraction is not evidence of no event.
- Prompt/model/version fingerprints.
- Source-backed extracted claims feeding EIE-11.
- Unscheduled-event classification.

This is proposed future scope only. Work stops after EIE-11; no later phase, service,
MCE/AME integration or trading functionality was implemented. Changes remain in the
working tree without committing or pushing.
