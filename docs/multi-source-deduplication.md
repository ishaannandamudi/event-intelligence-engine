# Multi-source structured event deduplication

EIE-11 uses immutable `EventDescriptor` inputs and `DuplicateCandidate` decisions:
`MERGE`, `KEEP_SEPARATE` or `UNRESOLVED`. Candidates pin both exact descriptor revisions,
source event IDs, categorical match/separation reasons, optional exact time delta and policy.
Pair ordering is canonical. A shared source-item bundle never establishes event identity.

## Blocking and bounds

Deterministic blocks use existing EIE identity, scoped provider/resource/event key, SEC
accession, EIE-4 corporate identity, exact issuer/type/period, official release identity
and issuer/type/UTC-date buckets. Adjacent date buckets accommodate midnight crossings.
Different macro series have distinct event-type blocks. `ce_blocks` retains an indexed
auditable blocking projection. Resolution constructs a bounded per-call block index;
it does not compare every event with every event.

Defaults: 1,000 claims and 4,096 references per source item, 64 relationships per assertion,
128 candidates per event, 256 descriptors per group, 10,000 query records and traversal
depth at most 8. Candidate limits preserve stable ordering and expose truncated event IDs.
Truncated events cannot acquire new multi-event merges. Group limit exhaustion is explicit.
Large queries fail rather than imply completeness. Histories count retained revisions toward
their query limit; callers partition larger ledgers or configure a bounded larger limit.

## Matching

Hard separation guards run before any merge: incompatible event types, different known
entities, different reporting periods, distinct SEC accessions, conflicting party roles,
distinct effective/action dates, different EIE-4 identities, release stages and incompatible
terminal lifecycle states. Even an otherwise strong shared key cannot override these guards.
Conflicting hard annotations under a single existing canonical event ID fail closed; callers
must retain distinct correctly identified canonical events before resolving aliases.

Strong matches include exact accession/corporate/EIE event identity, a scoped provider key,
or exact issuer + fiscal period + earnings event type. Macro matches require exact series,
period, stage and sourced official release identity. M&A requires exact target/acquirer
roles as well as a strong deal identity. Missing parties preserve ambiguity.

Time proximity alone remains unresolved. Tolerances are explicit by event-type code;
unconfigured types have zero tolerance and no universal proximity merge. Date-only timing
does not invent a timestamp. A stable semantic identity can survive an exact-time refinement
or reschedule; time disagreement remains available to the separate claim resolver.

False-merge fixtures include earnings versus management departure, two same-day SEC filings,
nearby dividends, different deal counterparties and same-day distinct macro series.
False-split fixtures include multi-provider earnings, exact refinements, mirrored accessions,
normalized corporate IDs and official/secondary macro observations.

## Groups and history

`EventResolutionGroup` is an alias partition with a deterministic identity derived from
sorted member event IDs and policy. The representative ID is a stable alias for lookup;
field provenance is resolved separately and does not give one source ownership of every
field. Every cross-pair needs a retained merge decision before groups combine. An A/B and
B/C match cannot override an A/C separation or an unexamined cross-pair.

Source descriptors are append-only. A later explicit descriptor correction can cause
split→merge or merge→split. Historical runs retain exact input descriptor, candidate and
group membership links. Earlier event revisions, raw observations and evidence remain.
See [evidence replay](evidence-replay.md) for cutoff/sealing and build boundaries.


## EIE-12 text extraction

Text candidates and source-local event descriptors remain distinct for each source.
Exact duplicate content can reuse bytes without combining observations. Configured
source families preserve syndication lineage. EIE-12 performs no fuzzy article matching
or event alias selection. EIE-11 alone compares descriptors and resolves multi-source
facts; measurement slots include exact issuer/period/metric qualifiers and units.

See [structured extraction](structured-extraction.md) and [text ingestion](text-ingestion.md).
