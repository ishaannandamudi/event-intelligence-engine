# Event Intelligence Engine (EIE) — Master Specification

**Status:** Master design; implementation completed through EIE-6 (`0.7.0`)<br>
**Planned roadmap:** EIE-0 through EIE-17 (updated by the EIE-5 phase specification)<br>
**Primary language:** Python<br>
**Repository name:** `event-intelligence-engine`<br>
**Core design goal:** institutional-grade, causal, replayable, provider-neutral event intelligence infrastructure for downstream quantitative systems.

---

## 1. Mission

The Event Intelligence Engine (EIE) is a standalone system that answers:

> **What scheduled or unscheduled event is affecting this security or the market, what was knowable at this exact timestamp, how important/material is it, who is exposed, how is the market reacting, and what event-risk context should downstream systems know about?**

EIE is not a trading bot. It does not submit orders, produce broker instructions, or map “good news” to “buy” and “bad news” to “sell.” It produces normalized, auditable, causally valid event intelligence that other systems may consume.

EIE must be independently usable, testable, versioned, replayable, auditable, and provider-neutral.

---

## 2. System Position in the Broader Stack

EIE will eventually integrate with independent systems such as:

### Market Context Engine (MCE)
Provides broad market state:
- regime
- trend
- realized volatility
- breadth
- liquidity/stress
- sectors
- correlation/dispersion
- factors/network
- options context
- macro context
- ensemble state

### Axiom Microstructure Engine (AME)
Provides short-horizon market mechanics:
- order book state
- order-flow imbalance
- microprice
- trade flow
- queue/microstructure behavior
- execution infrastructure

### Future Alpha / Statistical Arbitrage Engine
May combine:
- EIE catalyst intelligence
- MCE market state
- volatility forecasts
- statistical/relative-value features

### Future Portfolio & Risk Engine
Owns:
- position sizing
- portfolio constraints
- exposure management
- portfolio-level risk

### Boundary
EIE supplies **typed event intelligence snapshots**, not raw provider schemas and not trading decisions.

---

## 3. Core Design Principles

Priority order:

1. **Causality**
2. **Data provenance**
3. **Determinism**
4. **Temporal correctness**
5. **Source quality**
6. **Entity resolution**
7. **Replayability**
8. **Explainability**
9. **Fail-closed behavior**
10. **Measurable historical validation**

Do not prioritize:
- flashy AI summaries
- raw sentiment scores
- opaque LLM judgments
- speculative predictions
- complex models before reliable data
- trading decisions

The engine must always distinguish:

> **KNOWN NOW**

from

> **KNOWN LATER**

Historical replay at time `T` must only use information whose availability/knowledge time is `<= T`.

---

## 4. Strict Non-Goals

EIE must not directly implement:
- broker integration
- order placement
- execution algorithms
- buy/sell recommendations
- portfolio construction
- expected-return forecasts as a final trading instruction
- arbitrary headline sentiment as a trading signal
- opaque LLM-only severity or direction
- future-data contamination

LLMs may assist with extraction/classification later, but they are not authoritative for event existence, timestamps, canonical entity identity, numeric truth, source confirmation, or trade direction.

---

## 5. Canonical Time and Bitemporal Model

Time semantics are first-class.

### Canonical internal representation
Prefer UTC integer nanoseconds consistently throughout the project.

### Event/source timestamps
Where applicable, preserve:
- `scheduled_for_ns`
- `announced_at_ns`
- `published_at_ns`
- `source_observed_at_ns`
- `ingested_at_ns`
- `effective_at_ns`
- `last_updated_at_ns`
- `available_at_ns`

Do not collapse them into a generic `timestamp`.

### Bitemporal semantics
EIE should explicitly distinguish:

**Valid time**<br>
When a fact/event applies in the real world.

**Knowledge/system time**<br>
When EIE could have known that fact/event.

Example:
- event applies to Q2
- publication occurs July 25 at 16:05
- EIE sees it at 16:05:00.320
- a correction arrives at 16:17

A replay query at 16:10 sees the original version, not the 16:17 correction.

---

## 6. Raw Source Observation vs Canonical Event

Maintain a hard separation between:

### Raw Source Observation
Preserve:
- provider/source name
- source identifier
- source class
- source URL/reference when permissible
- source timestamp
- observation timestamp
- ingestion timestamp
- raw event type
- raw entity identifiers
- payload hash
- provider version
- adapter version
- raw/normalized payload metadata
- parsing/extraction status

