# SEC EDGAR filing metadata (EIE-3)

EIE-3 reads [SEC EDGAR submissions data](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), its referenced historical submissions files, and the SEC ticker/exchange association JSON. The parser accepts their native column-oriented JSON shapes. The checked-in fixtures use those shapes with **synthetic** CIKs, names, and filings. They are small metadata examples, not real filings or evidence that these events occurred. Runtime code uses only the Python standard library and makes no request on import.

## Boundary and configuration

`SecAdapter` parses bytes; `SecIngestor` owns normalization, in-memory persistence, queries, diagnostics, and health. Call `ingest_payload` or `ingest_ticker_map` with source-observed and ingested UTC nanoseconds. Fetching is optional: `fetch_and_ingest` uses a caller-supplied `HttpTransport`, approved SEC HTTPS URLs, a configured identifying contact user agent, a timeout, and one request. It has no bundled HTTP client, polling loop, retry, or background task. Configure `Settings(sec=SecSettings(enabled=True, ...))`; SEC is disabled by default. Set `user_agent` with contact information before using the fetch boundary, and operate within the [SEC's fair access guidance](https://www.sec.gov/about/developer-resources). Payload size, item count, staleness interval, and timeout are bounded by settings. No secret or credential belongs in the source URL, raw metadata, or fingerprints.

## Parsing and provenance

The main submissions document requires CIK, name, `filings.recent` columns, and `filings.files` history references. Required recent columns are accession number, filing date, acceptance timestamp, form, and primary document. Optional report period, file number, and explicit issuer CIK are preserved when present. Rows must have consistent column lengths; invalid identity, dates, timestamps, document names, or required structure fail closed. Harmless extra list columns contribute to the source-fragment hash, so changed official row metadata can be detected. History files are ingested separately using an explicit CIK and resource kind; references are listed by `history_files_as_of` only after they were learned. A reference does not imply its file was fetched or its filings ingested. The ticker map uses `fields`/`data` with exact CIK, name, ticker, and exchange columns.

Each source item has an immutable `RawSourceObservation` before any filing or event write. It records source identity/version, adapter version, `OFFICIAL_PRIMARY`, source reference, source item ID, content hash, observed and ingested times, and raw identifiers. The source payload itself is not stored. A row with unresolved issuer identity remains as raw evidence with an explicit degraded diagnostic; it does not receive a fabricated company scope. Source schema/parse failures create diagnostics and leave the filing/entity/event state unchanged.

## Filing and time semantics

`CIK` is a ten-digit canonical filer key and `Accession` is canonical 10-2-6 digits. The stable filing ID derives from both, independent of ticker, document title, or observation time. A `FilingMetadata` revision retains exact form and base family, filing date, acceptance timestamp, report period, primary document, file number, source reference, source evidence, issuer/filer IDs, amendment relation, quality, and EIE availability. Supported families are 8-K, 10-Q, 10-K, 6-K, 20-F, S-1, 424B variants, Schedule 13D/G, and Form 4, including supported `/A` variants. `document_url` is a derived SEC Archives link; EIE-3 does not fetch the document or parse its body. An 8-K item number is not invented from a full filing.

The SEC acceptance timestamp is a public-source timing proxy. Naive SEC `acceptanceDateTime` is interpreted as U.S. Eastern time, with ambiguous or nonexistent daylight-saving times rejected; explicit timezone offsets are honored. `filingDate` is a separate date and `reportDate` is the period covered, not an availability clock. Source-observed time is caller supplied and cannot precede acceptance for a row. Canonical `effective_at_ns` holds acceptance; `EventRevision.available_from_ns` and filing `available_from_ns` hold **local ingestion time**. Delayed observation or ingestion never backdates EIE knowledge to SEC acceptance. The canonical event has `SEC_FILING` family, typed form code, source facts, raw evidence ID, and issuer company scope when resolved. A Form 4 from an individual reporting-person CIK without a supported issuer link uses `SEC_FILER` scope and an unresolved-entity flag; it is not assigned to an arbitrary issuer.

Amendments have their own accession, filing ID, raw observation, and canonical event. A deterministic link requires the same filer CIK, base form, report period, and an earlier accepted ordinary filing visible by the current knowledge time. Exactly one candidate yields `LINKED`; none yields `UNRESOLVED`; multiple yield `AMBIGUOUS` with candidate IDs. No uncertain link is silently selected. A change to official metadata for the **same accession** appends a correction revision of that filing and event; it is not an SEC amendment. Repeating identical source rows produces no extra filing/event revisions.

`SecIngestor` stages the registry, filing index, event store, and history references on a copy and publishes them together only after the whole batch validates. This is an in-process atomic batch boundary; the state is in memory and has no restart recovery or cross-process transaction guarantee. Attempts advance in explicit knowledge time. `SecDiagnostics` distinguishes item, raw, issuer, filing, amendment, duplicate, and unresolved counts. Health distinguishes healthy empty results, degraded unresolved identity or parse failures, schema mismatch, rate limiting, unavailable transport, and staleness. Health is not a claim that SEC history is complete.

## Queries and limits

`filings_for_cik`, `filings_for_issuer`, `filings_for_symbol`, and `recent_filings` use explicit as-of knowledge time. Exact form, base form, amendment, and report-period filters are available through the query/store APIs. Filing results are deterministic, typed, serialized and fingerprinted with configuration. Symbol lookup requires ticker, exchange, and a distinct valid-time input; unresolved or ambiguous identity returns an explicit resolution and no issuer filing set. Historical symbols attached to a filing are checked at its acceptance time using information known by the query's as-of time. A later ticker map snapshot never becomes valid for earlier dates merely because it is the latest snapshot.

Only metadata and basic provenance are in scope. EIE-3 has no filing text, sections, NLP, materiality model, EDGAR search crawler, guaranteed historical ticker reconstruction, or complete archive coverage. The SEC mapping provides observed associations, not verified security identifiers or exact start/end listing dates. See [entity identity](entity-identity.md), [source policy](source-policy.md), and [replay and causality](replay-and-causality.md).

## EIE-6 current submissions polling

`sec_provider` supplies disabled-by-default, bounded HTTPS acquisition of one
configured CIK submissions resource. Contact User-Agent and rate policy live
in operational configuration; `SecSettings.user_agent` remains None in the
durable domain session. Recent filing, registry and canonical history commit
with the resource checkpoint. Restart restores accession/revision/idempotency
state. Historical references are not followed and documents are not archived.
See [live ingestion](live-ingestion.md) and [persistence](persistence.md).
