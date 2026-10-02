# Live acquisition in EIE-6

Engine `0.7.0` adds bounded HTTP acquisition and a foreground polling runtime.
Runtime, ingestion and persistent-store schemas start at `1.0`; all prior schemas
remain `1.0`. Importing EIE performs no network activity. Both the runtime and each
provider registration are disabled by default.

```text
HTTP policy + retry + rate limit
       → PollableSource → bounded acquisition queue
       → AdapterProcessor → raw evidence + normalization
       → SQLite transaction: domain state + checkpoint
       → existing query / replay APIs
```

The source owns an explicitly configured resource, transport acquires bytes,
and the processor invokes an existing offline adapter. The scheduler parses
nothing. Queries and persistence never fetch missing data.

## Offline demonstration

From the repository root:

```sh
PYTHONPATH=src python examples/runtime_replay.py
```

This uses the checked-in **synthetic** SEC fixture through `FixtureSource`, opens
SQLite, polls once, commits data and checkpoint, stops and closes, reopens,
queries through `StoreReplayReader`, and polls the same payload again. There is
one canonical event, two checkpoint generations and one suppressed filing
duplicate. It requires no internet and creates a temporary database.

## Concrete HTTP sources

`sec_provider(cik, user_agent, clock, enabled=True)` acquires one configured
`data.sec.gov/submissions/CIK##########.json` resource. It uses the existing SEC
metadata adapter, contact User-Agent, default two requests per second, three
finite attempts, a two MB body limit, and no redirects. Recent rows in the response
are processed together. Historical file references are retained as metadata;
the runtime does not follow them or download filing documents or ticker maps.

The SEC publishes a user-wide ceiling of ten requests per second across machines.
EIE validates its local rate against that ceiling; operators must coordinate
other applications sharing the same access identity. See [SEC developer
resources](https://www.sec.gov/about/developer-resources) and the
[SEC developer FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions).
The contact is supplied by the operator; no personal identity is bundled.

`bls_provider(clock, enabled=True)` acquires the [official BLS calendar](https://www.bls.gov/schedule/news_release/bls.ics)
using the existing iCalendar parser, `text/calendar`, a one-hour poll interval,
and at most one request per minute including retries. Unsupported or changed
calendar structure fails explicitly.

Fed and BEA extract adapters can be registered behind an audited acquisition
source. Their offline extract requirements remain intact. EIE-6 adds no HTML
scraper, commercial vendor collector, paid news client, or historical crawler.

## URL and response policy

HTTPS and explicit lowercase host allowlists are required. Userinfo, query
strings, fragments, control characters, and non-HTTP schemes are rejected.
Provider bytes cannot choose endpoints or filesystem paths. Redirects default
to zero; standalone transport can explicitly enable redirects, revalidated
against the same host policy and capped at five. Rate-limited polling clients
require redirects disabled so each HTTP request consumes a rate slot.
Conditional headers are removed across hosts.

DNS results must all be public addresses, then the connection is pinned to one
validated address while TLS validates the original hostname. An explicit
development policy permits loopback HTTP; it does not permit arbitrary private
network access. DNS waits and abandoned resolver threads are bounded. Connect
and read timeouts are separate; body reads also use a monotonic deadline and a
hard byte limit. Standard-library HTTP header limits remain in force.

Requests advertise identity encoding. Compressed responses are rejected, so
there is no unbounded decompression. A 200 response must match an adapter's
declared content type. Empty 200 JSON goes through the normal parser and can
be quarantined; it is not silently interpreted as a healthy empty event list.

304 is healthy no-new-data only after a successful checkpoint exists. It
preserves the cursor, ETag and Last-Modified and advances the operational
generation without changing canonical state. 404 is unavailable. 429 is rate
limited. Selected 5xx and transport failures have finite retries; parser,
schema, certificate, URL, size and content-policy failures do not.

## Time and provenance

Acquisition completion supplies `source_observed_at_ns`. Worker preparation
supplies a separate `ingested_at_ns`. Source publication, acceptance, vintage,
scheduled and effective times retain the existing adapter meanings. Old source
times never move knowledge availability backward. Production UTC must be
synchronized; backwards acquisition/ingestion clock conflicts fail without
advancing the checkpoint. Durations, rate windows and poll deadlines use
monotonic time. Tests inject `ManualClock`.

Fetch, queue, preparation, parse validation, normalization, commit and total
durations are operational telemetry. The bridge performs a separately measured
parse-validation pass; normalization invokes the unchanged adapter's defensive
parse again. Normalization time therefore includes that second parse and
domain staging. Preparation also includes state loading and writer contention.
These measurements are not empirical source-latency intelligence.

See [provider runtime](provider-runtime.md), [persistence](persistence.md) and
[operations](operations.md) for recovery, concurrency and controls.

## Historical coexistence (0.8.0)

The EIE-6 live APIs, checkpoints, observation clocks and durable session layout stay
in use. EIE-7 stores historical builds in isolated tables on the same SQLite
transaction boundary, with append/index page commits. A matching live raw observation
is reused without changing its genuine clock; canonical logical identities remain
shared when issuer context is pinned. Historical source availability is never written
as a fabricated live observation time. Multi-CIK SEC backfill and dataset rebuild are
explicit foreground jobs; live polling does not automatically backfill or claim that
a recent source window covers downtime. See [historical backfill](historical-backfill.md).
