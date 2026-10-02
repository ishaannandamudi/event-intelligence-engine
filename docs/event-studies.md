# Descriptive event studies

EIE-8 answers **what happened around an event**. Raw event-window movements, sector
comparisons and response persistence do not establish causal attribution. Raw responses
are separate from EIE-9's [model-based abnormal returns](counterfactual-impact.md).
There are no trade labels or causal-identification claims.

## Reproducible inputs

`StudyEngine(DatasetReader(repository, build_id), market_provider, selection_as_of_ns)`
selects exactly the event revisions visible in one finalized EIE-7 build at the explicit
selection cutoff. Later corrections do not replace the selected original release.
Every study pins the build/content fingerprint, canonical revision, historical fact ID,
reconstruction quality, semantic gap fingerprints, market dataset ID/hash/version,
source, instrument, window, policy and engine/schema versions. Supporting source/local
acquisition clocks remain in the historical repository; they are not backdated.

The supplied `MarketSeriesProvider` boundary has identity, indexed range retrieval and
session metadata methods. `IndexedMarketProvider` is the deterministic offline reference
implementation. No market fetcher, vendor dependency or integration with another engine
is introduced. Instruments are stable generic IDs: issuer stock, share classes, target,
acquirer, index, rates or futures can each receive separate results. The caller supplies
an audited event/instrument association; the engine does not infer it from a ticker.

Bars carry explicit timeframe, OHLC, nonnegative volume, source, completion and availability.
Quotes carry bid/ask/sizes; options carry expiry/tenor/bucket, IV/skew and optional volume/OI.
All observations are immutable, ordered and validated. Prices can be zero for supplied
contracts, but a zero reference makes a return unavailable and logs require positive prices.
Dataset hashes are supplied provenance claims. Exact bounded inputs are also retained and
fingerprinted, so changed bytes under the same declared identity cannot overwrite a study.

## Anchors and windows

The caller chooses publication, SEC acceptance, exact scheduled time, reconstructed
source availability or actual EIE acquisition availability. The engine verifies the anchor
against the selected revision or linked filing/acquisition evidence. There is no timestamp
fallback. Publication is unavailable when the source did not provide it.

`EventWindow` pins anchor semantics, pre/post durations, explicit timeframe, elapsed or
supplied trading-session clock, sorted response offsets and coarse-session policy.
See [alignment](market-data-alignment.md) for BMO/AMC and market closure handling.

## Measurements

- Simple raw return: `price / reference - 1`; log return: `log(price) - log(reference)`.
- Cumulative curve: each sampled completed close relative to the chosen reference.
  Every point preserves its requested offset, resolved target and actual observation time.
- Pre return: first eligible pre-bar open to last eligible pre-bar close, separately from
  the reference-based curve. Missing boundary coverage makes it unavailable.
- Post return: reference to last eligible post-bar close; missing endpoint is unavailable.
- Realized volatility: unannualized `sqrt(sum(log-return²))` between adjacent eligible
  closes, with separate pre/post counts, difference and ratio. Missing active intervals
  are not bridged; supplied closures can be bridged. Minimum counts are explicit.
- Volume: each bar's raw/cumulative window volume and ratio to median prior matching
  session-offset volume. Daily bars use a daily bucket. Baselines require strictly
  earlier completion/availability and the configured minimum count. No future days or
  fabricated exchange time buckets enter a baseline; zero baselines give no ratio.
- Quotes: prior/current spread, post relative spread and quoted depth, and midpoint
  movement. Pre state must be available at the anchor; staleness bounds are explicit.
- Options: IV, skew, volume and OI changes between causally aligned matching supplied
  expiry/tenor/bucket snapshots. There is no options pricing/surface model.
- Benchmarks: supplied sector, market, industry or ETF-proxy raw curves and volatility,
  plus a descriptive asset-minus-benchmark endpoint difference. Adjustment/return type,
  timeframe and resolved session bounds must match. This is not an abnormal return.

