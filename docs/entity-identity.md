# SEC filer, issuer, security, and listing identity

An SEC CIK identifies a filer. Filers can be operating companies, individual reporting persons, or other entities; a CIK alone does not establish that every filing is an issuer filing. EIE stores a stable `SecFilerRevision` for a named CIK and creates a distinct `SecIssuerRevision` only with issuer evidence. A native SEC ticker/exchange association can bootstrap an issuer when policy allows. A submissions record can bootstrap an issuer when `entityType` is `operating`; an already known issuer can receive later name/alias revisions. When issuer bootstrap is disabled or evidence is insufficient, raw filing evidence and diagnostics remain available. The provisional CIK-based issuer ID is stable; name changes append knowledge-time revisions and retain aliases. This does not merge distinct CIKs through fuzzy names or assert corporate succession.

`SecEntityRegistry` separates issuer, filer, security, and listing. `sec_issuer_id(CIK)` and `sec_filer_id(CIK)` are distinct domain-separated IDs. A security needs its own verified stable non-ticker identifier and a known issuer. A `SecListingRevision` links that security to an exact ticker and exchange with valid-from/valid-to and available-from times. The native SEC ticker map has no security identifier or authoritative listing validity dates, so ingestion records it as an observed association snapshot and does **not** invent a security or listing interval. Submissions `tickers`/`exchanges` metadata is retained as evidence, not automatically promoted to verified security identity.

Resolution uses exact ticker **and** exchange at an explicit valid time, limited by an explicit knowledge time. It returns `RESOLVED`, `UNRESOLVED`, or `AMBIGUOUS` with candidate issuer IDs and reason codes. The same ticker can move to another issuer in a later snapshot; older as-of views retain the former association. A single issuer may have multiple securities and listings. Issuer-scoped filings remain issuer-scoped instead of being copied onto each security. Filing symbol queries resolve the issuer at the requested valid time and then retrieve issuer filings known as of the knowledge cutoff. `historical_symbols` in a filing view reflects associations valid at its acceptance time and known at query time; later learned verified listing intervals can refine a later knowledge view without rewriting an earlier one.

Overlapping verified listing records can yield an explicit ambiguous result. Current SEC snapshots also can be ambiguous. Neither ambiguity is silently resolved by company name, ticker string alone, or arbitrary ranking. Corporate reorganizations, CIK merges, authoritative security master integration, and complete past ticker reconstruction require further evidence and are outside EIE-3.

EIE-4 corporate identities use this registry directly. Issuer-level facts remain issuer-level, while security-scoped facts require a verified security/issuer relationship; a supplied listing ID must match the issuer and optional security at its valid time. Earnings events from several providers match on stable issuer and fiscal reference period, not on ticker or scheduled date. Deal counterparties and spun-off issuers may be explicitly unresolved with quality flags. A symbol-change event requires one verified old listing, closes that listing at the change's effective time, and opens the new ticker/exchange at that same valid time, with both changes becoming known only at ingestion. A previously observed SEC ticker-map snapshot cannot silently restore a verified listing after that closure. Historical symbol queries preserve the old ticker before the effective time and the old knowledge view before ingestion. See [corporate events](corporate-events.md).

## EIE-10 graph identity reuse

Graph foundation nodes retain the exact issuer/security/listing contracts and IDs above.
CIK mappings and verified security issuer links cannot be rebound. Other typed entities
use sourced non-ticker authority/identifier hashes. Explicit primary identifiers permit
later aliases without creating a duplicate node. Classification identifiers must retain
their provider/taxonomy/version namespace.

Graph symbol claims pin an exact exchange and valid-time resolution against known listing
revisions. Ticker changes and reuse do not rewrite old resolved endpoints. Ambiguous or
missing endpoints remain raw provider claims without a canonical edge. Identity edges
must agree with the retained foundation links. See [relationship identity and taxonomy](entity-relationship-graph.md)
and [graph replay](graph-replay.md).
