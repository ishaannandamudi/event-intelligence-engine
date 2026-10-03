# Replay and causality

**Known now** means a record was available to EIE at or before `as_of_ns`. **Known later** must not enter that view. All canonical times are nonnegative signed-64-bit UTC nanoseconds since Unix epoch. Conversion from `datetime` requires an aware timezone, normalizes to UTC and uses integer arithmetic. Python `datetime` has microsecond precision, so conversion back rejects nanoseconds not divisible by 1,000 rather than truncating them.

World/valid times include a scheduled time and an effective time. Source publication, observation and ingestion are distinct arrival stages. `EventRevision.available_from_ns` is knowledge time. The model requires publication/observation/ingestion/last update (when present) no later than revision availability. The store additionally requires all raw evidence to have been ingested by availability. A future schedule is valid if its existence is already known. It is not future information by itself.

For `known_as_of(T)`, the store chooses the latest revision of each semantic event whose availability is `<= T`; absent events are omitted. Thus a 14:03 release is absent at 14:02; a 14:10 correction does not change the 14:09 view; tomorrow's known reschedule does not rewrite yesterday's schedule. Queries are ordered by semantic event ID, independent of insertion order. Optional direct scope and event-time interval filtering is applied after selecting the historically visible revision. `ReplayClock` only advances from explicit caller input and never reads wall time.

In-memory persistence retains history and is deterministic but not durable. EIE-0 does not implement the full bitemporal analytics, source-outage replay, or final `EventIntelligenceSnapshot`. Those later systems must preserve these availability rules.

EIE-1 schedule queries resolve the latest available schedule revision **before** filtering by scheduled event time. A later reschedule can move an event into or out of a range without changing an earlier replay view. Cancellation similarly affects only queries at or after its knowledge time. `events_between(start, end, known_as_of)` uses `[start, end)` on event time and `<= known_as_of` on knowledge time. All windows use the caller's explicit as-of clock and the resolved historical schedule. Source coverage assertions have independent availability times, so a future health report cannot certify an earlier empty result. See [scheduled events](scheduled-events.md).

EIE-2 official ingestion receives explicit source observation and ingestion times. An explicitly published release time remains separate; absence of a published time is flagged. Canonical revision availability is ingestion time, so a release observed after publication does not retroactively appear in EIE replay. Official schedule changes and release corrections append revisions and raw evidence while preserving earlier bytes. Date-only schedules are known facts in the canonical store but have no exact-time window or upcoming placement. See [official sources](official-sources.md).

EIE-3 treats SEC acceptance as a source-time proxy, filing date and report period as separate calendar facts, source observation as caller-supplied, and local ingestion as EIE knowledge time. Filing and canonical-event queries exclude a row before ingestion even if SEC accepted it earlier. Amendments and source metadata corrections append separate causal revisions. Symbol resolution has both `as_of_ns` (what mappings were known) and `valid_at_ns` (when a listing applied). An observed ticker-map snapshot cannot be projected backward before its observation; verified listing intervals can be queried against their separate valid and knowledge clocks. See [SEC EDGAR](sec-edgar.md) and [entity identity](entity-identity.md).

EIE-4 applies the same rule to corporate facts. Claim `available_from_ns` and event revision availability are knowledge time, while earnings schedules and action effective dates are world time. `known_as_of(T)` selects the latest corporate revision available by T. `upcoming_earnings`, announcement/effective interval queries, and symbol queries then filter that historical view. Civil date-only facts remain dates; BMO/AMC never acquire a made-up instant. A future company reschedule, dividend correction, deal status, or ticker change cannot alter prior result bytes. Symbol resolution also requires an explicit valid time, so a known future rename does not make the new ticker valid early. See [corporate events](corporate-events.md).

EIE-5 appends estimate, actual and guidance vintages with explicit availability.
Current consensus resolves the latest observation available by the query cutoff.
Release surprise defaults to consensus strictly before the earliest known original
actual publication/availability anchor; later estimate or actual corrections cannot
rewrite earlier views. Normalization reconstructs compatible history strictly
before that release cutoff, excluding the current event and all same-time releases.
Complete scale evidence and fingerprints are retained. See [surprise intelligence](surprise-intelligence.md).

## EIE-6 durable acquisition

SQLite and in-memory replay share insertion checks and historical resolution.
Acquisition completion is observation time; worker preparation is ingestion
time. A late source item appends knowledge now, preserving its earlier source
publication/effective time. Atomic checkpoints cannot skip uncommitted data.
Restart and overlapping polls preserve old record clocks and query fingerprints.
Operational generations, health, rate windows and latencies are separate.
See [persistence](persistence.md) and [live ingestion](live-ingestion.md).

## Historical dataset replay

