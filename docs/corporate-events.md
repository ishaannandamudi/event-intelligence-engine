# Corporate events (EIE-4)

EIE-4 stores provider-neutral corporate facts as immutable revisions. The taxonomy covers earnings schedule, release, and call; ordinary and special dividends; forward and reverse splits; buybacks and tender offers; mergers and acquisitions; spin-offs; symbol changes and delistings; and CEO, CFO, board, and auditor changes. It is descriptive event infrastructure, not a trading signal or a consensus estimate.

## Identity and applicability

`CorporateIdentity` combines kind, stable SEC issuer ID, semantic key, and optional verified security and listing IDs. Earnings semantic keys are derived from fiscal year, optional quarter, and period end. A ticker is never an issuer identifier. An issuer-level event applies when querying any verified security of that issuer; a security-level event applies only to that security. A supplied listing must match the issuer and, when supplied, the security at the applicable valid time. Unresolved issuer/security/listing relationships fail the batch atomically. Deal and spin-off counterparties can remain explicitly unresolved and receive `ENTITY_UNRESOLVED`.

One provider item may propose several separate events. Its raw observation is inserted first, and each event points to that observation. Different providers retain independent raw observations and claims. Claims with the same corporate identity are grouped; source precedence chooses a reported view while retaining losing claims and explicit conflict flags/reasons. `corporate.<lowercase_kind>` precedence overrides are supported. Equal-ranked disagreements remain visible as disputes, including when one claim is selected deterministically.

## Time and revisions

A corporate claim separates announcement timestamp or civil date, source publication, source observation, ingestion/availability, scheduled earnings time, and action effective timestamp or civil date. The exact timestamp queries use `[start, end)` UTC nanoseconds; date queries use `[start_date, end_date)` ISO civil dates. A date-only fact is never converted into midnight or a fabricated intraday time. `known_as_of(T)` resolves the latest available revision before filtering; a later correction, cancellation, or reschedule cannot alter an earlier query result. Event history and canonical event evidence are append-only. `canonical_state_bytes()` and typed result fingerprints are deterministic.

Dividend record, ex, and payment dates remain distinct; the ex-date is used in the action effective-date query. Split record, effective, and trading adjustment dates remain distinct; ratio direction is validated. Buyback authorization uses exact integer micros and optional share/basis-point quantities. Tender offer price and expiry are separate. Deal expected close, actual close, and termination fields, plus status history, are preserved. Effective-date queries include actual deal close, spin-off distribution, delisting and management effective dates. Symbol changes require a verified previous listing; their new valid-time interval becomes known only after ingestion. Historical symbol queries use both knowledge and valid time.

## Provider contract and failures

`CorporateAdapter` accepts only versioned `EIE_CORPORATE_EXTRACT_V1` JSON extracts with a stable provider ID and declared kind capabilities. This is an audited interchange format for synthetic or externally prepared data, not a claim of a live vendor integration. `CorporateIngestor` stages the entire batch and commits after normalization succeeds. Reingesting identical source items is idempotent. Limits bound payload bytes, item count, and result count. Diagnostics distinguish unavailable, rate limited, schema mismatch, parse error, normalization error, and unresolved entity; health can become stale. An explicit healthy empty coverage interval differs from unavailable or unknown coverage. Coverage is a provider assertion, not proof of a complete universe.

The parser treats provider text as data: no code execution, HTML execution, arbitrary file access, or network calls. URLs must be credential-free HTTPS; money is integer micros; dates and enums are validated. Corporate ingestion is disabled by default. EIE-5 adds separate expectations and surprise analytics using copies of the corporate canonical store and registry; see [expectations and consensus](expectations-and-consensus.md). Built-in corporate fetching and a universal security master remain deferred; EIE-6 durability is described below. A changed effective time for a previously applied symbol change currently fails closed because verified listing intervals are not automatically retracted; future correction handling needs explicit listing revision semantics.

## EIE-6 durability

AdapterProcessor stages existing corporate extracts behind a registered
acquisition source. SQLite stores claims, corporate/canonical history, registry,
coverage and identity caches with an atomic checkpoint. Restored sessions expose
the existing query APIs and preserve prior result bytes. No live commercial
provider or new corporate semantics are added. See [persistence](persistence.md).

## EIE-8 reaction studies

Publication and exact schedule anchors are explicit. Date-only and BMO/AMC schedules
are rejected by exact intraday studies; explicit supplied-session endpoint proxies retain
coarse quality and verify date/session/source facts. Target/acquirer and share classes use
separate supplied instrument series. Known splits in unadjusted windows suppress returns;
price and total-return adjustment semantics never silently mix. Co-bundle/same-issuer
contamination is descriptive and does not allocate a shared movement among events.
See [alignment](market-data-alignment.md) and [event studies](event-studies.md).

## EIE-10 structural relationships

The graph reuses corporate entity identities and accepts sourced `SPINOFF_FROM`,
`SUCCESSOR_OF`/`PREDECESSOR_OF` and ownership/control claims with explicit validity.
Existing corporate event/revision references can be retained as sourced qualifiers.
These structural edges do not replace the corporate event or imply current ownership.
An announced, approved or generic closed deal is not automatically converted into a
parent/subsidiary edge; the source must establish the realized relationship and effective
semantics. No document extraction or automatic corporate-action graph inference is added.
See [relationship taxonomy](entity-relationship-graph.md).

## EIE-11 conservative multi-source event aliases

Structured matching reuses EIE-4 corporate identity and exact fiscal period/type. It keeps
earnings, calls, dividends, splits, management changes and deal lifecycle events distinct.
M&A retains exact target/acquirer roles; separate deal IDs or effective dates prevent merge.
Provider formatting and exact-time refinements can support an existing semantic identity.
Source-item bundles never imply one event. See [deduplication](multi-source-deduplication.md)
and [claim resolution](claim-resolution.md).
