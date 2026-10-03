# Information latency

`InformationClocks` preserves seven distinct typed clocks:

| Clock | Meaning |
| --- | --- |
| VALID_TIME | Explicit fact validity/effective time, independently of disclosure |
| PUBLICATION_TIME | Provider's retained publication evidence |
| SOURCE_AVAILABLE_TIME | Explicit historical source availability from acquisition lineage |
| OBSERVED_TIME | Actual EIE acquisition observation |
| INGESTED_TIME | Actual durable ingestion/acquisition time |
| NORMALIZED_TIME | Explicit normalization availability instrumentation, or EIE-12 accepted extracted-claim processing availability |
| SNAPSHOT_AVAILABLE_TIME | Explicit downstream result availability instrumentation |

The chain is publication → source availability → observation → ingestion → normalization
→ snapshot. Live source availability is missing when its owner provides no separate
clock. Generic event knowledge time, query cutoff and ingestion are never substitutes
for normalization/snapshot clocks. EIE-12 processing availability includes extraction
and verification of the normalized accepted fields; it is not an isolated CPU-stage
measurement. Other producers must supply explicit trusted `ProcessingTiming` evidence.

Timing instrumentation pins the exact source revision fingerprint, evidence reference
and actual evidence availability. It is a caller-supplied measurement contract; EIE
cannot independently establish external wall-clock truth. Contradicting an existing
extraction normalization clock is refused. Source availability and diagnostic-frame availability remain separate. New instrumentation
appends a new compact frame for the same source revision; earlier frames stay immutable.
Each query selects the latest frame already available at its cutoff, and source samples
remain anchored to original local source availability. Instrumentation never inflates counts.

`latency(row_id, known_as_of_ns)` returns signed components for valid→publication,
publication→source availability, source availability→observed, publication→observed,
observed→ingested, ingested→normalized, normalized→snapshot, publication→snapshot and
source availability→snapshot. Missing endpoints yield missing latency, never zero.

Exact UTC clocks yield integer nanoseconds. Supplied date/session clocks yield conservative
calendar-day difference intervals under an explicit source timezone, with no point estimate or invented within-session ordering.
`missing_clocks` retains the specific missing clock names. Coarse clocks require an explicit IANA source timezone for conversion; otherwise latency
remains unavailable with MISSING_TIMEZONE. Session labels do not narrow the day interval.
Exact clocks retain their owner's UTC nanoseconds. No timezone is invented.

Impossible pipeline order retains the signed value and NEGATIVE_LATENCY; provider-clock
inversions additionally carry CLOCK_SKEW. Core raw/event owners may reject impossible
source timestamps before indexing. Negative valid→publication delay can be legitimate
advance disclosure and is retained. No offset, absolute value, clamping or skew correction
is applied.

Distributions expose exact count, median, mean, linear-interpolated configured quantiles,
MAD, min/max, missing/coarse/skew counts. Coarse and explicitly invalid pipeline samples
are excluded from exact distributions with explicit counts. No tail trimming occurs.
Source publication delay, collection delay, transport delay, normalized-field processing
and downstream availability remain separate from market impact and content uncertainty.
