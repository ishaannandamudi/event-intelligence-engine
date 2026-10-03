# Source and provenance policy

Source class remains categorical: official primary, exchange primary, company primary, regulated filing, licensed news, secondary news, or unverified. `SourcePrecedencePolicy` carries a default complete ordering and optional fact-specific overrides. EIE-1 applies it to the `scheduled_for` fact; EIE-4 applies it to corporate kinds. Raw observations and losing claims are preserved when sources conflict. A future empirical reliability metric must remain separate from the categorical class.

Every raw observation records source identity, source item/version, adapter version, payload hash, timing and optional reference. The canonical event points back to evidence observation IDs. Source names and versions must contain no credentials; credentials, raw documents and executable payloads do not belong in these contracts or fingerprints. The package has no network activity on import.

`EventSource` declares health and typed capabilities. Optional polling, fetching and scheduled-source protocols expose distinct operations; a provider need not implement all of them. `SourceHealth` distinguishes healthy, degraded, stale, unavailable, schema mismatch, rate limited and unknown. An empty `SourcePollResult` with healthy state means **no events**. Unavailable or schema mismatch means **source failed**. Parse failure and staleness diagnostics are retained; absence is never fabricated from failure.

Prefer first-party primary evidence for underlying facts when available. The precedence contract has a default ordering and optional explicit per-fact overrides. It does not silently adjudicate conflicts. Keep other observations and report conflicts rather than replacing them. EIE-2 adds the Fed, BLS, and BEA official-source adapters. EIE-3 adds SEC EDGAR metadata. EIE-4 adds offline corporate and earnings extracts; live vendor adapters remain for later phases.

For scheduled events, the index requires a raw observation for every claim and selects from the highest-ranked available source class. Disagreement sets `CONFLICTING_SOURCES`; equal-ranked disagreement also requires explicit adjudication reason codes. A coverage ledger records source health for asserted intervals and scopes as of a knowledge time. This distinguishes a healthy empty result from unavailable, stale, or never-ingested schedule information. Coverage is source-specific; it is not a universal completeness guarantee. See [scheduled events](scheduled-events.md).

EIE-2 Fed, BLS, and BEA items use `OFFICIAL_PRIMARY` for facts those agencies publish directly. Agency identity, source version, adapter version, item version, official URL, and raw fragment hash remain in observation provenance. Parse/schema failures and outages produce typed health and do not become healthy empty calendars. A changed official time without an explicit reschedule/correction status is retained as a conflict with the prior time selected pending review. The current fixture extraction format requires a separately audited upstream extraction process. See [official sources](official-sources.md).

EIE-3 SEC metadata uses `OFFICIAL_PRIMARY` for the EDGAR source. Exact accession/CIK, accepted time, and a row hash remain in raw provenance. SEC identity ambiguity is explicit; source health and failed batches never imply a healthy empty filing history. SEC's current ticker map is an observed association and cannot certify complete historical listing validity. See [SEC EDGAR](sec-edgar.md).

EIE-4 extends the same source policy to `corporate.<lowercase_kind>` claim selection. The selected claim comes from the highest-ranked source class available at the query's knowledge time; all other claims and raw observations stay attached. Disagreement adds explicit conflict flags and reason codes, even when a primary source wins. Equal-ranked disagreement is never hidden. An audited interchange extract must declare provider ID, schema, source version, item references and kind capabilities. Healthy empty coverage is a dated, provider-specific assertion; unavailable, rate limited, malformed, or stale sources do not establish absence. EIE-4 adds synthetic offline extracts only; external vendor fetchers are outside this phase. See [corporate events](corporate-events.md).

EIE-5 applies precedence to `consensus.<lowercase_measurement_id>` and
`actual.<lowercase_measurement_id>`. Provider disagreements remain explicit claims;
no cross-provider analyst identity or automatic provider average is assumed.
Synthetic estimate adapters declare consensus/analyst/actual/guidance/vintage
capabilities and require raw evidence before typed observations. Missing vintage
provenance prevents backtest-safe surprise. Covered empty, unknown coverage,
failed provider, and stale consensus have distinct outcomes. Runtime latency and
failure counters remain in diagnostics and are projected out of result fingerprints.
See [expectations and consensus](expectations-and-consensus.md).

## EIE-6 acquisition policy

Live transport uses trusted configured endpoints, bounded HTTP and independent
operational settings. Raw payload retention defaults to hash/metadata only;
bounded bytes require explicit ALLOWED permission. UNKNOWN/RESTRICTED cannot
enable raw retention. Quarantine suppresses repeated normalization of identical
malformed content. Source failure never certifies event absence. SEC contact
identity stays out of domain settings and persistence.
See [live ingestion](live-ingestion.md) and [operations](operations.md).

## Historical storage and trust

Raw historical retention defaults to metadata only. Bounded raw requires explicit
allowed permission and size bounds; restricted/unknown entitlement never enables
raw storage. Source class is pinned in the historical definition and cannot be
upgraded by provider bytes or normalized output. Contacts stay in runtime configuration.
Acquisition clock, source availability and replay clock are separately recorded.
Exact availability claims require provider evidence and are qualified by coverage/
reconstruction labels. See [archive policy](source-archive.md) and
[completeness](data-completeness.md).

## EIE-10 relationship source policy

Relationship adapters accept explicitly structured claims from configured source classes
and capabilities. Source bytes cannot set trust or enable implied removal. Primary,
commercial and audited supplied claims retain alternatives; categorical precedence,
confirmation counts, disagreement, estimated values and freshness are descriptive.
No opaque confidence or empirical-return edge weight is added.

Raw graph observations retain payload hashes and metadata; the adapter never archives
native vendor documents or invokes transport. Source-item receipts retain the normalized
typed interchange and lineage needed for deterministic validation. Callers must have
permission to retain those normalized fields. Optional acquisition/archival of original
vendor bytes still uses the EIE-6/EIE-7 retention and license policy; this adapter does
not grant an entitlement or enable archival. Structured identifiers/exposures may be
supplied from a SEC disclosure, but filing prose is not parsed or fuzzy-resolved here.
See [relationships](entity-relationship-graph.md) and [exposure](exposure-graph.md).

## EIE-11 independent origins and conflicts

Evidence providers configure a source family separately from source name/class. Syndicated
mirrors can share a primary origin; repetitions/revisions never inflate independent support.
Categorical precedence retains every alternative, and equal-rank disagreement can remain
unresolved. Predicate-specific policies resolve fields independently. Normalized retention
requires configured entitlement; raw bytes remain under EIE-6/7 archive policy. See
[claim resolution](claim-resolution.md) and [claim/evidence](claim-evidence-graph.md).


## EIE-12 text extraction

Text providers explicitly configure normalized-field entitlement, raw retention,
source family, currency and reference timezone. Models are processing provenance,
never a new source class. Original licensed bytes are content-addressed once under
bounded raw permission; metadata-only queries cannot gain another provider's retained
body. Missing content produces a validation warning, not fabricated verification.

See [structured extraction](structured-extraction.md) and [text ingestion](text-ingestion.md).