Raw observations should be append-only where practical.

### Canonical Event
A deterministic normalized semantic event derived from one or more raw observations.

Canonicalization must never destroy provenance.

---

## 7. Deterministic Identity and Fingerprints

Primary historical event identity must not rely on random UUIDs.

Use stable semantic/source information to derive deterministic IDs.

Requirements:
- same underlying event + same canonical inputs => same event ID
- updates to the same event remain linked
- duplicate ingestion remains idempotent
- corrections/revisions do not become unrelated events

Maintain fingerprints for:
- raw observation
- source metadata
- canonical payload
- event result
- configuration
- extraction prompt/model version when AI assistance is used

---

## 8. Revision, Correction, and Supersession Model

Never overwrite history.

Represent:
- original observation
- revision
- correction
- superseding value/event
- amendment
- schedule change
- cancellation
- reschedule

Suggested relations:
- `revision_number`
- `supersedes_event_id`
- `supersedes_observation_id`
- `correction_of`
- `schedule_revision_of`

Historical replay returns the version known at the requested time.

---

## 9. Event Taxonomy

Use a typed hierarchical taxonomy with stable machine identifiers.

### A. Macroeconomic Events
Examples:
- CPI / Core CPI
- PPI / Core PPI
- Employment Situation / NFP
- unemployment rate
- average hourly earnings
- JOLTS
- claims
- GDP
- PCE / Core PCE
- retail sales
- industrial production
- sentiment
- ISM/PMI when lawful source exists
- durable goods
- housing
- trade balance
- productivity
- employment cost index

Example stable IDs:
- `MACRO.CPI.US`
- `MACRO.NFP.US`
- `MACRO.CORE_PCE.US`

### B. Central-Bank Events
Keep distinct identities for:
- FOMC decision
- statement
- SEP
- press conference
- minutes
- scheduled speeches/testimony

### C. Corporate Earnings
Support:
- earnings date
- release timing: before market / during market / after market / unknown
- EPS actual/consensus
- revenue actual/consensus
- prior values
- guidance
- guidance revision
- earnings-call timestamp

### D. SEC / Regulatory Filings
At minimum classify:
- 8-K
- 10-Q
- 10-K
- 6-K
- 20-F
- S-1
- 424B
- 13D
- 13G
- Form 4
- material amendments

### E. Corporate Actions
Examples:
- dividends
- splits / reverse splits
- buybacks
- tender offers
- spin-offs
- mergers/acquisitions
- delistings
- symbol changes

### F. Management / Governance
Examples:
- CEO/CFO departure or appointment
- board changes
- auditor changes
- bankruptcy/restructuring
- material legal/regulatory events

### G. Product / Business Catalysts
Examples:
- major product launch/delay
- customer contract win/loss
- regulatory approval/rejection
- recall
- major partnership

### H. Market Structure Events
Examples:
- trading halt
- reopening
- limit state
- exchange disruption
- symbol-specific suspension

Taxonomy must remain extensible and provider-neutral.

---

## 10. Event Scope and Applicability

Typed scope:
- `SYMBOL`
- `COMPANY`
- `INDUSTRY`
- `SECTOR`
- `INDEX`
- `MARKET`
- `MACRO_GLOBAL`

A market-wide event should not be copied onto every ticker as an independent event.

Instead, downstream symbol queries should use deterministic applicability logic.

---

## 11. Canonical Entity Registry

Build stable entity identity instead of relying on company-name strings.

At minimum support:
- stable issuer ID
- SEC CIK where applicable
- ticker
- exchange
- aliases
- historical ticker changes
- issuer/security distinction

Fuzzy matching may generate candidates, but may not silently become canonical identity.

---

## 12. Entity Relationship / Exposure Graph

Advanced phases should build a typed relationship graph.

Possible relations:
- supplier-of
- customer-of
- competitor-of
- subsidiary-of
- parent-of
- strategic-partner-of
- sector-member
- industry-member
- ETF-member
- index-member

Each relationship should preserve:
- source/provenance
- confidence/quality
- valid time
- knowledge time
- revision history where applicable

Outputs may include:
- direct exposure
- first-order relationship exposure
- second-order propagation candidates
- sector/industry applicability

These represent **event exposure relationships**, not predicted price moves.

---