EIE-7 separates source historical availability from actual EIE acquisition. A
`DatasetReader` is explicitly bound to a frozen build and filters on audited exact
source cutoffs. Canonical projections omit local observed/ingested/last-updated
clocks. Supporting historical facts retain original acquisition provenance;
archive replay records a separate replay clock. Coarse/current/unknown histories
are excluded from exact queries, and revision conflicts fail latest selection.
Later discovery/reprocessing does not change an older build. These source projections
are subject to [reconstruction assumptions](data-completeness.md), including SEC's
unproven complete metadata-vintage history. They do not replace live knowledge-time
semantics or establish causal proof.

## EIE-8 descriptive response replay

Study selection pins one historical build and explicit event revision cutoff. Pre bars
are completed/available at the anchor; event-spanning bars are excluded. Context/surprise
conditioning cannot use future anchor information. Prior distributions require event,
anchor and completed-study availability strictly before T; the current event is explicitly
excludable even after completion. Later corrected events cannot replace an original
persisted study. Retained market inputs and source fingerprints support byte-identical
recalculation. Reconstructed study availability is an evidence/replay cutoff, not a
backdated claim of actual EIE computation. See [alignment](market-data-alignment.md).

## EIE-9 counterfactual replay

A model pins its original raw study, event fact/revision, frozen build, market/factor/
control/feature inputs, estimation/event windows and versioned policies. Fit periods
and their availability strictly precede both event and fit cutoff. Control snapshots/
scaling populations are prior-only. Event factors use the study data cutoff; future
control features and unavailable observations are projected out. Later event corrections
do not replace a historical model. As-of impact queries require anchor and complete
result availability strictly before T. Replay availability is an evidence cutoff, not
an assertion that EIE actually computed a reconstructed result in the past. Model-based
residuals are descriptive and do not prove causation. See [counterfactual impact](counterfactual-impact.md).

## EIE-10 graph replay

Graph queries take explicit knowledge and valid timestamps. Retroactive effective dates
cannot backdate ingestion knowledge. Corrections/terminations retain prior claims and
cutoff-aware derived knowledge intervals. ETF/index snapshot points do not become
continuous truth without an explicit flagged carry policy; omitted partial members
do not imply removal.

Snapshot identities pin exact edge revisions, source/traversal policies, both clocks,
bounded paths and processing/schema versions. Sealed persisted cutoffs reject subsequent
backdated graph writes, including empty source checkpoints. Graph-derived candidates
retain the original direct event scope and cannot use future relationships. See
[graph replay](graph-replay.md) and [propagation](event-propagation.md).

## EIE-11 evidence and event resolution history

Claims separate knowledge time from valid intervals; event descriptors retain knowledge
revisions and supplied event timing. Correction/retraction history is append-only. Later
merges or splits never rewrite old inputs.
Completed item clocks cannot backdate; independent providers may commit at one unsealed
clock. Freeze a complete cutoff atomically before relying on an unfinished ingest wave's
view as immutable. Historical acquisitions pin their EIE-7 build/fact/acquisition and retain actual local
clocks separately from qualified source availability. See [evidence replay](evidence-replay.md).


## EIE-12 text extraction

Extraction knowledge cannot precede source acquisition or actual processing
availability. The additive `ExtractionAcquisition` preserves original observed,
ingested and historical source-availability clocks and adds processing availability.
Historical text pins the EIE-7 build/fact/acquisition. Later reprocessing creates new
immutable runs and updates; earlier cutoff audits do not change. Separately selected
historical re-extractions should use explicit new build/dataset lineage.

See [structured extraction](structured-extraction.md) and [text ingestion](text-ingestion.md).

## EIE-13 prior-only intelligence

Independent novelty pools exclude the current event, pinned EIE-11 aliases, same-time
and future inputs. Older proposition vintages remain available for corrections and
guidance changes. Exact references, policy and prior/current graph cutoffs pin every
result. Historical source cutoff is separate from local result availability; queries
enforce both. Later impact audits never change ex-ante materiality. Calculation and
append share one SQLite snapshot and seal the evidence cutoff. See
[intelligence replay](intelligence-replay.md).

## EIE-14 frozen analogue pools

Candidates and complete response data require availability strictly before the target cutoff. Source reconstruction retains its actual build/processing availability. The target response is excluded from retrieval input; later comparison pins the frozen result. Future context/revisions cannot replace old rows. SQLite collection and freeze are transactional, and backdated index insertions into sealed cutoffs fail. See [historical analogues](historical-analogues.md).


## EIE-15 interaction consumer

EIE-15 contexts pin visible event revisions, exact EIE-11 alias groups, explicit window policy and EIE-10 root paths. Future source, alias, schedule or relationship updates cannot change frozen contexts. Same-time/coarse events remain tied/unordered. Occurrence and information chronology remain separate. Indexed backdating into sealed cutoffs is refused; deep validation re-collects exact pinned inputs.
