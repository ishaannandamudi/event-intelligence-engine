"""Build-bound study engine and deterministic calculation from retained aligned inputs."""

from dataclasses import replace

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.datasets.models import HistoricalFact, semantic_record
from eie.datasets.queries import DatasetReader
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent
from eie.expectations.surprise import SurpriseObservation
from eie.filings.models import FilingMetadata
from eie.studies.alignment import align, lag, point_bar
from eie.studies.contamination import ContaminationIndex
from eie.studies.liquidity import quote_response, volume_response
from eie.studies.market import Adjustment, MarketSeriesProvider, MarketSlice, SplitBoundary
from eie.studies.models import (
    AnchorQuality,
    AnchorSemantics,
    BenchmarkInput,
    BenchmarkResponse,
    BenchmarkType,
    ContaminationFlag,
    ContextLabel,
    EventAnchor,
    EventStudyResult,
    EventWindow,
    ResponsePoint,
    StudyDefinition,
    StudyPolicy,
    StudyQuality,
    StudyRunManifest,
)
from eie.studies.options import change, options_response
from eie.studies.returns import raw_return
from eie.studies.volatility import volatility
from eie.studies.windows import event_anchor, offset_time, resolve_anchor


def visible(data: MarketSlice, cutoff: int) -> MarketSlice:
    return replace(
        data,
        bars=tuple(b for b in data.bars if b.available_ns <= cutoff),
        quotes=tuple(q for q in data.quotes if q.available_ns <= cutoff),
        options=tuple(o for o in data.options if o.available_ns <= cutoff),
        splits=tuple(s for s in data.splits if s.available_ns <= cutoff),
    )