## 13. Source Trust and Precedence

Source trust classes should remain categorical:
- `OFFICIAL_PRIMARY`
- `EXCHANGE_PRIMARY`
- `COMPANY_PRIMARY`
- `REGULATED_FILING`
- `LICENSED_NEWS`
- `SECONDARY_NEWS`
- `UNVERIFIED`

Do not immediately collapse these into a single opaque number.

Precedence should generally favor authoritative primary sources for underlying facts while preserving all observations and explicitly recording conflicts.

The system must distinguish:

**NO EVENTS**

from

**SOURCE FAILED**

---

## 14. Provider-Neutral Adapter Architecture

Conceptual source interface:

```text
EventSource
  source_name
  source_version
  health()
  capabilities()
  scheduled_events(...)
  poll(...)
  fetch(...)
  normalize(...)
```

Adapters should expose:
- health
- last successful fetch
- latency
- parse failures
- schema mismatches
- outages
- staleness
- rate-limit state
- supported event families

No network call may occur merely by importing the package.

---

## 15. Event Lifecycle and Risk Window State

### Event lifecycle
Use one explicit deterministic state machine, e.g.:
- `SCHEDULED`
- `CONFIRMED`
- `IMMINENT`
- `RELEASED`
- `UPDATED`
- `POST_EVENT`
- `NORMALIZED`
- `CANCELLED`
- `RESCHEDULED`

### Trading-context event window
Represent separately:
- `PRE_EVENT`
- `EVENT_WINDOW`
- `POST_EVENT`
- `NORMAL`

Windows are configurable by event type; no universal blackout duration.

---

## 16. Scheduled Event Subsystem

Support:
- events between T1/T2
- next event affecting symbol/scope
- time until event
- events in pre-event/blackout windows
- cancellations/reschedules
- schedule revisions
- schedule-known-at semantics

A changed event time must preserve the historical previous schedule.

---

## 17. Numerical Expectations and Surprise Intelligence

### Macro releases
Retain:
- actual
- consensus
- previous
- revised previous
- unit
- seasonal adjustment
- reference period
- revision status

Raw surprise:

`actual - consensus`

Do not compute surprise without consensus.

Prior-only normalized surprise may be computed only with a defensible historical scale using information available before the current release.

### Earnings
Keep separate dimensions such as:
- EPS surprise
- revenue surprise
- guidance change
- margin guidance
- capex guidance

Do not collapse immediately into one bullish/bearish score.

---

## 18. Static Importance, Materiality, and Empirical Impact

Keep three concepts separate:

### Static Importance
How important the event class generally is.

### Event Materiality
How economically significant the specific event is.

Potential components:
- financial magnitude
- business relevance
- affected operations/revenue
- regulatory importance
- scope
- surprise magnitude
- novelty
- confirmation

### Empirical Market Impact
How strongly similar prior events have historically affected markets.

Do not let an LLM directly invent any of these.

---

## 19. Event Study Engine

For each event category, measure behavior around event time.

Possible measurements:
- signed return
- absolute return
- abnormal return
- realized volatility
- volume
- quoted spread
- depth where supplied
- liquidity disruption
- microstructure instability
- options IV/skew reaction where supplied
- time to normalization

Configurable windows may include:
- pre-event
- 0–1m
- 1–5m
- 5–15m
- 15–30m
- 30–60m
- close
- +1d
- +5d

Any historical statistic used live at `T` must use only events before `T`.

---

## 20. Abnormal Reaction and Counterfactual Research

Measure both raw and residualized reaction.

Possible baselines:
- market model
- sector model
- factor model
- matched control
- synthetic-control-style comparison
- difference-in-differences where defensible

Typed research output may include:
- observed return
- estimated counterfactual return
- abnormal return
- estimation method
- control universe
- pre-event fit quality
- uncertainty

Do not call residualized movement proof of causation.

---

## 21. Event Influence / Decay

Event relevance should eventually be measured empirically rather than only by fixed windows.

Possible outputs:
- initial impact
- current impact
- impact decay
- information saturation
- market normalization
- still-price-relevant indicator

Do not invent arbitrary decay constants if historical evidence can estimate the behavior.

---

## 22. Deduplication and Event Clustering

Repeated coverage of one underlying event must not become many independent catalysts.

