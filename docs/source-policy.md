# Source and provenance policy

Source class remains categorical: official primary, exchange primary, company primary, regulated filing, licensed news, secondary news, or unverified. `SourcePrecedencePolicy` carries a default complete ordering, but fact-specific precedence and conflict adjudication are deferred. Raw observations are preserved even when sources conflict. A future empirical reliability metric must remain separate from the categorical class.

Every raw observation records source identity, source item/version, adapter version, payload hash, timing and optional reference. The canonical event points back to evidence observation IDs. Source names and versions must contain no credentials; credentials, raw documents and executable payloads do not belong in these contracts or fingerprints. The package has no network activity on import.

`EventSource` declares health and typed capabilities. Optional polling, fetching and scheduled-source protocols expose distinct operations; a provider need not implement all of them. `SourceHealth` distinguishes healthy, degraded, stale, unavailable, schema mismatch, rate limited and unknown. An empty `SourcePollResult` with healthy state means **no events**. Unavailable or schema mismatch means **source failed**. Parse failure and staleness diagnostics are retained; absence is never fabricated from failure.

Prefer first-party primary evidence for underlying facts when available. The precedence contract has a default ordering and optional explicit per-fact overrides. It does not silently adjudicate conflicts. Keep other observations and report conflicts rather than replacing them. Actual official, SEC, earnings and commercial provider adapters start in later phases.
