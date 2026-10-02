# Immutable historical datasets

Definitions pin transformation and acquisition policies. Dataset identity excludes
creation time; build identity adds an explicit version and parent. Finalization
requires every planned root and terminal partition, including deterministic split
children. Failed/pending work cannot become a finished manifest.

A manifest includes the definition/job, engine/store/dataset schema versions,
partition results, archive hashes, normalized/domain/event/revision/raw counts,
duplicate counts, explicit gaps, completeness dimensions and completion time.
It is frozen with SQL triggers guarding the manifest and its build's records,
partitions, observations, acquisitions, lineage and gaps. New discoveries belong
to a new build. Operations remain appendable without changing content identity.

## Fingerprints and lineage

Content identity includes definition semantics, archived inputs (including retained
parse failures), normalized content, source revisions/availability, pinned parser
and adapter versions, settings, partition results and semantic gaps. Operational
creation/completion/acquisition/replay clocks, build labels, random IDs and hostnames
are excluded. An acquisition's own ID still identifies its real operational origin.
Two builds over identical pinned inputs/settings therefore have identical content
fingerprints while retaining distinct acquisition provenance.

The queryable lineage chain is:

```text
source provider/resource + archived page hash
  -> acquisition + partition
  -> raw observation identity
  -> typed domain / canonical fact
  -> build manifest + versions/settings
```

Facts and lineage are appended/indexed in SQLite, without rewriting the entire
EIE-6 durable session per page. Finalization streams the record digest. Queries and
validation can materialize the selected build; they do not serialize all live sessions.

## Rebuild and extension

`rebuild` requires a finalized source build and an explicit new build label.
`NO_REPROCESS` rejects the operation. `REBUILD_SAME_VERSION` requires the original
parser/adapter versions; `REPROCESS_WITH_NEW_VERSION` requires at least one version
change. All requested parsers must be available as trusted code supplied by the
application. The CLI exposes the installed same-version processors only.

Eligible archive objects are verified before creating the rebuild. Same-version
output must match the original content fingerprint before the new manifest freezes;
unversioned code/output drift leaves a failed job and no new manifest. Original local
raw acquisition clocks are retained; the new acquisition records `ARCHIVE_REPLAYED`
and the actual replay time. Missing/corrupt/restricted bytes fail explicitly; no
refetch repairs the original. Retained failed parse pages reproduce their gap.
When a gap has no acquired page, replay retains the declared absence; it does not
claim to have reconstructed unavailable data. Different code versions create an
isolated namespace and never rewrite the original records.

`reuse_completed` is an explicit API for extension builds with the original build
as parent. Exact matching completed/known-empty intervals can be copied after
checking original partition fingerprint and retained byte integrity. Source,
settings, identity, engine, parser, adapter, partition and retention policies must
match. New intervals fetch normally. Gapped and dynamically split roots are not
reused. Input reuse is explicit use of the frozen snapshot, not a claim about a
provider's current content. General build diffs and automated repair are deferred.

## Historical queries and validation

`DatasetReader(repository, build_id)` requires a finalized manifest. `records(T)`
returns immutable fact wrappers with exact source availability at or before `T`;
latest selection uses source revisions, independent of discovery order. Conflicting
same-revision/source-time records are preserved and latest selection fails explicitly.
Missing/reversed/conflicting domain revision chains create a temporal gap.

`events_known_at(T)` reuses the canonical event and `KnownEvents` contracts, direct
scope filters, source precedence and optional effective/scheduled date range.
Projection sets historical source availability and leaves local observed/ingested/
last-updated clocks absent. `filings(CIK,T)` reconstructs amendment links across only
visible pages. `registry(T)` reuses stable issuer/security/listing contracts and exact
valid-time resolution; ticker reuse never invents issuer identity. Supporting domain
facts keep honest acquisition clocks: do not insert those wrappers into a live
engine while pretending its local clock was in the past.

`validate_dataset` checks root/split plans, page/cursor chains, partition digests,
archive hashes/metadata/bytes, indexed fact contracts, references, lineage/settings/
versions, counts, completeness, record digest and database integrity. Results are
`VALID`, `VALID_WITH_KNOWN_GAPS` or `INVALID`. Validation does not refetch or mutate.
A valid qualified manifest certifies its recorded gaps and assumptions; it does not
certify universal source coverage. Metadata-only validation cannot validate absent
raw bytes. Manifest export uses allowlisted typed JSON.

## EIE-8 linkage

Each study pins a finalized build ID, manifest content fingerprint, selected canonical
revision and historical fact ID. Study-run manifests seal sorted study IDs/result hashes,
counts and the event build; each member retains complete market/window/policy provenance.
Mixing incompatible event builds is refused. Adding store migration 1.2 preserves existing
1.1 manifests and content fingerprints; validation accepts that supported prior layout.
Old engine-pinned archive rebuilds still require their matching installed processing code
or explicit new-version reprocessing. See [event studies](event-studies.md).

## EIE-9 model linkage

Impact specs/results pin the raw study and its selected historical fact/revision, build
content and reconstruction quality, plus exact market, benchmark, factor and control/
feature dataset hashes/versions. Impact-run manifests seal model IDs, result fingerprints,
dataset pins, selected members and failure/contamination counts. Different historical
builds cannot be silently mixed. Store migration 1.3 preserves earlier manifests and
studies byte-for-byte; it adds separate immutable model/fit/result/sensitivity/run tables.
Retained exact inputs permit recalculation but do not certify a provider's asserted
point-in-time completeness. See [counterfactual impact](counterfactual-impact.md).
