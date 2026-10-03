# Claim/evidence replay and acquisition lineage

Queries always name an evidence dataset and explicit UTC integer nanosecond cutoff.
Optional `valid_at_ns` is independent of knowledge time. Claim revision history derives
knowledge ends only from revisions visible at the cutoff, so future corrections cannot
leak their existence or timing. Event history similarly selects visible source descriptor
revisions before candidate generation and grouping.

## Saved views and concurrency

Source items commit raw metadata, acquisition lineage, references, claims, relationships,
canonical claim resolutions, optional event groups and an immutable checkpoint receipt
under the existing SQLite `BEGIN IMMEDIATE` transaction. A late failure rolls back the
whole logical item. Independent provider commits at equal knowledge time are allowed;
per-thread revisions must advance and completed item clocks cannot move backward.

`EvidenceEngine.freeze(queries, cutoff, valid_at)` recalculates and persists complete
claim/event views, then seals the dataset cutoff in that same transaction. Future inserts
at or before a sealed cutoff fail. Pure unsaved queries describe all committed evidence
currently visible; freeze before treating an incomplete equal-clock ingest wave as final.
The two-connection race test retains both conflicting sources and reproduces the same final
resolution after restart. Another test proves no second writer can enter between validation
and sealing. No wall clock or operational worker setting participates in results.

## Historical imports

`EvidenceDataset` optionally pins a completed EIE-7 build and its exact content fingerprint.
Reconstructed `AcquisitionLineage` pins the retained historical fact and exact EIE-7
acquisition ID, parser, normalization settings, reconstruction quality, source availability
and actual local observation/ingestion. The repository verifies the build's fact/acquisition
link, parser and adapter versions, resource and actual acquisition clocks. Authoritative or
source-timestamp reconstructed availability can drive historical knowledge; coarse/unknown
reconstruction uses actual acquisition time without fabricating precision.

Live and later backfilled copies of the same provider/item/version/content use one logical
raw observation where appropriate. Separate acquisition rows preserve their actual clocks
and provenance. Reference identities exclude acquisition clocks, enabling reuse without
copying fragments; provider assertions pin their acquisition and dataset separately.
Different historical builds cannot be combined silently. To reconstruct a backdated ledger,
use a separate dataset and ingest it in knowledge order rather than bypassing sealed live views.

## Recalculation and security

Saved resolutions store IDs and normalized values, never raw bodies. Restart checks index
agreement, raw/hash/parser linkage, revision chains, relationships, candidates, group
membership and fingerprints. `validate_store` performs a bounded complete scan under one
transaction. Exact pinned artifacts remain byte-identical on reload and independent rebuild.
Optional supplied raw bytes verify precise fragments in memory; raw archival requires the
existing separate entitlement-controlled archive path. Source data never invokes imports,
commands, filesystem reads or network requests.

Run the [offline example](../examples/evidence_demo.py) from the repository root:

```bash
PYTHONPATH=src:. python examples/evidence_demo.py
```

The [profile](../benchmarks/evidence_profile.py) measures ingestion, blocked candidates,
resolution, reference lookup, historical replay, fully checked restart and storage size.
No CI timing threshold or claim of production-scale throughput is implied.


## EIE-12 text extraction

`TextQueries.audit(run_id, known_as_of_ns)` traces accepted candidate/verification,
claim/assertion, original reference, segment/map, document and raw observation.
Processing versions and extraction availability are explicit. Corrections/retractions
and later model runs append history without changing earlier views. Metadata-only
validation reports the inability to recalculate source-body spans.

See [structured extraction](structured-extraction.md) and [text ingestion](text-ingestion.md).


## EIE-16 source diagnostics

EIE-16 adds explicit local bitemporal audit results over retained EIE-11 revisions. Claim validity remains separate from owner knowledge and actual acquisition/processing availability. Historical source reconstruction is qualified and cannot enter an earlier local reliability baseline. Later corrections and instrumentation create new diagnostic frames/results.

See [source reliability](source-reliability.md), [latency](information-latency.md),
[bitemporal audit](bitemporal-intelligence.md) and [coverage](source-coverage.md).