def calculate(
    definition: StudyDefinition,
    market: MarketSlice,
    benchmarks: tuple[BenchmarkInput, ...],
    contamination: tuple[ContaminationFlag, ...],
) -> EventStudyResult:
    if definition.event.lifecycle is LifecycleState.CANCELLED:
        raise ValidationError("cancelled event cannot receive an observed reaction study")
    if (
        definition.anchor.quality is AnchorQuality.EXACT_TIMESTAMP
        and definition.anchor.semantics
        in (
            AnchorSemantics.PUBLICATION,
            AnchorSemantics.SCHEDULED,
            AnchorSemantics.SOURCE_AVAILABILITY,
        )
    ):
        if definition.anchor != event_anchor(definition.event, definition.anchor.semantics):
            raise ValidationError("anchor differs from selected event timestamp")
    if any(
        len(data.bars)
        + len(data.quotes)
        + len(data.options)
        + len(data.sessions)
        + len(data.gaps)
        + len(data.splits)
        > definition.policy.maximum_input_observations
        for data in (market, *(b.data for b in benchmarks if b.data is not None))
    ):
        raise ValidationError("study input observation limit exceeded")
    anchor = resolve_anchor(definition.anchor, definition.window, market.sessions)
    w, p = definition.window, definition.policy
    sessions = tuple(s for s in market.sessions if s.available_ns <= anchor)
    start, end = (
        offset_time(anchor, -w.pre_ns, w.session_policy, sessions),
        offset_time(anchor, w.post_ns, w.session_policy, sessions),
    )
    if end > definition.data_as_of_ns:
        raise ValidationError("study post window has not completed at data cutoff")
    if any(
        c.available_ns > definition.selection_as_of_ns
        or c.event_id == definition.event.event_id
        or not max(0, anchor - p.contamination_pre_ns)
        <= c.timestamp_ns
        <= anchor + p.contamination_post_ns
        for c in contamination
    ):
        raise ValidationError("incompatible/future contamination metadata")
    if any(c.timestamp_ns > anchor or c.available_ns > anchor for c in definition.context):
        raise ValidationError("future context cannot condition historical anchor")
    if definition.surprise is not None and definition.surprise.as_of_ns > anchor:
        raise ValidationError("future surprise cannot condition historical anchor")
    if visible(market, definition.data_as_of_ns) != market:
        raise ValidationError("future market observation in retained study inputs")
    a = align(market, start, anchor, end, p, w.session_policy)
    quality = set(a.quality)
    if definition.anchor.quality is not AnchorQuality.EXACT_TIMESTAMP:
        quality.add(StudyQuality.COARSE_EVENT_TIME)
    if definition.event_gap_fingerprints:
        quality.add(StudyQuality.EVENT_HISTORY_INCOMPLETE)
    risky = market.identity.adjustment is Adjustment.UNADJUSTED and any(
        start <= s.effective_ns <= end for s in market.splits
    )
    if market.identity.adjustment is Adjustment.UNADJUSTED:
        quality.add(StudyQuality.CORPORATE_ACTION_RISK)
    curve_bars = tuple(
        b
        for b in market.bars
        if b.end_ns <= anchor
        and b.available_ns <= anchor
        or b.start_ns >= anchor
        and b.end_ns <= end
    )
    curve = []
    for offset in w.offsets_ns:
        target = offset_time(anchor, offset, w.session_policy, sessions)
        b = point_bar(curve_bars, target, anchor, end, p, w.session_policy, sessions)
        value = (
            None
            if b is None
            or risky
            or (offset <= 0 and a.reference_ns is not None and a.reference_ns > anchor)
            else raw_return(a.reference_price, b.close, p.return_method)
        )
        if any(
            g.start_ns < max(target, a.reference_ns if a.reference_ns is not None else target)
            and g.end_ns > min(target, a.reference_ns if a.reference_ns is not None else target)
            for g in market.gaps
        ):
            value = None
        curve.append(
            ResponsePoint(offset, target, None if value is None or b is None else b.end_ns, value)
        )
    pre_return = (
        None
        if risky
        or not a.pre
        or a.pre[0].start_ns > start
        or lag(a.pre[-1].end_ns, anchor, w.session_policy, sessions) >= w.timeframe.duration_ns
        else raw_return(a.pre[0].open, a.pre[-1].close, p.return_method)
    )
    post_return = (
        None
        if risky
        or not a.post
        or lag(a.post[-1].end_ns, end, w.session_policy, sessions) >= w.timeframe.duration_ns
        else raw_return(a.reference_price, a.post[-1].close, p.return_method)
    )
    rv = volatility(a.pre, a.post, sessions, p.minimum_volatility_returns)
    if risky:
        rv = replace(rv, pre=None, post=None, difference=None, ratio=None)
    if rv.pre is None or rv.post is None:
        quality.add(StudyQuality.LOW_OBSERVATION_COUNT)
    if any(point.value is None for point in curve) or pre_return is None or post_return is None:
        quality.add(StudyQuality.NUMERIC_UNAVAILABLE)
    volumes = volume_response(a.pre, a.post, market.bars, sessions, anchor, p)
    if not volumes and (a.pre or a.post):
        quality.add(StudyQuality.NUMERIC_UNAVAILABLE)
    if not volumes or any(v.baseline is None or v.relative is None for v in volumes):
        quality.add(StudyQuality.BASELINE_UNAVAILABLE)
    quotes = quote_response(market.quotes, start, anchor, end, p.max_lag_ns)
    if quotes.post_timestamp_ns is not None and (
        quotes.relative_spread is None or quotes.quoted_depth is None
    ):
        quality.add(StudyQuality.NUMERIC_UNAVAILABLE)
    if quotes.pre_timestamp_ns is None or quotes.post_timestamp_ns is None:
        quality.add(StudyQuality.MISSING_QUOTES)
    options = options_response(market.options, p.options_key, start, anchor, end, p.max_lag_ns)
    if options.pre_timestamp_ns is None or options.post_timestamp_ns is None:
        quality.add(StudyQuality.MISSING_OPTIONS)
    responses = []
    for benchmark in benchmarks:
        if benchmark.data is None:
            responses.append(
                BenchmarkResponse(
                    benchmark.kind,
                    benchmark.instrument_id,
                    (),
                    None,
                    None,
                    frozenset({StudyQuality.MISSING_BENCHMARK}),
                )
            )
            quality.add(StudyQuality.MISSING_BENCHMARK)
            continue
        if (
            benchmark.data.identity.timeframe,
            benchmark.data.identity.return_type,
            benchmark.data.identity.adjustment,
        ) != (
            market.identity.timeframe,
            market.identity.return_type,
            market.identity.adjustment,
        ):
            raise ValidationError("incompatible asset/benchmark return or adjustment semantics")
        other = calculate(definition, benchmark.data, (), ())
        if (other.anchor_ns, other.pre_start_ns, other.post_end_ns) != (anchor, start, end):
            raise ValidationError("benchmark session clock differs from asset window")
        difference = (
            None
            if post_return is None or other.post_return is None
            else change(other.post_return, post_return)
        )
        responses.append(
            BenchmarkResponse(
                benchmark.kind,
                benchmark.instrument_id,
                other.curve,
                other.volatility,
                difference,
                other.quality,
            )
        )
    if contamination:
        quality.add(StudyQuality.CONTAMINATED)
    times = [end, definition.event.revision.available_from_ns, definition.selection_as_of_ns]
    times.extend(c.available_ns for c in contamination)
    for data in (market, *(b.data for b in benchmarks if b.data is not None)):
        times.extend(v.available_ns for v in data.bars)
        times.extend(v.available_ns for v in data.quotes)
        times.extend(v.available_ns for v in data.options)
        times.extend(v.available_ns for v in data.splits)
    return EventStudyResult(
        definition,
        market,
        benchmarks,
        anchor,
        start,
        end,
        None if risky else a.reference_price,
        a.reference_ns,
        tuple(curve),
        pre_return,
        post_return,
        rv,
        volumes,
        quotes,
        options,
        tuple(responses),
        contamination,
        frozenset(quality),
        max(times),
    )


