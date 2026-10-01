# EIE architecture through EIE-2

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

The raw/canonical boundary is structural: the two immutable classes do not inherit from each other. A canonical event names one or more raw observation IDs. The store verifies that evidence exists, its ingestion precedes the event's availability, and its source classes match. No provider schema leaks into core events. A future normalizer implements the protocol and must preserve evidence IDs.

Canonical identity uses event type, scope and a stable source/semantic key. Source-specific adapters must supply stable keys such as an official release period or meeting ID; a schedule timestamp or ticker alone is unsuitable. Revision number and availability time identify knowledge changes. The in-memory store retains all revisions. The store is an offline example and protocol implementation, not durable storage or restart recovery.

Time is signed-64-bit nonnegative UTC nanoseconds. Scheduled/effective time describes the world; revision `available_from_ns` describes what EIE could know. Replay returns the latest revision available at the requested time. A later revision cannot rewrite an earlier view. Listings also have valid and available intervals. Exact listing resolution accepts a knowledge cutoff and an optional distinct valid-time query, so a ticker that was valid earlier can be resolved from information known later.

Future downstream consumers should receive typed event intelligence snapshots after later phases. EIE does not import MCE or AME and does not make trading decisions.

See [scheduled events](scheduled-events.md) for the full EIE-1 contract.
