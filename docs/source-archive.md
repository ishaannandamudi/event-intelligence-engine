# Source archive

`FileArchive` stores uncompressed bounded objects at `<root>/<sha256[:2]>/<sha256>`.
SHA-256 covers the exact page bytes. A distinct acquisition record retains provider,
resource, original safe source URL, local UTC acquisition time, original media type,
byte length/hash, partition/page, continuation, source coverage, retention policy,
parser/adapter versions and reconstructed/replayed provenance. Lineage connects
these objects to normalized records and dataset builds.

## Retention

Metadata-only is the default and retains hashes, acquisition metadata and normalized
records without page bytes. Bounded raw storage requires explicit `ALLOWED` retention
permission and respects both archive and retention size caps. `UNKNOWN` or
`RESTRICTED` permission cannot enable raw storage. Operator/provider licensing
policy must establish permission; the engine does not infer entitlement from a URL
or from a supplied historical extract. No cloud storage SDK or runtime dependency
is needed. Metadata-only builds can be queried and audited at the normalized level,
but cannot be deterministically reprocessed from absent raw source bytes.

## Atomicity and filesystem safety

Provider bytes never become path names. Hash syntax and bounds are checked. The
configured trusted root is anchored by an open directory descriptor. Root, prefix
and object symlinks are refused; directory-relative `O_NOFOLLOW` operations prevent
escape. Objects must be regular files, preventing FIFO reads from blocking.
A temporary file is flushed/fsynced, atomically linked to the immutable hash name,
and the directory is fsynced. Existing objects are verified and never overwritten.
Temporary files are cleaned on ordinary failure. Filesystem ACLs and parent directory
ownership remain the operator's responsibility; a configured parent path may be
canonicalized before opening the trusted root.

Publication precedes the SQLite metadata/data/progress transaction. A power/process
failure can leave an unreferenced object; it cannot produce an accepted page or
manifest reference without committed metadata. Hash/length verification is mandatory
on reuse/rebuild and validation. Missing/corrupt objects are explicit integrity
errors. A frozen manifest is not changed to hide later storage damage; validation
returns `INVALID`. Refetching would require a new build or explicit repair operation.
Automatic garbage collection, compression, encryption, remote object stores and
in-place repair are deferred.

A page that is published successfully but fails normalization/cursor validation has
separate failed-acquisition metadata, is included in input fingerprints, and can
reproduce its failure from bytes. A source failure before it returns a page has only
a gap/operation record, not an invented archive reference. Native SEC reference
parsing occurs during source traversal; malformed current reference metadata can
therefore fail before a historical page is returned for archival.

## Operational information

Historical operation records retain status, provider/partition, committed byte/hash
counts, duration and sanitized failure categories. They omit bodies and arbitrary
exception messages. Contact User-Agent lives in runtime configuration, not dataset
identity, manifests or persisted normalization settings. Source references require
HTTPS without credentials, query or fragment. Providers must supply non-secret
continuations; application registration is the trust boundary. Original acquisition
time and replay time remain distinct. Archive replay never claims that EIE observed
an old record when it was originally published.
