# EIE-15 completion report

## A. Architecture

A descriptive interaction consumer projects retained canonical/evidence revisions into
compact rows, blocks candidates by indexed time/entity/scope/bundle context, calculates
separate interaction artifacts and saves an immutable snapshot in one SQLite transaction.
Identity, graph, materiality and market owners remain independent.

## B. Package structure

Fifteen modules under `src/eie/interactions`: schema, models, inputs, windows, collection,
collisions, sequences, transitions, propagation, contamination, engine, repository,
queries, validation and package schema declaration. Examples, seven test modules and an
offline profile accompany the package.

## C. Interaction schema

Event-interaction schema is `1.0`. Frozen typed artifacts use deterministic SHA-256 IDs
and the existing allowlisted JSON codec. Prior public schema definitions are unchanged.

## D. Collision model

`EventCollision` canonicalizes a symmetric distinct event-row pair and pins cutoff,
window, intervals, categories, graph-path references, quality and explicit ordering basis.
Rows retain exact source event/revision IDs and fingerprints. Collision never merges events.

## E. Collision taxonomy

SAME_ISSUER, SAME_SECURITY, SAME_SOURCE_BUNDLE, MACRO_ISSUER, SECTOR_ISSUER,
MARKET_ISSUER, GRAPH_RELATED, SAME_TIMESTAMP, OVERLAPPING_WINDOW and SEQUENTIAL_RELATED.
Multiple categories may describe one pair.

## F. Interaction windows

Immutable elapsed/supplied-session pre/post windows support type/family duration and
clock overrides, coarse-time permission and versions. Explicit mixed occurrence/scheduled
macro windows preserve both clock semantics. Maximum individual duration is 30 days.

## G. Event-time quality

Existing canonical effective/announcement/publication and scheduled clocks are retained.
EIE-11 resolved time assertions, including earnings schedules, preserve original precision
and assertion references. Raw acquisition availability and canonical knowledge availability
are distinct. No missing timestamp becomes a zero or an invented exact anchor.

## H. Same-time/coarse-time handling

Exact same-time events remain tied. Permitted date/session overlap uses a UTC date interval;
it supplies no exact gap or within-day order. Stable IDs affect serialization only.
Simultaneous lifecycle alternatives cannot become an arbitrary prior state.

## I. Sequence model

`EventSequence` retains exact row/revision references, range, ordering basis, timestamp
layers, unordered members, positive primary elapsed steps, signed secondary gaps and
quality. Public APIs support exact entity and event-code filters.

## J. Knowledge versus occurrence chronology

Context artifacts retain separate occurrence and knowledge views. Public APIs also
support source availability and schedules. Retroactive disclosure can reverse the two
orders; secondary signed differences are retained independently of positive primary gaps.

## K. Partial ordering

Coarse and missing occurrence times remain unordered. Tied layers have no within-layer
edge. Precise neighboring singleton layers can expose typed PRECEDES steps; precise
same-basis canonical collision pairs may carry PRECEDES/FOLLOWS orientation.

## L. Same-source bundles

Shared retained raw observation IDs reuse disclosure provenance. Earnings, buyback and
management changes remain distinct events with common availability and tied information
order. Neither dedupe nor market-movement allocation is performed from bundle membership.

## M. Macro collisions

Existing macro/central-bank canonical events supply historical release/schedule metadata.
Explicit scheduled-time overrides support an issuer event five minutes before CPI.
Future schedule corrections do not alter saved snapshots.

## N. Market/sector collisions

Broad-market collisions require typed events. Sector/industry collisions require exact
historical EIE-10 paths. Prices and future classifications cannot create historical events
or scope applicability.

## O. Graph-related collisions

Queries pin visible, untruncated EIE-10 snapshots rooted in the direct event entities and
valid at root occurrence, or root knowledge when occurrence is unavailable/coarse.
Graph-qualified interactions retain exact paths and edge revision references.

## P. Event state transitions