DEFAULT_POLICY = StudyPolicy()


class StudyEngine:
    def __init__(
        self, events: DatasetReader, market: MarketSeriesProvider, selection_as_of_ns: int
    ) -> None:
        require_ns(selection_as_of_ns)
        self.reader, self.market, self.selection_as_of_ns = events, market, selection_as_of_ns
        self.events = events.events_known_at(selection_as_of_ns).events
        self._events = {e.event_id: e for e in self.events}
        self._facts: dict[str, list[HistoricalFact]] = {e.event_id: [] for e in self.events}
        for f in events.records(selection_as_of_ns, kind="CanonicalEvent", latest=True):
            if (
                isinstance(f.record, CanonicalEvent)
                and f.record.event_id in self._events
                and f.record.revision.number == self._events[f.record.event_id].revision.number
                and semantic_record(f.record) == semantic_record(self._events[f.record.event_id])
                and f.raw.source.source_class in self._events[f.record.event_id].source_classes
            ):
                self._facts[f.record.event_id].append(f)
        self.contamination = ContaminationIndex(self.events)
        self._splits = tuple(
            e
            for e in self.events
            if e.identity.event_type.code
            in ("CORPORATE_ACTION.STOCK_SPLIT", "CORPORATE_ACTION.REVERSE_SPLIT")
        )

    def anchor(self, event_id: str, semantics: AnchorSemantics) -> EventAnchor:
        e = self._events[event_id]
        if semantics is AnchorSemantics.ACCEPTANCE:
            values = {
                f.record.accepted_at_ns
                for f in self.reader.records(
                    self.selection_as_of_ns, kind="FilingMetadata", latest=True
                )
                if isinstance(f.record, FilingMetadata)
                and f.record.source_observation_id in e.evidence_observation_ids
            }
            if len(values) != 1:
                raise ValidationError("acceptance requires unique visible filing evidence")
            return event_anchor(e, semantics, accepted_ns=next(iter(values)))
        if semantics is AnchorSemantics.EIE_AVAILABILITY:
            values = {f.raw.ingested_at_ns for f in self._facts[event_id]}
            if len(values) != 1:
                raise ValidationError(
                    "EIE availability requires unique actual acquisition evidence"
                )
            return event_anchor(e, semantics, eie_available_ns=next(iter(values)))
        return event_anchor(e, semantics)

    def study_event(
        self,
        event_id: str,
        instrument_id: str,
        anchor: EventAnchor,
        window: EventWindow,
        *,
        data_as_of_ns: int,
        policy: StudyPolicy = DEFAULT_POLICY,
        benchmarks: tuple[tuple[BenchmarkType, str], ...] = (),
        context: tuple[ContextLabel, ...] = (),
        surprise: SurpriseObservation | None = None,
    ) -> EventStudyResult:
        if event_id not in self._events:
            raise ValidationError("event is not visible in chosen historical build/cutoff")
        e = self._events[event_id]
        if e.lifecycle is LifecycleState.CANCELLED:
            raise ValidationError("cancelled event cannot receive an observed reaction study")
        if anchor.quality is AnchorQuality.EXACT_TIMESTAMP:
            if anchor != self.anchor(event_id, anchor.semantics):
                raise ValidationError("anchor differs from selected event revision/evidence")
        else:
            facts = {f.name: f.value for f in e.facts}
            precision = facts.get("schedule_precision")
            if (
                anchor.semantics is not AnchorSemantics.SCHEDULED
                or precision not in ("DATE_ONLY", "DATE_PLUS_SESSION")
                or anchor.session_label != facts.get("scheduled_date")
            ):
                raise ValidationError(
                    "coarse anchor must match selected structured schedule evidence"
                )
            if (
                precision == "DATE_ONLY"
                and anchor.quality is not AnchorQuality.DATE_ONLY
                or precision == "DATE_PLUS_SESSION"
                and anchor.quality is not AnchorQuality.SESSION_LABEL
            ):
                raise ValidationError("coarse anchor quality differs from source precision")
            timing = facts.get("release_timing")
            if (
                timing == "BEFORE_MARKET"
                and window.coarse_policy.value != "SESSION_OPEN"
                or timing == "AFTER_MARKET"
                and window.coarse_policy.value != "SESSION_CLOSE"
            ):
                raise ValidationError("coarse session policy disagrees with BMO/AMC evidence")
        identity = self.market.identity(instrument_id)
        if identity is None:
            raise ValidationError("market instrument not supplied")
        sessions = self.market.sessions(instrument_id)
        at = resolve_anchor(anchor, window, sessions)
        start = offset_time(at, -window.pre_ns, window.session_policy, sessions)
        end = offset_time(at, window.post_ns, window.session_policy, sessions)
        data = visible(
            self.market.read(
                instrument_id, max(0, min(start, at - policy.volume_baseline_ns)), end
            ),
            data_as_of_ns,
        )
        known_splits = tuple(
            SplitBoundary(
                other.effective_at_ns,
                other.revision.available_from_ns,
                "EIE_HISTORY",
                other.event_id,
            )
            for other in self._splits
            if other.identity.event_type.code
            in ("CORPORATE_ACTION.STOCK_SPLIT", "CORPORATE_ACTION.REVERSE_SPLIT")
            and other.effective_at_ns is not None
            and start <= other.effective_at_ns <= end
            and (
                other.identity.scope == e.identity.scope
                or instrument_id in other.identity.scope.identifiers
            )
        )
        data = replace(
            data,
            splits=tuple(
                sorted(
                    {*data.splits, *known_splits},
                    key=lambda action: (action.effective_ns, action.action_id),
                )
            ),
        )
        if data.identity != identity:
            raise ValidationError("provider changed market identity during retrieval")
        inputs = []
        for kind, symbol in sorted(benchmarks):
            spec = self.market.identity(symbol)
            supplied = (
                None
                if spec is None
                else visible(
                    self.market.read(
                        symbol, max(0, min(start, at - policy.volume_baseline_ns)), end
                    ),
                    data_as_of_ns,
                )
            )
            if supplied is not None and supplied.identity != spec:
                raise ValidationError("provider changed benchmark identity during retrieval")
            inputs.append(BenchmarkInput(kind, symbol, supplied))
        f = self._facts[event_id][0]
        definition = StudyDefinition(
            e,
            self.reader.build_id,
            self.reader.manifest.content_fingerprint,
            tuple(
                sorted(
                    {
                        fingerprint("eie:study-event-gap:v1", g.semantic_content())
                        for g in self.reader.manifest.gaps
                    }
                    | (
                        {
                            fingerprint(
                                "eie:study-unknown-event-time:v1", self.contamination.unknown_count
                            )
                        }
                        if self.contamination.unknown_count
                        else set()
                    )
                )
            ),
            f.quality,
            self.selection_as_of_ns,
            data_as_of_ns,
            anchor,
            window,
            policy,
            f.fact_id,
            context,
            surprise,
        )
        flags = self.contamination.flags(
            e,
            at,
            max(0, at - policy.contamination_pre_ns),
            require_ns(at + policy.contamination_post_ns),
        )
        result = calculate(definition, data, tuple(inputs), flags)
        return result

    def study_events(
        self,
        requests: tuple[tuple[str, str, EventAnchor, EventWindow], ...],
        *,
        data_as_of_ns: int,
        policy: StudyPolicy = DEFAULT_POLICY,
    ) -> tuple[EventStudyResult, ...]:
        results = tuple(
            self.study_event(e, i, a, w, data_as_of_ns=data_as_of_ns, policy=policy)
            for e, i, a, w in sorted(requests, key=lambda r: fingerprint("request", r))
        )
        if len({r.study_id for r in results}) != len(results):
            raise ValidationError("duplicate batch study request")
        return tuple(sorted(results, key=lambda r: r.study_id))


def run_manifest(results: tuple[EventStudyResult, ...]) -> StudyRunManifest:
    if (
        not results
        or len(
            {(r.definition.event_build_id, r.definition.event_dataset_fingerprint) for r in results}
        )
        != 1
    ):
        raise ValidationError("study run requires one compatible historical build")
    ordered = tuple(sorted(results, key=lambda r: r.study_id))
    return StudyRunManifest(
        ordered[0].definition.event_build_id,
        ordered[0].definition.event_dataset_fingerprint,
        tuple(r.study_id for r in ordered),
        tuple(r.result_fingerprint for r in ordered),
        sum(bool(r.contamination) for r in results),
        sum(
            bool(
                r.quality
                & {
                    StudyQuality.DATA_GAP,
                    StudyQuality.MISSING_PRE_DATA,
                    StudyQuality.MISSING_POST_DATA,
                }
            )
            for r in results
        ),
    )