Typed relationships:
- `NEW_EVENT`
- `UPDATE`
- `CONFIRMATION`
- `DUPLICATE_REPORT`
- `CORRECTION`
- `FOLLOW_UP`

Start with deterministic source/event metadata where possible.

Semantic/NLP similarity can be added after deterministic infrastructure is reliable.

---

## 23. Claim-Level Evidence Graph

Canonical events should be decomposable into claims.

Example:

```text
Canonical Event
  ├─ Claim A
  │   ├─ Source observation 1
  │   └─ Source observation 2
  ├─ Claim B
  │   └─ SEC filing
  └─ Claim C
      └─ unverified report
```

Each claim may have:
- claim type
- structured value
- unit
- source observation IDs
- source span/section when available
- extraction method
- extraction confidence
- confirmation state
- contradictory evidence
- verification status

This allows mixed-confirmation events.

---

## 24. Unscheduled Event Intelligence

Later-stage flow:

```text
raw item
→ source validation
→ entity resolution
→ event classification
→ claim extraction
→ duplicate/update detection
→ source confirmation
→ canonical event
```

Potential categories:
- unscheduled company announcement
- product/business catalyst
- legal/regulatory event
- cyber incident
- recall
- contract win/loss
- product delay
- management event
- market-structure incident

One unverified headline must not automatically become a confirmed critical event.

---

## 25. LLM-Assisted Extraction Policy

LLMs may assist with:
- classification
- structured extraction
- summarization
- entity candidate generation
- document section identification
- semantic duplicate candidates

LLMs must not be authoritative for:
- publication timestamps
- canonical numeric truth
- event existence
- ticker/issuer identity
- source confirmation
- trading direction

All AI-assisted extraction must be schema validated.

### Prompt-injection / untrusted-content security
Source documents are untrusted input.

Requirements:
- sanitize HTML/script content
- never treat source text as system/tool instructions
- no automatic tool execution from document instructions
- validate extracted entity IDs independently
- verify numeric values against evidence where possible
- extraction confidence remains separate from source reliability
- parser/extractor failure must not mean “no event”
- record extraction model/prompt version fingerprint

---

## 26. Novelty Intelligence

Novelty should become first-class.

Potential dimensions:
- document novelty
- factual novelty
- numeric novelty
- guidance novelty
- entity novelty
- cross-source novelty
- aggregate novelty

Distinguish:
- repetition
- confirmation
- correction
- follow-up
- genuinely new fact

A mostly repeated article containing one materially new number should preserve that distinction.

---

## 27. Historical Analogue Retrieval

Retrieve similar prior events using causal historical data only.

Similarity dimensions may include:
- event type
- industry
- company size
- surprise magnitude
- guidance magnitude
- materiality
- novelty
- event timing
- prior trend
- MCE regime
- volatility regime
- liquidity regime
- options context
- macro-event proximity

Outputs should be descriptive distributions, e.g.:
- number of analogues
- median abnormal return by horizon
- quantiles
- IV change distribution
- time-to-normalization distribution

Do not automatically turn analogue statistics into a trade forecast.

---

## 28. Market-Reaction Surprise

Keep information surprise separate from reaction surprise.

### Information surprise
Actual vs consensus / guidance vs previous guidance.

### Market-reaction surprise
Observed market reaction vs historically typical reaction for comparable events.

Possible outputs:
- expected reaction distribution from analogues
- observed reaction
- reaction percentile
- reaction residual
- “unusually muted” / “unusually strong” reason codes

This can later become valuable input to downstream research systems.

---

## 29. Event Interaction / Collision Intelligence

Events may overlap.

Examples:
- CPI + FOMC same day
- earnings + CEO departure
- guidance cut + investigation
- supplier disruption + sector weakness

Represent:
- event clusters
- temporal overlap
- shared entities
- independent-event count
- interaction type
- aggregate materiality
- collision risk
- parent/child event relationships
- dominant event candidate

Do not simply add severity scores.

---

## 30. Event Propagation Chronology

For directly and indirectly affected assets, preserve when reactions appear.

Possible historical sequence:
- primary issuer reaction
- supplier/customer reaction
- competitor reaction
- sector ETF reaction
- index reaction
- options reaction

Outputs should remain descriptive and timestamped.

---

## 31. Cross-Asset Reaction Mapping

Architect event-study outputs so they are not permanently equity-price-only.

Potential reaction domains:
- underlying equity
- options IV/skew/term structure
- sector ETF
- broad index
- rates
- FX
- commodities later where appropriate

