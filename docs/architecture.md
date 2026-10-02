# EIE architecture through EIE-11

```text
Source adapter protocol → immutable RawSourceObservation → EventNormalizer protocol
                                                            ↓
                                    immutable CanonicalEvent revision
                                        ├── EIE-0 append-only EventStore → as-of replay
                                        └── EIE-1 ScheduledEventIndex → EventStore
                                                    ├── source coverage ledger
                                                    └── causal schedule queries / event windows
```

`src/eie/core` owns UTC nanoseconds, schemas, serialization, hashes, quality and errors. `sources` owns provenance, trust, health and optional-capability protocols. `events` owns taxonomy, scope, semantic identity, lifecycle and revisions. `entities` separates issuer, security and time-bounded ticker listing. `persistence` holds the append-only protocol and deterministic in-memory store. `replay` owns the explicit clock and minimal known-events view. `config` holds immutable settings.

`scheduled` adds immutable schedule claims and revisions, a sorted event-time index over the canonical store, an append-only source coverage ledger, typed query results, and configurable window policies. The schedule index is the insertion entry point for scheduled events; direct underlying-store writes bypass it. Its sorted timestamps speed range and window queries without replacing historical revision resolution. Coverage records describe source-specific asserted intervals and health, and never imply completeness across unrepresented sources.

`providers` adds narrow Fed, BLS, and BEA parsers plus a common explicit-clock ingestion coordinator. Fetching is an injected transport boundary. Parsers output typed official items; ingestion writes raw observations before canonical/scheduled revisions and records source health separately. Date-only events remain canonical events without invented exact times. BEA's native calendar JSON supplies time-only candidates that can verify enriched extracts; it cannot supply reference period or GDP stage by itself. See [official sources](official-sources.md).

EIE-3 adds `providers/sec` for native EDGAR submissions, historical filing metadata, and ticker association parsing; `filings` for typed metadata and causal queries; and `entities/sec` for CIK-based filer/issuer revisions and point-in-time ticker/listing resolution. `SecIngestor` stages raw, registry, filing, canonical-event, and history-reference changes together before an in-memory batch commit. Its transport is injected and disabled by default. See [SEC EDGAR](sec-edgar.md) and [entity identity](entity-identity.md).

The raw/canonical boundary is structural: the two immutable classes do not inherit from each other. A canonical event names one or more raw observation IDs. The store verifies that evidence exists, its ingestion precedes the event's availability, and its source classes match. No provider schema leaks into core events. A future normalizer implements the protocol and must preserve evidence IDs.

Canonical identity uses event type, scope and a stable source/semantic key. Source-specific adapters must supply stable keys such as an official release period or meeting ID; a schedule timestamp or ticker alone is unsuitable. Revision number and availability time identify knowledge changes. The in-memory store retains all revisions. The in-memory store remains a deterministic offline implementation; EIE-6 adds durable SQLite and restart recovery through the same event protocol.

Time is signed-64-bit nonnegative UTC nanoseconds. Scheduled/effective time describes the world; revision `available_from_ns` describes what EIE could know. Replay returns the latest revision available at the requested time. A later revision cannot rewrite an earlier view. Listings also have valid and available intervals. Exact listing resolution accepts a knowledge cutoff and an optional distinct valid-time query, so a ticker that was valid earlier can be resolved from information known later.

Future downstream consumers should receive typed event intelligence snapshots after later phases. EIE does not import MCE or AME and does not make trading decisions.

See [scheduled events](scheduled-events.md) for the full EIE-1 contract.

## EIE-4 corporate layer

The `corporate` package separates shared enums/values, earnings payloads, action payloads, identity/claim/event contracts, queries, settings, and an append-only index. The `providers.corporate` package parses versioned offline extracts, selects multi-provider claims, and stages whole-batch ingestion. Each provider item first becomes an immutable raw observation; each selected corporate revision also becomes a canonical EIE event. The corporate index owns its in-memory canonical store and a copy of the SEC entity registry. Batch ingestion deep-copies this state and commits one new state only after all observations, entity checks, normalizations, and listing changes succeed. Query results preserve knowledge-time coverage, claims, conflicts, and deterministic fingerprints. See [corporate events](corporate-events.md).

## EIE-5 expectations layer

`expectations` contains typed measurements/units, estimate and actual observations,
guidance, an append-only observation ledger, consensus selection/aggregation,
strict-prior normalization, surprise calculations, immutable query contracts, settings,
and `ExpectationsEngine`. `providers/estimates` contains capability declarations,
typed parsed records, the versioned synthetic extract parser, and atomic ingestion.

```text
Known canonical event store + verified SEC registry → exact event/period binding
Synthetic estimate extract → staged raw evidence → append-only observation streams
                                                      ├─ as-of consensus / histories
                                                      ├─ frozen release surprise
                                                      │      └─ strict-prior scale artifact
                                                      └─ company guidance comparisons
```