Closed structured M&A, guidance and management states require explicit stable canonical
thread metadata. Foundation lifecycle transitions reuse the existing validator.
Unsupported progression and mixed-family threads fail. No source title is interpreted.

## Q. Correction/retraction boundaries

CORRECTION_OF and RETRACTION_OF are information relations. Neither implies real-world
completion. Event revisions remain one canonical identity and do not self-collide.

## R. Interaction context

`InteractionContext` contains compact collision, sequence, transition, chronology, chain
and path references, category counts and quality. No source document, numeric claim,
full event, intelligence result or graph payload is copied into every pair.

## S. Interaction-aware contamination

All seven required categories are present, plus typed security, sector, market and
window-only overlap categories. Categories and counts are descriptive. Existing EIE-8
study artifacts remain unchanged; richer context is independently retained.

## T. Non-additive handling

No materiality/severity combination or ranking is implemented. The offline example
computes real independent EIE-13 MAJOR and VERY_HIGH materiality artifacts and keeps
them per event. EIE-9 returns, EIE-14 surprise and EIE-13 materiality owners are never
queried for interaction membership/order; an explicit test enforces this boundary.

## U. Propagation chronology

`PropagationChronology` describes subsequent related known events within an explicit
finite horizon under a precise occurrence/source/knowledge basis. Root aliases, direct
root parties, same-time events and outside-horizon events are excluded.

## V. Propagation chains

One finite chain per root retains timestamp layers and chronology IDs. Default graph
depth is two; hard maximum is four. Session horizons require supplied calendars.
EIE-10 simple paths terminate cycles. No recursive derived-candidate generation occurs.

## W. Graph path provenance

Compact path references pin snapshot ID/fingerprint, original path ID, ordered nodes,
edge revision IDs, relationship types, hop count, valid/knowledge times and qualification.
Full graph provenance remains in EIE-10. Future and pre-root terminated edges are excluded.

## X. Alias/dedupe behavior

Retained EIE-11 runs remain authoritative and are pinned by cutoff, groups and fingerprint.
Merged reports count once; a later split changes only a new context. Missing runs supply
only canonical-ID exclusion; callers need a stored EIE-11 run for cross-source identity.
No fuzzy, transitive or severity-based merge is added.

## Y. Replay/causality

T1/T2/T3 contexts remain immutable as later events become known. Future relationships,
source revisions, aliases and schedule corrections cannot alter old results. Dataset
index cutoffs are sealed; backdated additions are refused. Concurrent future insertion
and atomic rollback are tested. Co-occurrence/sequence/graph chronology establish no
causal attribution or trading instruction.

## Z. Persistence/migrations

Additive transactional migration 10 advances SQLite 1.8 to 1.9 with nine interaction
tables, lookup/membership indices and UPDATE/DELETE protection. Earlier DDL and payloads
remain unchanged. The EIE-14 migration test still starts explicitly at store 1.7.
Restart, failed migration, source preservation and membership corruption are tested.

## AA. Query APIs

`interaction_context`, `collisions`, `propagation`, `sequence`, `state_transitions` and
bounded atomic `run` expose typed retained artifacts. Horizon, relationships, hops and
window semantics are immutable policy inputs. Normal reads avoid rebuilding upstream
intelligence, studies or all-history graph/resolution calculations.

## AB. Validation

Validation checks target identity/cutoff, alias exclusion, windows, sequence layers,
transition rules, graph timing/path references, propagation gaps, policy/count/byte
bounds, category memberships and fingerprints through exact compact recalculation.
Deep validation re-collects pinned inputs, rebuilds event projections and verifies exact
retained graph paths and EIE-11 identity artifacts.

## AC. Performance/storage

The measured workload retains 1,000 events across ten issuer blocks, ten macro events
and two-hop paths. Five target queries materialize 62–69 blocked rows each, with six
collisions and 19–99 chronology entries. Contexts total 97,237 bytes; typed artifacts
913,245 bytes; SQLite 13,361,152 bytes including canonical/source/graph owners.

## AD. Security