Only add data domains when supported by reliable sources.

---

## 32. Information Latency Intelligence

Measure the information pipeline:
- source publication → EIE observation
- EIE observation → ingestion
- ingestion → canonicalization
- canonicalization → snapshot availability

Track per-source latency distributions where meaningful.

Operational latency must remain distinct from market impact.

---

## 33. Empirical Source Reliability

Static trust class remains separate from empirical reliability.

Potential source diagnostics:
- confirmation rate
- correction rate
- retraction rate
- duplicate rate
- parse failure rate
- schema-change frequency
- entity-resolution accuracy
- numeric extraction accuracy
- conflict rate
- median latency

A source can be categorically `LICENSED_NEWS` while also carrying empirical historical reliability metrics.

---

## 34. Uncertainty Decomposition

Do not rely on one confidence number.

Potential dimensions:
- source uncertainty
- entity-resolution uncertainty
- classification uncertainty
- numeric-extraction uncertainty
- event-identity uncertainty
- confirmation uncertainty
- materiality uncertainty
- analogue uncertainty
- impact-estimation uncertainty

Downstream systems should be able to distinguish why confidence is limited.

---

## 35. Durable Event Store

Requirements:
- append-only raw observations where practical
- canonical event history
- revisions
- claim/evidence links
- source metadata
- versioned schemas
- stable fingerprints
- transaction-safe writes
- restart recovery
- idempotent ingestion

Public transport/artifacts should use validated JSON-compatible structures, not executable serialization formats.

---

## 36. Replay

Historical replay is mandatory.

Conceptual API:

```text
snapshot(as_of=T, symbol="NVDA")
```

must return exactly what EIE could have known at `T`.

Replay must preserve:
- source availability
- schedule knowledge
- event revisions
- corrections
- issuer mappings
- historical ticker changes
- claim confirmation state
- model/extraction artifact availability
- source outages if modeled

---

## 37. Quality Model

Quality conditions may include:
- `STALE`
- `FUTURE_TIMESTAMP`
- `MISSING_TIMESTAMP`
- `UNCONFIRMED`
- `SOURCE_UNAVAILABLE`
- `SCHEMA_CHANGED`
- `ENTITY_UNRESOLVED`
- `DUPLICATE`
- `CONFLICTING_SOURCES`
- `CORRECTED`
- `DEGRADED`

Do not silently convert bad or missing data into healthy zero-valued evidence.

---

## 38. Aggregate Event-Risk State

The engine may eventually expose compact descriptive risk state:
- `NORMAL`
- `ELEVATED`
- `HIGH`
- `EVENT_BLACKOUT`

This is event-risk context, not a trading signal.

Every aggregate state must reconcile to active canonical events and explicit reason codes.

---

## 39. Event Intelligence Snapshot

Primary downstream contract:

```text
EventIntelligenceSnapshot
```

Potential content:
- schema version
- `as_of_ns`
- queried symbol/scope
- active events
- next scheduled event
- seconds to next event
- event-window state
- aggregate event-risk state
- top material events
- novelty/materiality components
- numerical surprise components
- empirical impact
- historical analogue summaries
- relationship/exposure propagation
- claim/source confirmation
- quality flags
- uncertainty decomposition
- source fingerprint
- configuration fingerprint
- result fingerprint

Downstream systems should not need to parse raw news or raw SEC documents.

---

## 40. Operational Metrics

Track separately from predictive quality:
- raw items received
- canonical events created
- duplicates suppressed
- updates applied
- corrections
- entity-resolution failures
- source conflicts
- provider errors
- stale sources
- latency
- active events
- upcoming events

Separate:

1. **Operational validation**
2. **Information quality**
3. **Market-impact evidence**

Infrastructure correctness does not imply trading effectiveness.

---

## 41. Intelligence Evaluation Framework

By later phases evaluate the intelligence components themselves.

### Entity resolution
- precision
- recall
- unresolved rate

### Deduplication
- duplicate precision/recall
- false-merge rate
- false-split rate

### Event classification
- per-event-type precision/recall

### Numerical extraction
- exact-match accuracy
- unit accuracy

### Confirmation logic
- confirmation accuracy

### Novelty/materiality
- reviewed ranking agreement / calibration where defensible

Keep these separate from downstream trading results.

---

## 42. Security

