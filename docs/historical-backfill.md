# Historical backfill (EIE-7)

Engine 0.8.0 constructs explicitly requested, immutable historical builds. It uses
`backfill`, `archive` and `datasets` packages beside the existing live ingestion
runtime. No event studies, trading, service API or external engine integration is
implemented.

## Contracts and plans

`DatasetDefinition` pins namespace, a nonempty UTC nanosecond `[start,end)` range,
provider/resource list, capability and coverage assumptions, source class,
identity bindings, adapter/parser versions, engine version, settings, partition
and retention policies. Providers and resources are sorted and unique. Creation
time is operational and excluded from identity. Event families and entity scope
identifiers filter canonical events and schedules; supporting domain records and
archived input pages remain available for audit. Current SEC ticker maps never
supply historical listings.

`BackfillJob` derives deterministic dataset, build and job IDs. Build version and
optional parent distinguish immutable branches. `plan(job)` is pure: it reads no
database, creates no archive and performs no network request. Each resource is
covered by consecutive bounded intervals, also split at capability boundaries.
The plan count is checked before allocation. Unsupported intervals remain visible
in the plan and later become explicit gaps.

`HistoricalSource.fetch(partition,cursor,stop)` returns bounded bytes, safe source
reference, opaque continuation, coverage claim and optional split signal.
`HistoricalProcessor.normalize` uses the pinned parser and returns historical
facts backed by raw observations. Registrations must match the definition,
including source trust and identity context. Audited JSON extracts are a bounded,
allowlisted interchange for structured histories, with one source fragment per
record; they are not a general collector or an arbitrary plugin loader.

## Scheduling and recovery

A coordinator rotates providers and resources one page at a time, with 1–8 workers
and at most that many submitted tasks. HTTP providers use the existing bounded
transport, retries and shared rate limiter. Page count, payload, item count, cursor
length and split depth are capped. Repeated/circular continuations and repeated
body bytes with a further continuation terminate as pagination gaps. A final
overlapping page is safe and preserves lineage while suppressing duplicate facts.

A too-many-results response splits an uncommitted partition at its exact midpoint;
parent, children, depth and input are retained. Splits after committed pages are
refused as gaps. Splitting cannot exceed the job's partition cap.

Each attempted page is recorded before fetching. Publishing an archive object
precedes the SQLite transaction. Accepted raw observations, facts, lineage,
acquisition metadata, gaps and progress are committed together. Progress cannot
advance without durable linked pages. A crash before commit rolls all linked data
back; a crash after commit resumes beyond the committed page. Unreferenced archive
objects can remain after a crash; they are never claimed as committed inputs.

Jobs move through planned, running, paused, failed, cancelled and completed states.
A page budget pauses after admitted work finishes. Pause/cancel stops admission;
bounded active work finishes or notices the stop event. Cancelled jobs cannot be
resumed; choose a new build. Transient acquisition failures get a bounded number
of attempts per page. An explicitly resumed failed job retries failed partitions,
clearing only retryable gaps. Other providers continue. Database conflicts/failures
abort the run and preserve a failed resumable job when the database remains usable.
Process death can leave `RUNNING`; reopening and running resumes durable progress.
Custom source implementations must honor transport bounds and cooperative stopping;
Python threads cannot forcibly interrupt an arbitrary blocking third-party function.

## SEC native vertical slice

`sec_history` takes an explicit unique CIK watchlist, operator contact User-Agent,
clock and optional transport. The same SEC client/limiter is shared across CIKs.
It traverses current submissions and the official referenced historical metadata
files. Continuations only name bounded, CIK-specific files on `data.sec.gov`.
Native EIE SEC parsing/normalization is reused. Filing bodies, exhibits, XBRL and
unrequested ticker maps are not fetched.

Verified issuer CIKs must be explicitly supplied and are pinned in source identity
bindings. This reproduces the existing live company event identity without
backdating current company names or ticker mappings. Unbound filers retain unresolved
identity and an explicit gap. Form 4 actor/issuer distinctions remain intact.

Every partition visits current metadata and its references, then filters acceptance
times by `[start,end)`. Referenced bytes deduplicate in the archive; requests are
not cached across date partitions, because each acquisition remains explicit.
This favors auditable traversal; large CIK/range jobs can incur repeated requests.
SEC results carry source-timestamp reconstruction quality and unknown vintage
completeness, even when all referenced pages were traversed.

## Source knowledge versus acquisition

Raw `source_observed_at_ns` and `ingested_at_ns` are real local acquisition clocks.
Historical facts separately carry `source_available_ns`, source revision and
reconstruction quality. Exact availability requires a documented authoritative
or source-timestamp assumption. Date-only/current/unknown histories retain no
fabricated exact cutoff and are excluded from exact historical queries. The
source availability projection omits EIE observation/ingestion clocks.

Macro and estimate history requires explicitly audited source vintages. Existing
live BLS calendars and current estimate snapshots do not establish exhaustive
historical release/correction coverage. There is no automatic licensed estimate
fetcher or universal macro vintage downloader.

See [manifests](dataset-manifests.md), [archive](source-archive.md),
[completeness](data-completeness.md) and [operations](operations.md).