The engine owns copies of upstream stores; changes are not synchronized implicitly.
Provider consensus and explicit within-provider analyst aggregates use separate
origins. Whole-batch staging commits one engine pointer; failed batches retain only
diagnostic health. Queries reconstruct historical knowledge and scale inputs from
the immutable ledger, without mutable online statistics or wall-clock access.
See [expectations and consensus](expectations-and-consensus.md) and
[surprise intelligence](surprise-intelligence.md).

## EIE-6 acquisition and persistence

Dedicated `transport`, `runtime` and `ingestion` packages separate bounded HTTP,
per-provider rate/retry policy, polling and queues from adapter normalization.
A serialized SQLite ingestion transaction loads the latest domain session,
stages raw/domain/canonical changes, persists them and advances the checkpoint
together. Independent operational rows hold health, latency and quarantine.
The existing in-memory engines are reconstructed from explicit versioned typed
state on restart; SQL does not duplicate their quantitative rules.

Runtime configuration is separate from `Settings`; HTTP contacts/timeouts and
worker counts cannot contaminate canonical fingerprints. Current SEC submissions
and BLS iCalendar have opt-in sources. Fed/BEA remain audited-extract adapters.
See [live ingestion](live-ingestion.md), [persistence](persistence.md),
[provider runtime](provider-runtime.md) and [operations](operations.md).

## EIE-7 historical construction

`backfill` owns pure plans, bounded page scheduling, SEC traversal, reuse and explicit
archive rebuild. `archive` owns bounded retained bytes and acquisition metadata.
`datasets` owns isolated append/index records, lineage, immutable manifests,
validation and build-bound queries. All use the existing serialized SQLite
transaction boundary; historical page commits do not copy live durable sessions.
Source availability is stored in a wrapper beside honestly acquired domain/raw
contracts. This preserves established live clock invariants. See
[historical backfill](historical-backfill.md) and [dataset manifests](dataset-manifests.md).

## EIE-8 study architecture

Frozen event build + explicit revision cutoff → supplied indexed market provider →
explicit anchor/session/window alignment → raw response measurements + indexed
contamination → immutable indexed study definitions/results/run manifests → strict-prior
cohort distributions and sampled influence/decay. Retained bounded input slices permit
pure recalculation without becoming a market-data warehouse. Domain/live caches and
existing event identities remain unchanged. See [event studies](event-studies.md).

## EIE-9 counterfactual architecture

Validated raw study + explicit supplied prior target/benchmark/factor/control history →
causal input projection + pinned model spec → exact paired-period fit/match diagnostics →
period abnormal returns + independent CAR + justified uncertainty → immutable indexed
model/fit/result/set/run storage. The impact package separates inputs/models, estimation,
regression/fitting, controls, abnormal, uncertainty, diagnostics, sensitivity, engine,
queries, repository and validation. Every result preserves its raw study and historical
fact/build/reconstruction qualification. No model winner is chosen from post-event
performance. See [counterfactual impact](counterfactual-impact.md).

## EIE-10 relationship architecture

Verified EIE-3 identities + configured structured relationship source → raw observation
→ append-only provider claims → categorical multi-source canonical resolution → indexed
two-time adjacency → bounded immutable paths/snapshots → separate event propagation
candidates. Raw/source-item/claim/canonical/checkpoint changes share one SQLite transaction.
Store migration 5 is additive (`1.3` → `1.4`); graph schema is `1.0`.

`graph` separates taxonomy, nodes, exposure, claims, canonical/traversal contracts,
history, queries, traversal, propagation, ingestion, repository and validation.
`providers/relationships` contains the bounded static interchange adapter, configured
capabilities/snapshot policy and normalization. Graph knowledge is explicit and ordered;
persisted snapshots seal their cutoff against later backdated inserts. Local traversal
uses indexed adjacency rather than a permanent in-memory copy of the whole graph.
Prior event scopes, studies and impact contracts stay independent. See
[relationships](entity-relationship-graph.md) and [graph replay](graph-replay.md).

## EIE-11 evidence architecture

Configured structured provider → raw metadata/acquisition → normalized hash-pinned field
references → semantic claims and provider assertion revisions → typed support/conflict/
correction links → categorical field resolutions → optional structured duplicate candidates
and immutable event alias groups. One source item and checkpoint commit under the existing
SQLite transaction. Migration 6 raises the store to `1.5`; evidence schema is `1.0`.

`evidence` separates vocabulary, claims, references/content verification, relationships,
resolution, matching/blocking, grouping, history, queries, repository, validation and engine.
`providers/evidence` supplies typed bounded interchange and existing-domain bridges.
The exposure graph keeps its own semantics. Saved evidence views seal their cutoff;
independent provider writes at a shared unsealed clock retain both alternatives.
See [claim/evidence](claim-evidence-graph.md) and [deduplication](multi-source-deduplication.md).