Source text remains inert. No source-driven import, shell, filesystem action, URL fetch,
model/tool call or network operation is introduced. Closed typed JSON, parameterized
queries, hard bounds and frozen source references are reused. Fake system/tool/URL/
credential text is absent from contexts and cannot create an event or action.

## AE. Documentation

Four new documents cover collisions, sequences, chronology and contamination. README,
master status, architecture, replay, studies, relationships/propagation, novelty,
materiality, historical analogues and persistence are updated. The runnable example
uses real EIE-11/EIE-13 owners and verifies historical bytes after restart.

## AF. Tests added

111 cases across seven interaction modules cover clocks, precision, tied/partial order,
bundles, macro/market/sector/graph collisions, state progression, correction/retraction,
alias merge/split, future leakage, multi-hop/cycles/horizons, non-additive independence,
limits, migration, corruption, insertion order/hash seeds, concurrency and rollback.

## AG. Full pytest result

Full pytest: **2,958 passed in 161.78 s**. Focused interaction tests: **111 passed in
2.79 s**. Starting socket-blocked baseline: **2,847 passed**, zero connection attempts.

## AH. Coverage

Full branch-enabled socket-blocked suite: **2,958 passed in 436.26 s**, with **zero
network connection attempts**. Combined statement/branch coverage is **95.959%**
(20,924/21,509 statements and 6,836/7,420 branches covered), above the required 95%.

## AI. Ruff

Lint passes. Formatting passes for all **468 checked files**, including Markdown Python
examples. No lint rules are disabled.

## AJ. Mypy

Strict mypy passes for **279 source files**, with no errors.

## AK. Git diff check

Diff whitespace checks pass. Full tracked/new source, tests, examples, benchmark and
documentation are reviewed. Prior source changes are additive codec/migration/version
updates; prior tests change only current versions and the frozen legacy migration slice.

## AL. Secret/unfinished-marker scan

All **492 tracked and new text files** have no detected secrets or trailing whitespace. No new
unfinished markers; the two prior marker references remain in EIE-5 completion and the
master specification.

## AM. Benchmark/profile

`benchmarks/event_interactions.py --count 1000` completed with cProfile. Measured totals:
source/index 2.002 s; blocking 0.642 s; pair/sequence/chronology 1.365 s; complete snapshot
persistence 2.383 s; ordinary query 1.801 s; deep validation 2.980 s; restart/query
1.856 s. Profile total 13.371 s. See `benchmarks/EIE-15-profile.json`. Measurements are
local, include validation and overlapping verification workloads, and impose no CI threshold.

## AN. Versions

Engine **1.6.0**, event-interaction schema **1.0**, SQLite store **1.9**. All prior public
schemas retain **1.0**.

## AO. Dependencies

Runtime dependencies remain empty. Python 3.11+, standard library and existing EIE
contracts are reused. Sibling repositories were neither modified nor imported; only
existing external development-tool executables were used.

## AP. Remaining limitations

Explicit retained canonical/evidence indexing defines the input universe; ISOLATED is
conditional on that universe, policy and source coverage. Cross-source aliases require
a stored EIE-11 run. Graph roots require exact stable scope/node bindings, with no new
symbol resolver. Coarse session labels retain day-level overlap and no inferred intraday
position. Domain progressions require sourced structured states/thread metadata; no
free-text lifecycle inference is supplied. The phase implements one nonrecursive root
chain, fails on output bounds rather than returning a partial prefix, and profiles 1,000
events rather than claiming production scale. EIE-7 source projections remain in their
existing acquisition/evidence owners; no independent historical reader is added.

## AQ. Exact proposed EIE-16 scope

**Bitemporal Intelligence + Source Reliability + Information Latency**: explicit
valid-time/knowledge-time audits and source/version selection; publication → observed →
ingested → normalized → snapshot latency; empirical correction/retraction rates,
timeliness, consistency and coverage by event type/field; source class remains separate
from empirical reliability; prior-only calculation, latency/reliability uncertainty
and no trading recommendations. This is proposed scope only. EIE-16 is not implemented.

No commit or push was performed. Work stops after EIE-15.
