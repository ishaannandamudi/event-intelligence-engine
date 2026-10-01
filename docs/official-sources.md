# Official U.S. sources (EIE-2)

EIE-2 adds offline-testable official-source adapters for the Federal Reserve, BLS, and BEA. It retains EIE-0 raw observations and canonical revisions and uses the EIE-1 scheduled index for exact-time schedules. The runtime remains standard-library only. All providers are disabled by default; no module fetches on import.

## Boundaries and supported inputs

The `providers` package has provider-specific parsers, common typed source facts, a small ingestion coordinator, and an optional `HttpTransport` protocol. Callers explicitly pass bytes and source observation/ingestion UTC nanoseconds to `OfficialSourceIngestor.ingest_payload`. `fetch_and_ingest` accepts a caller-supplied transport, validates the official HTTPS host, uses configured timeout/user agent, and makes one request with no hidden retry. EIE does not bundle a live HTTP transport or daemon. Production collection/extraction is a separate operational step.

- **Federal Reserve:** versioned minimal extracts of [FOMC calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) entries. A meeting entry can create separate decision, statement, SEP, press-conference, and minutes identities. Each derived release has a stable raw item ID and fragment hash so its publication time and evidence stay distinct; the meeting span is retained as namespaced raw metadata. Only events explicitly present in the extract are emitted. The public calendar does not imply an exact release time; date-only entries stay date-only. The parser does not scrape arbitrary Fed HTML.
- **BLS:** versioned extracts of official schedule/release metadata and a strict subset of the [BLS iCalendar feed](https://www.bls.gov/schedule/news_release/bls.ics). Supported families are CPI (including core as a component), PPI, Employment Situation (with unemployment and earnings components), JOLTS, ECI, and productivity. A release package is one event, not one event per numerical table row. The iCalendar parser requires a stable UID, supported summary/reference month, and timezone-aware start; unrelated calendar entries are skipped. Release metadata is ingested through the extract contract.
- **BEA:** versioned extracts covering GDP by stage, Personal Income and Outlays/PCE with core PCE and income/spending components, and U.S. trade. The adapter also parses BEA's [native machine-readable release dates](https://apps.bea.gov/API/signup/release_dates.json) into time-only candidates. That feed lacks reference period and GDP stage, so candidates are not made into canonical events on their own. `verify_extract_against_calendar` can check an enriched extract's times against those candidates. It does not claim historical completeness from the current feed.

`EIE_OFFICIAL_EXTRACT_V1` is EIE's small, explicit **extraction interchange format**, not a claim that an agency publishes this JSON schema. Its items include official source URL, stable item ID/version, release or meeting identifier, reference period/stage where available, status, and exact/date-only schedule or publication timestamp. Unknown harmless keys are retained in the hashed source fragment but ignored by normalization. Missing critical fields fail closed. The extraction process must be audited separately before live deployment.

## Provenance, clocks, and identity

Every parsed item becomes a `RawSourceObservation` before any canonical write. Source identity records agency, source version, adapter version, and `OFFICIAL_PRIMARY`; the observation records item ID/version, canonical source-fragment SHA-256, content type, official reference, optional published time, source observed time, and ingestion time. The raw payload itself is not stored. `source_observed_at_ns` and `ingested_at_ns` are explicit caller inputs. A source's publication time is used only when explicitly present and cannot follow observation time. A missing release publication timestamp is flagged; EIE does not substitute the scheduled time.

Canonical identity uses event type, U.S. macro scope, a stable release/meeting key, reference period, and GDP stage where relevant. A changed schedule or corrected release keeps the same event ID and gets a new revision with availability at **EIE ingestion time**. Source publication does not backdate EIE knowledge. Exact-time schedules use `ScheduleRevision` with raw claim evidence. Release and correction revisions retain the prior schedule claim plus their new raw observation. Replaying before a later ingestion returns the earlier canonical and scheduled revision byte-for-byte.

`ReferencePeriod` stores validated start and exclusive end dates, frequency, and label. `TimePrecision` is exact timestamp, date-only, or unknown. Date-only events are retained as canonical events with a `schedule_date` fact and no fabricated nanosecond timestamp. They do not appear in exact-time `upcoming` queries. If an exact time is later published, its canonical revision joins the scheduled index while the earlier date-only revision remains in the underlying event store. An exact schedule cannot silently downgrade to date-only.

Source status must explicitly distinguish an original schedule, a real reschedule, a schedule correction, a release, a release correction, and a cancellation. A changed timestamp supplied merely as another `SCHEDULE` is treated as conflicting official evidence: the prior time stays selected, both claims remain, `CONFLICTING_SOURCES` is set, and the canonical confirmation becomes disputed pending review. No timestamp change is silently called a reschedule.

## Health, coverage, and configuration

`IngestionDiagnostics` reports item, raw, canonical, schedule-revision, and duplicate counts with a typed `SourceHealth`. Healthy empty extracts have zero items and `HEALTHY`; malformed JSON, schema mismatch, unavailable transport, and rate limits have distinct health/error categories. Failure reports carry last success, last attempt, and parse-failure count. `health_as_of` marks a previously healthy source stale after the configured threshold. These are explicit-clock operational records and do not alter canonical fingerprints.

A caller may assert an event-time coverage interval when ingesting a snapshot or reporting failure. EIE writes that as source-specific coverage; it never infers completeness from a partial page, a date-only entry, or the current native feed. This supports EIE-1's `NO_MATCH` versus `INDETERMINATE` distinction. With no coverage assertion, an empty exact-time query does not imply a complete official calendar.

`Settings.official` configures enabled provider IDs, staleness threshold, maximum payload bytes, one-request timeout, and a nonpersonal user agent. It participates in the configuration fingerprint. There are no secrets or new runtime dependencies. The optional transport is an injected protocol; tests use local bytes/fakes only.

## Fixtures and limits

`tests/fixtures/official/fed_calendar.json` transcribes only the [September 2026 FOMC meeting and SEP association](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) as date-only facts. `bls_schedule.json` transcribes selected [September 2026 BLS dates and Eastern times](https://www.bls.gov/schedule/2026/09_sched.htm). `bea_schedule.json` transcribes selected [BEA GDP and Personal Income and Outlays schedule rows](https://www.bea.gov/news/schedule/full). `bea_release_dates.json` is a small subset of the [native BEA JSON feed](https://apps.bea.gov/API/signup/release_dates.json). Test-generated revisions, outages, releases, corrections, and conflict times are **synthetic** and do not assert those historical changes occurred. Fixtures contain no full documents or credentials.

EIE-2 does not parse the Fed's live HTML calendar or BEA's live full-schedule HTML, fetch native release articles, or extract numerical values. Its extract format requires an upstream audited extraction step. The EIE-2 in-memory ingestion path is not transaction-safe across a multi-item batch. EIE-3 adds a separate SEC EDGAR metadata path with staged in-memory batch commits; see [SEC EDGAR](sec-edgar.md). No live provider polling, event studies, statistical surprise, or trading features are included.

## EIE-5 expectation binding

The canonical store produced by official ingestion can initialize an
`ExpectationsEngine`. Bind the exact existing `ReferencePeriod` and release stage
before adding separately sourced consensus and actual observations. Components
retain individual measurement IDs; agency schedules do not supply or invent market
consensus. The expectations engine owns a copy of the store. See
[expectations and consensus](expectations-and-consensus.md) and
[macro integration tests](../tests/test_expectations_integration.py).

## EIE-6 runtime integration

The existing BLS iCalendar parser has an explicit opt-in HTTPS polling source.
Fed/BEA extraction remains offline/audited; EIE-6 adds no HTML scraping. All
three adapters can use AdapterProcessor with a trusted source and a durable
shared official namespace. Whole official batches now stage before SQLite
commit, including their checkpoint. Existing offline APIs remain available.
See [live ingestion](live-ingestion.md).
