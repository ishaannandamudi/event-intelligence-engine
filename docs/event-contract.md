# Event contracts

Engine version is `0.5.0`; raw-observation, canonical-event, entity, configuration, scheduled-event, official-source extract, SEC filing, and corporate-event schemas are each `1.0`. Constructors reject unsupported schema versions.

## Raw observation

`RawSourceObservation` has source name/version/adapter version/class; item ID and item version; SHA-256 payload hash; content type; observed, ingested and optional published times; optional source reference and raw event type; sorted raw entity identifiers and string metadata; quality flags. The raw payload itself is outside this core contract. Observation ID hashes full source identity, item ID, item version and payload hash. Its result fingerprint hashes the whole immutable record. Inserting an identical observation twice is idempotent; a different record with the same ID is rejected.

## Canonical event

`CanonicalEvent` contains `EventIdentity`, `EventRevision`, lifecycle, confirmation, evidence observation IDs, source classes, title, distinct optional scheduled/announced/published/observed/ingested/effective/updated timestamps, typed scalar facts with optional units, and quality flags. Evidence IDs and facts are sorted and unique. No NaN, infinity or arbitrary executable object is accepted. `EventIdentity` consists of hierarchical `EventType`, `EventScope`, and a stable semantic key. Its event ID is independent of schedule, title and revision. The key must be stable across ingestion; EIE-0 cannot infer one from arbitrary text.

`EventRevision` records number, kind, availability, previous revision number, optional superseded event ID and sorted reason codes. First revision is `ORIGINAL` number 1. Later revisions reference the immediate predecessor. Store insertion requires contiguous numbers and strictly increasing availability. A reschedule changes the schedule while retaining semantic event ID and prior revision. Cancellation is terminal. An actual different semantic event may use `supersedes_event_id`; the store requires the target to be a different event already known by that availability time but does not adjudicate cross-event facts in EIE-0.

`LifecycleState` is separate from `EventWindowState`. The transition table lives in `events/lifecycle.py`. `UPDATED` requires a prior released/post-event/updated/normalized state. `CANCELLED` has no outgoing transition. Scheduled and confirmed states permit same-state schedule updates, and `RESCHEDULED` permits consecutive moves, return to scheduled/confirmed, or release. EIE-1 calculates window state from an explicit policy. Reinstatement after cancellation remains deferred because cancellation is terminal.

Confirmation states are `UNVERIFIED`, `REPORTED`, `CORROBORATED`, `CONFIRMED_PRIMARY`, `DISPUTED`, `RETRACTED`. They are explicit evidence judgments, not inferred from headline count. Typed quality flags include stale, missing/future timestamp, source unavailable, schema changed, unresolved entity, duplicate, conflict, corrected, degraded, invalid unit/scope/revision. The system never treats a flagged record as a healthy zero value.

Scope kinds include symbol, security, company, SEC filer, industry, sector, index, market, macro country and macro global. Direct applicability matches explicit identifiers; broad market/global scopes apply through matching query context. No relationship propagation exists yet. Taxonomy codes are uppercase dot-separated identifiers; family prefixes are validated while new family-specific codes remain possible.

Canonical JSON sorts object keys and unordered sets, emits UTF-8, preserves explicit nulls, rejects non-finite numbers and unknown object types. SHA-256 fingerprints have domain prefixes separated by a NUL byte. `event_payload_fingerprint` hashes all canonical payload fields except revision metadata/schema; `result_fingerprint` hashes the entire event record. Configuration has its own domain. These hashes exclude secrets and runtime memory state.

## Scheduled-event specialization

`ScheduleRevision` wraps exactly one `CanonicalEvent` revision and immutable, raw-observation-backed `ScheduleClaim`s. It reuses canonical semantic identity, lifecycle, confirmation, quality, revision chain, and evidence. It adds the selected schedule claim and optional adjudication reasons; its own schema and fingerprint are `1.0` and domain-separated. `ScheduleStatus` is derived as active or cancelled and remains distinct from lifecycle and window state. A cancellation claim proposes no new time while canonical `scheduled_for_ns` retains the last known time for historical reference.

`ScheduledEventView`, range/query results, next-event result, active-window result, and coverage summary are immutable typed results. Query results include as-of time, resolved revision and provenance, configuration fingerprint, source coverage, and deterministic serialization. Empty `NO_MATCH` requires reported available coverage; other empty outcomes are `INDETERMINATE`. See [scheduled events](scheduled-events.md).

## Official-source facts

`OfficialItem` is a typed parser output with source item ID/version, source version, official HTTPS reference, event type, stable semantic key, item status, optional publication time, schedule precision, optional reference period, release stage, component codes, and deterministic source-fragment hash. `ReferencePeriod` has validated ISO start and exclusive end dates, typed frequency, and label. `TimePrecision` distinguishes exact timestamp, date-only, and unknown. Provider-neutral period/stage/precision facts are added to `CanonicalEvent`; source-specific schema/version and URL remain in raw provenance. See [official sources](official-sources.md).

## SEC filing specialization

`FilingMetadata` has stable CIK-plus-accession identity, separate filer and optional issuer IDs, exact form/base/amendment type, filing date, accepted timestamp, report period, primary document, source evidence, knowledge time, revision number, amendment status/target/candidates, and quality flags. Metadata corrections append a revision; amendments are distinct filings. `FilingView`, `FilingQueryResult`, and `SymbolFilingQueryResult` include historical symbol context, configuration fingerprint, and exact resolution outcome. Each has domain-separated deterministic fingerprints. The canonical filing event uses the `SEC_FILING` family with company or SEC-filer scope and the raw observation as evidence. See [SEC EDGAR](sec-edgar.md).

## Corporate event specialization (EIE-4)

Engine version is `0.5.0` and corporate-event schema is `1.0`; prior schema versions remain unchanged. `CorporateIdentity` hashes kind, stable issuer, semantic key, and optional verified security/listing. `CorporateClaim` retains provider event ID, raw observation ID, source class, available-from, status, typed payload, distinct announcement date/timestamp and effective timestamp, and quality. `CorporateEvent` appends contiguous revisions, selected claim key, confirmation, conflicts, and all provider claims. Earnings semantic identity is its fiscal reference period. Date-only schedules never create an exact canonical scheduled time. The canonical event carries a domain-separated event ID, lifecycle, evidence IDs, sorted structured facts, and distinct publication/observation/ingestion/effective clocks. Typed query results and coverage carry as-of time and configuration fingerprints. See [corporate events](corporate-events.md) and [earnings calendar](earnings-calendar.md).