Never put secrets in:
- logs
- event records
- fingerprints
- CLI arguments
- test fixtures

Use environment/config secret handling where credentials are needed.

Security requirements:
- no unsafe pickle for public artifacts
- no dynamic `eval`
- no shell execution from source content
- validate all external timestamps/numerics/IDs
- safe exception surfaces
- no local-path/credential leakage in service errors
- strict untrusted-content handling for filings/news

---

## 43. Testing Philosophy

All automated tests must run without external network access.

Use small legally usable normalized fixtures or mocks.

Never depend on today’s live calendar.

Core deterministic tests should cover:
- serialization
- fingerprints
- event-ID determinism
- idempotent ingestion
- duplicates
- revisions/corrections
- schedule changes
- source conflicts/precedence
- entity resolution
- unresolved entities
- ticker changes
- timestamps
- future-timestamp rejection
- historical replay
- lifecycle transitions
- cancellation/reschedule
- event windows
- no-event vs provider-outage
- staleness
- surprise handling
- revised prior
- event scope/applicability
- restart recovery
- deterministic repeated-run equality
- claim/evidence lineage
- bitemporal knowledge-time correctness
- unscheduled-event fail-closed behavior
- novelty/materiality causality
- analogue prior-only selection
- counterfactual no-lookahead
- source-reliability history causality

---

# 44. Full Roadmap

## EIE-0 — Foundation, Contracts & Temporal Semantics

Build:
- repository/package structure
- canonical time model
- bitemporal semantics foundation
- event taxonomy
- raw source observation model
- canonical event model
- entity model
- event scope/applicability contracts
- source trust model
- deterministic event identity/fingerprints
- revision/supersession model
- lifecycle state machine
- immutable configuration/fingerprint
- source adapter interfaces
- persistence interfaces
- replay interfaces
- quality flags
- deterministic serialization
- representative tests/docs

No live ingestion required.

**Acceptance:** foundation contracts are typed, fail closed, deterministic, replayable, and fully documented.

---

## EIE-1 — Scheduled Event Engine

Build:
- scheduled-event index/store
- upcoming-event query
- time-to-event
- event-window state
- configurable windows by event type
- cancellations
- reschedules
- schedule revisions
- schedule-known-at semantics
- applicability queries
- historical replay tests

**Acceptance:** first useful vertical slice for scheduled-event risk.

---

## EIE-2 — Official Macro & Central-Bank Adapters

Implement suitable official-source adapters for:
- Federal Reserve
- BLS
- BEA
- other appropriate official sources where justified

Add:
- schedules
- releases
- source health
- idempotent ingestion
- schema/version handling
- source availability clocks
- outage vs no-event semantics

**Acceptance:** deterministic official macro/central-bank ingestion and replay.

---

## EIE-3 — SEC EDGAR & Issuer Identity

Build:
- canonical issuer registry
- CIK/ticker/exchange mapping
- historical ticker changes
- SEC submissions metadata
- accession IDs
- filing timestamps
- form classification
- report period
- amendment handling
- idempotent ingestion

Do not start full filing-language intelligence yet.

**Acceptance:** reliable filing metadata and issuer identity foundation.

---

## EIE-4 — Corporate Calendars, Earnings & Actions

Add provider-neutral support for:
- earnings
- calls
- dividends
- splits
- buybacks
- tender offers
- M&A
- spin-offs
- delistings
- symbol changes
- management/governance events

Preserve event-specific timestamp semantics.

**Acceptance:** strong structured corporate-event substrate.

---

## EIE-5 — Structured Expectation & Surprise Intelligence

Build:
- macro actual/consensus/previous/revised-previous
- raw surprise
- prior-only normalized surprise
- earnings EPS/revenue surprise
- structured guidance changes
- unit compatibility
- historical scaling
- revision handling

**Acceptance:** defensible numerical surprise without directional trade labeling.

---

## EIE-6 — Live Provider Transport & Ingestion Infrastructure

Build real HTTP transport, polling, streaming abstractions where useful, rate limits,
bounded retry/backoff, ingestion queues, per-provider concurrency, health,
checkpoints/cursors, restart recovery, persistent source state, ingestion latency,
atomic writes, and continuous official/provider data acquisition.

**Acceptance:** bounded, observable, restartable live ingestion preserving existing
raw-first provenance, atomicity, availability, and replay contracts.

