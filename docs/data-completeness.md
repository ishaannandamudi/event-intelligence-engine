# Historical completeness and reconstruction

Completeness is reported per requested resource and partition, with explicit gap
categories and reconstruction-quality counts. There is no confidence score and no
universal complete-history guarantee.

| State | Meaning |
|---|---|
| Completed | Last page reached with an explicit exhaustive source claim |
| Known empty | Exhaustive traversal produced no matching normalized facts |
| Gapped | Unsupported, partial, unknown, parsing, pagination, archive or temporal limitations |
| Failed | Bounded transient attempts exhausted; job remains resumable |
| Pending/running | Work has not durably completed |
| Split | Parent traversal produced bounded child work; count leaves separately |

Known empty is never inferred from failure, absent source registration, unknown
coverage, unsupported range or current snapshot. A partial provider page remains
partial even when it contains useful records. Unsupported and missing point-in-time
history can be reported without a network request. Missing roots/children and damaged
lineage invalidate validation. No pending/failed partition can be finalized.

Gap records carry provider/resource, affected interval/partition, reason, discovery
time, retryability and sorted evidence codes. Discovery time is operational; semantic
gaps enter the immutable content fingerprint. Reasons distinguish unavailable source,
unsupported history, missing point-in-time history, missing partition, pagination,
archive, parsing, schema mismatch, partial provider coverage, unknown completeness,
license restriction, unresolved identity and temporal ambiguity. Retention restrictions
are explicit policy failures; metadata-only operation is allowed, with absent raw
reprocessing capability disclosed by the manifest's retention policy.

| Reconstruction quality | Exact source cutoff permitted? |
|---|---|
| Authoritative point-in-time | Yes, under the audited provider's availability proof |
| Source timestamp reconstructed | Yes, under explicit publication/acceptance assumptions |
| Date only reconstructed | No |
| Current snapshot only | No |
| Unknown | No |

Source available time is separate from EIE acquisition/ingestion. Audited extract
providers must substantiate their exact cutoffs and revisions; serialization cannot
prove their historical claims. Source timestamp reconstruction is not proof that the
archived current row was byte-identical at its original publication. SEC acceptance
supports filing event timing, while current metadata does not prove exhaustive
correction/vintage history. SEC builds therefore retain unknown-completeness gaps,
explicit source assumptions and reconstruction-quality labels. Verified CIK bindings
establish stable identity; current tickers and company names do not establish old
listings. Exact as-of projections must be interpreted together with these labels.

Estimate/actual/guidance histories must preserve provider vintages, source revisions,
units, periods and publication times. Later-discovered earlier revisions are retained;
later corrections are filtered by their own exact availability. Missing/conflicting
chains are disclosed. The live BLS calendar and current estimate values provide no
exhaustive historical macro or consensus reconstruction capability by themselves.

Metadata-only manifests can be valid for recorded normalized content while lacking
raw rebuild capability. `VALID_WITH_KNOWN_GAPS` means all recorded integrity checks
passed and declared limitations remain. `INVALID` means integrity, plan, reference,
version or content validation failed. Neither result authorizes using unproven
historical availability in a causal study as if it were verified.

## EIE-8 market/event limitations

Studies propagate EIE-7 semantic gaps and reconstruction quality. Unknown event times
limit contamination coverage. Market gaps, missing pre/post endpoints, coarse source time,
closures, partial bars, absent benchmarks/quotes/options and low counts are explicit flags.
Missing prices never become zero returns. Relative volume requires prior matching supplied
session buckets; absent/zero baselines remain unavailable. Unadjusted prices carry action
risk, and known split windows suppress price/volatility measures. Per-offset sample counts
and unavailable reasons accompany decay/aggregates. See [event studies](event-studies.md).