Missing quotes/options/benchmarks do not prevent an asset study. Missing data stays `None`;
quality flags identify gaps, coarse timing, closures, partial bars, small counts, absent
baselines, unadjusted-action risk and incomplete event history. There is no confidence score.

## Contamination

An indexed view of existing selected EIE history flags same entity, macro/central-bank,
market-wide, exact-time co-occurrence, shared raw observation bundles and scheduled overlap
within explicit contamination durations. IDs, revisions, clock basis, timestamps and
availability are retained. Same-time ordering cannot change flags. No event is assigned
unique responsibility for a shared raw response. Unknown event times and EIE-7 gaps remain
explicit limitations. Contaminants are limited to the same historical selection cutoff;
new information cannot silently alter a persisted result. A full interaction model is deferred.

## Aggregation and queries

`DistributionQuery` requires as-of time, build, event class, cohort fingerprint, horizon
and minimum samples. Responses must have **anchor, event availability and complete-study
availability strictly before T**. The current event can be explicitly excluded even when
queried after completion. An unfinished reaction never enters a prior distribution.

Cohorts pin window, method, reference/alignment policy, timeframe, adjustment, return type,
event build/content and code/schema version. Incompatible cohorts and repeated per-event
samples fail closed; filter windows/revisions/instruments explicitly before aggregation.
`EventStudyFilter` supports event type/family, issuer, anchor range, surprise availability,
reconstruction quality and contamination exclusions.

Distributions preserve all raw samples and IDs, count, mean, median, quantiles, unscaled
MAD, median absolute response, positive/negative frequencies and median volatility change.
Quantiles use linear interpolation at `(n-1)*q` (the usual type-7 definition). No outlier
removal, automatic trimming or future-calibrated bins occur. Insufficient samples yield
unavailable descriptors and a structured reason.

Existing EIE-5 `SurpriseObservation` can condition a cohort using explicit numeric bucket
boundaries and measurement identity; equality enters the bucket to the right. It must be
bound to the selected event and available at its anchor. Context labels (including regime,
sector, size or session descriptors) carry source, version, observation and availability;
both clocks must precede/equal the anchor. No regime model or MCE coupling is included.

## Persistence and validation

`StudyRepository.append` atomically inserts immutable definitions and results into separate
indexed tables. Re-inserting identical content is idempotent; changed content under the
same study identity fails. `append_run` seals sorted members/counts/fingerprints atomically.
SQL triggers refuse definition/result/run updates and deletes. SQLite migration 3 adds
these tables and indexes, raising store 1.1 to 1.2 without changing prior domain schemas.

Retained inputs permit pure recalculation and byte-identical typed JSON round trips. Store
reads check definitions, scalar indexes, fingerprints and recalculated outputs. Appends
verify the historical fact, visible revision, reconstruction quality and manifest pin.
`validate_study(result, reader)` additionally checks selected event/anchor/contamination
against the original build; standalone validation cannot prove omitted contamination.
Results report valid, valid with warnings, or invalid, with structured reasons.

`StudyQueries` exposes response curves, strict-prior study views, distributions and
single/class decay. Each result can be read by ID for offline research even after its
reconstructed cutoff; as-of queries use strict availability filters. `available_ns` is the
reconstructed completed-input/replay cutoff, **not** a claim of actual historical EIE
computation or observation. Reconstruction quality and actual archive clocks remain separate.

Inputs are capped at 100,000 observations/metadata per series (configurably lower),
1,000 response offsets, and 20 MB serialized persisted result. Windows must finish by the
explicit market cutoff. See [influence/decay](event-influence-decay.md) for persistence of
observed response magnitude.

## EIE-9 consumption

A validated raw result is retained unchanged inside `EventImpactResult`; its cumulative
reference-price curve is never summed to form CAR. The impact layer derives compatible
period returns and applies a declared prior-fitted benchmark/factor/control model.
Information surprise, raw response and abnormal response stay separate. See
[counterfactual impact](counterfactual-impact.md) and [abnormal returns](abnormal-returns.md).