**Implemented:** engine `0.7.0` adds standard-library HTTPS, bounded polling and
queues, atomic SQLite data/checkpoints, durable domain sessions, recovery,
provider health and foreground operational controls. Current SEC submissions
and BLS iCalendar are explicit opt-in sources. All quantitative schemas remain
`1.0`; runtime/ingestion and persistent-store schemas start at `1.0`.
See [EIE-6 completion](EIE-6-completion.md), [live ingestion](live-ingestion.md)
and [persistence](persistence.md). EIE-7 and later phases remain unimplemented.

---

## EIE-7 — Historical Backfill, Dataset Construction & Source Archival

Build provider-aware historical backfill, reproducible dataset construction,
source archival, and provenance-preserving historical vintage acquisition.

**Acceptance:** auditable historical datasets with explicit vintage coverage and
causal reconstruction boundaries.

---

## EIE-8 — Event Studies + Influence/Decay

Build event-study framework for:
- raw returns
- realized vol
- volume
- spread
- depth where available
- liquidity disruption
- microstructure instability
- options reaction where supplied
- time to normalization

Add causal rolling/expanding historical impact statistics.

Add empirical event-influence/decay characterization.

**Acceptance:** prior-only historical market-impact evidence by event family.

---

## EIE-9 — Abnormal Reaction & Counterfactual Impact

Add:
- market-adjusted returns
- sector-adjusted returns
- factor-adjusted returns
- matched-control methods
- synthetic-control-style methods where defensible
- fit/uncertainty diagnostics

Preserve raw reaction alongside residualized reaction.

**Acceptance:** stronger descriptive event-reaction attribution without claiming proof of causation.

---

## EIE-10 — Entity Relationship & Exposure Graph

Build:
- supplier/customer graph
- competitors
- parent/subsidiary
- strategic relationships
- sector/industry membership
- ETF/index membership
- provenance and historical validity of relationships
- direct and indirect event applicability

**Acceptance:** deterministic second-order exposure mapping.

---

## EIE-11 — Multi-Source Deduplication & Claim/Evidence Graph

Build:
- duplicate detection
- update/confirmation/correction/follow-up classification
- source conflict handling
- claim-level evidence graph
- source spans/structured provenance
- mixed-confirmation events
- deterministic source precedence

**Acceptance:** multiple reports resolve into auditable semantic events instead of duplicate catalysts.

---

## EIE-12 — Unscheduled Event Intelligence & Secure Extraction

Add provider-neutral infrastructure for unscheduled news/business events.

Build:
- classification
- entity extraction candidates
- structured fact extraction
- duplicate clustering
- source confirmation
- evidence linkage
- secure LLM-assisted extraction
- prompt-injection/untrusted-content controls

**Acceptance:** unscheduled events can enter canonical infrastructure without making the model authoritative.

---

## EIE-13 — Novelty, Materiality & Uncertainty

Build:
- document/factual/numeric/guidance novelty
- static importance
- event-specific materiality
- separate empirical impact
- explicit uncertainty decomposition
- explainable reason codes

**Acceptance:** engine distinguishes repeated information from genuinely new material information.

---

## EIE-14 — Historical Analogues & Reaction Surprise

Build analogue retrieval conditioned on:
- event type
- magnitude
- company/sector characteristics
- MCE context when supplied
- liquidity/volatility/options context
- time-of-day/event timing

Return distributions, not trade recommendations.

Add:
- expected historical reaction distribution
- actual reaction percentile/residual
- unusually muted/strong reaction diagnostics

**Acceptance:** causal context-conditioned historical comparison engine.

---

## EIE-15 — Event Collision, Interaction & Propagation

Build:
- overlapping-event clusters
- temporal collisions
- shared entities
- independent-event count
- parent/child event relationships
- dominant-event candidate
- aggregate interaction state
- propagation chronology across related assets
- cross-asset reaction mapping where data is supplied

Do not simply sum severity.

**Acceptance:** EIE handles compound catalyst environments coherently.

---

## EIE-16 — Bitemporal Intelligence, Latency & Empirical Source Reliability

Formalize:
- valid time
- knowledge time
- point-in-time source/version selection

Add:
- publication→observation latency
- observation→ingestion latency
- ingestion→normalization latency
- normalization→snapshot latency
- empirical source reliability statistics
- source correction/conflict history

Keep trust class separate from measured reliability.

**Acceptance:** high-integrity information-arrival and source-quality intelligence.

---

## EIE-17 — Evaluation, Snapshot, Service, Replay & Hardening

Finalize standalone engine.

Build:
- intelligence-quality evaluation suite
- immutable `EventIntelligenceSnapshot`
- aggregate event-risk state
- Python service API
- replay API
- CLI
- canonical JSON/JSONL export
- health/readiness
- provider diagnostics
- restart recovery
- operational metrics
- security hardening
- latency/performance benchmarks
- comprehensive causality tests
- MCE/AME/downstream typed integration contracts
- production-style documentation

Do not modify external repositories unless explicitly requested after standalone acceptance.

**Acceptance:** EIE standalone system is versioned, reproducible, observable, replayable, hardened, and ready to expose stable typed contracts.

---

# 45. Codex Execution Contract

For every phase:

1. Inspect the current repository before changing code.
2. Review the previous phase contracts and tests.
3. Implement **only the requested phase**.
4. Preserve backward compatibility unless the phase explicitly requires a versioned breaking change.
5. Do not silently weaken causal or replay guarantees.
6. Do not introduce hidden mutable state.
7. Do not fabricate missing data as zero.
8. Keep provider-specific code behind adapters.
9. Update tests and documentation in the same phase.
10. Run all configured quality gates.
11. Review full diff and repository status.
12. Stop after the requested phase.

No Codex phase should independently “improve” future roadmap items unless required by the active phase.

---

## 46. Standard Quality Gates

Prefer project-configured commands equivalent to:

```bash
pytest -q
ruff check .
ruff format --check .
mypy src
git diff --check
```

As the repository evolves, also require where appropriate:
- coverage >= configured threshold
- deterministic serialization tests
- replay/no-lookahead tests
- secret scan
- TODO/FIXME review
- docs review
- benchmark when performance is phase-relevant

Do not silence meaningful failures.

---

## 47. Documentation Set

At minimum evolve toward:
- `README.md`
- `docs/architecture.md`
- `docs/event-contract.md`
- `docs/source-policy.md`
- `docs/time-and-replay.md`
- `docs/entity-resolution.md`
- `docs/event-studies.md`
- `docs/evidence-graph.md`
- `docs/novelty-materiality.md`
- `docs/analogues.md`
- `docs/security.md`
- `docs/service-api.md`
- `docs/operations.md`
- `docs/roadmap.md`

---

## 48. Final System Questions EIE Should Answer

For any timestamp `T` and symbol/universe, EIE should eventually answer:

1. Is a scheduled event approaching?
2. Did an event just occur?
3. What type/family is it?
4. What scope does it apply to?
5. When was it scheduled/announced/published/observed/effective?
6. What was actually knowable at `T`?
7. Was it expected?
8. What are actual/consensus/previous/revised values where applicable?
9. How surprising was it using only prior information?
10. How important is this event class?
11. How material is this specific event?
12. How novel is the information?
13. How trustworthy/confirmed are the underlying claims?
14. Which entities are directly and indirectly exposed?
15. What has historically happened around similar events?
16. What reaction would be typical for similar historical events?
17. How unusual is the current market reaction?
18. Is the market still behaving as though the event matters?
19. Are multiple events interacting or colliding?
20. Are sources conflicting?
21. What uncertainty exists and why?
22. Is the event in PRE_EVENT / EVENT_WINDOW / POST_EVENT / NORMAL?
23. What compact event-risk state should downstream systems know?
24. What provenance and evidence support every important output?

---

## 49. Definition of Success

EIE is successful when it becomes a trustworthy information/catalyst layer that can sit beside MCE and AME without duplicating them.

The mature stack should conceptually provide:

```text
MCE
“What market environment are we in?”

EIE
“What happened, what is about to happen, who is exposed, and how unusual/material is it?”

AME
“What is happening in the order flow and execution environment right now?”

Future Alpha Engine
“What opportunities are supported by the combined evidence?”
```

EIE itself remains a causal, descriptive, event-intelligence system rather than a trading decision engine.

---

## 50. Initial Implementation Boundary

**Start with EIE-0 only.**

Do not start EIE-1 until EIE-0 has been reviewed and accepted.

The first Codex implementation should establish the contracts that every later phase depends upon; it should not attempt live ingestion, advanced NLP, event studies, relationship graphs, or downstream integration prematurely.
