"""Study clocks, causal input projection, exact close-to-close paired periods."""

from bisect import bisect_left
from dataclasses import replace

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.impact.inputs import ImpactInputs, ReturnSemantics
from eie.impact.models import CounterfactualModelSpec, DatasetPin, EstimationWindow, PeriodReturn
from eie.studies.alignment import active_duration
from eie.studies.market import Adjustment, MarketSession, MarketSlice, Timeframe
from eie.studies.models import EventStudyResult
from eie.studies.returns import raw_return
from eie.studies.windows import offset_time


def estimation_bounds(
    study: EventStudyResult,
    window: EstimationWindow,
    sessions: tuple[MarketSession, ...] | None = None,
) -> tuple[int, int]:
    calendar = study.market.sessions if sessions is None else sessions
    return (
        offset_time(study.anchor_ns, window.start_offset, window.session_policy, calendar),
        offset_time(study.anchor_ns, window.end_offset, window.session_policy, calendar),
    )


def tuple_bounds(study: EventStudyResult, start: int, end: int) -> tuple[int, int]:
    w = study.definition.window
    return (
        offset_time(study.anchor_ns, start, w.session_policy, study.market.sessions),
        offset_time(study.anchor_ns, end, w.session_policy, study.market.sessions),
    )


def calendar_fingerprint(data: MarketSlice, anchor: int, start: int, end: int) -> str:
    return fingerprint(
        "eie:impact-session-basis:v1",
        tuple(
            (s.label, s.open_ns, s.close_ns)
            for s in data.sessions
            if s.available_ns <= anchor and s.close_ns >= start and s.open_ns <= end
        ),
    )


def semantics(inputs: ImpactInputs, window: EstimationWindow) -> ReturnSemantics:
    start, _ = estimation_bounds(inputs.study, window, inputs.target.sessions)
    i = inputs.target.identity
    return ReturnSemantics(
        i.timeframe,
        i.adjustment,
        i.return_type,
        inputs.study.definition.policy.return_method,
        calendar_fingerprint(
            inputs.target, inputs.study.anchor_ns, start, inputs.study.post_end_ns
        ),
    )


def compatible(data: MarketSlice, inputs: ImpactInputs, spec: CounterfactualModelSpec) -> bool:
    start, _ = estimation_bounds(inputs.study, spec.estimation_window, inputs.target.sessions)
    return (
        ReturnSemantics(
            data.identity.timeframe,
            data.identity.adjustment,
            data.identity.return_type,
            spec.semantics.method,
            calendar_fingerprint(data, inputs.study.anchor_ns, start, inputs.study.post_end_ns),
        )
        == spec.semantics
    )


def trim_market(data: MarketSlice, start: int, end: int, cutoff: int, anchor: int) -> MarketSlice:
    bars = tuple(b for b in data.bars if b.end_ns <= end and b.available_ns <= cutoff)
    index = bisect_left(tuple(b.end_ns for b in bars), start)
    return replace(
        data,
        bars=bars[max(0, index - 1) :],
        quotes=tuple(
            q for q in data.quotes if start <= q.timestamp_ns <= end and q.available_ns <= cutoff
        ),
        options=(),
        sessions=tuple(s for s in data.sessions if s.available_ns <= anchor),
        gaps=tuple(g for g in data.gaps if g.start_ns < end and g.end_ns > start),
        splits=tuple(s for s in data.splits if s.effective_ns <= end and s.available_ns <= cutoff),
    )


def project_inputs(inputs: ImpactInputs, window: EstimationWindow) -> ImpactInputs:
    s = inputs.study
    start, _ = estimation_bounds(s, window, inputs.target.sessions)
    end, cutoff, anchor = s.post_end_ns, s.definition.data_as_of_ns, s.anchor_ns
    return replace(
        inputs,
        target_features=inputs.target_features
        if inputs.target_features is not None and inputs.target_features.available_ns < anchor
        else None,
        target=trim_market(inputs.target, start, end, cutoff, anchor),
        benchmark=None
        if inputs.benchmark is None
        else trim_market(inputs.benchmark, start, end, cutoff, anchor),
        factors=tuple(
            replace(
                f,
                observations=tuple(
                    o
                    for o in f.observations
                    if start <= o.start_ns and o.end_ns <= end and o.available_ns <= cutoff
                ),
            )
            for f in inputs.factors
        ),
        controls=tuple(
            replace(
                c,
                features=c.features
                if c.features is not None and c.features.available_ns < anchor
                else None,
                market=None
                if c.market is None
                else trim_market(c.market, start, end, cutoff, anchor),
                contamination=tuple(
                    f
                    for f in c.contamination
                    if f.available_ns <= s.definition.selection_as_of_ns
                    and s.pre_start_ns <= f.timestamp_ns <= end
                ),
            )
            for c in inputs.controls
        ),
        fit_exclusions=tuple(
            x for x in inputs.fit_exclusions if x.available_ns < anchor and x.start_ns < anchor
        ),
    )


def dataset_pins(inputs: ImpactInputs) -> tuple[DatasetPin, ...]:
    result = []
    for role, market in (
        ("TARGET", inputs.target),
        ("BENCHMARK", inputs.benchmark),
        *(("CONTROL", c.market) for c in inputs.controls),
    ):
        if market is not None:
            i = market.identity
            result.append(
                DatasetPin(role, i.dataset_id, i.dataset_fingerprint, i.version, i.instrument_id)
            )
    for f in inputs.factors:
        result.append(
            DatasetPin("FACTOR", f.dataset_id, f.dataset_fingerprint, f.version, f.factor_id)
        )
    for feature in (inputs.target_features, *(c.features for c in inputs.controls)):
        if feature is not None:
            result.append(
                DatasetPin(
                    "FEATURE",
                    feature.dataset_id,
                    feature.dataset_fingerprint,
                    feature.version,
                    feature.instrument_id,
                )
            )
    return tuple(
        sorted(
            set(result), key=lambda p: (p.role, p.member_id, p.dataset_id, p.fingerprint, p.version)
        )
    )


def period_returns(
    data: MarketSlice, method: ReturnSemantics, cutoff: int, *, strict: bool = False
) -> tuple[PeriodReturn, ...]:
    result = []
    sessions = tuple(
        s
        for s in data.sessions
        if s.available_ns <= cutoff and (not strict or s.available_ns < cutoff)
    )
    for a, b in zip(data.bars, data.bars[1:], strict=False):
        availability = max(a.available_ns, b.available_ns)
        if availability > cutoff or (strict and availability == cutoff):
            continue
        contiguous = (
            a.end_ns == b.start_ns
            or bool(sessions)
            and active_duration(a.end_ns, b.start_ns, sessions) == 0
        )
        complete = data.identity.timeframe is Timeframe.DAILY or all(
            bar.end_ns - bar.start_ns == data.identity.timeframe.duration_ns for bar in (a, b)
        )
        value = raw_return(a.close, b.close, method.method) if contiguous and complete else None
        if any(g.start_ns < b.end_ns and g.end_ns > a.end_ns for g in data.gaps) or (
            data.identity.adjustment is Adjustment.UNADJUSTED
            and any(
                a.end_ns < s.effective_ns <= b.end_ns
                and s.available_ns <= cutoff
                and (not strict or s.available_ns < cutoff)
                for s in data.splits
            )
        ):
            value = None
        result.append(
            PeriodReturn(
                a.end_ns,
                b.end_ns,
                value,
                availability,
                fingerprint("eie:impact-period:v1", (data.identity, a, b)),
            )
        )
    return tuple(result)


def fitting_periods(
    inputs: ImpactInputs, data: MarketSlice, spec: CounterfactualModelSpec
) -> tuple[PeriodReturn, ...]:
    start, end = estimation_bounds(inputs.study, spec.estimation_window, inputs.target.sessions)
    cutoff = min(spec.fit_cutoff_ns, inputs.study.anchor_ns)
    return tuple(
        p
        for p in period_returns(data, spec.semantics, cutoff, strict=True)
        if start <= p.start_ns and p.end_ns <= end and p.end_ns < cutoff
    )


def excluded(
    inputs: ImpactInputs,
    spec: CounterfactualModelSpec,
    period: PeriodReturn,
    members: tuple[str, ...],
) -> bool:
    return spec.exclude_fit_contamination and any(
        x.instrument_id in members
        and x.available_ns < spec.fit_cutoff_ns
        and x.start_ns < period.end_ns
        and x.end_ns > period.start_ns
        for x in inputs.fit_exclusions
    )


def verify_inputs(inputs: ImpactInputs, spec: CounterfactualModelSpec) -> None:
    from eie.studies.validation import StudyValidationStatus, validate_study

    s = inputs.study
    if validate_study(s).status is StudyValidationStatus.INVALID:
        raise ValidationError("impact requires a validated raw event study")
    if (
        project_inputs(inputs, spec.estimation_window) != inputs
        or spec.input_fingerprint != inputs.input_fingerprint
        or spec.study_id != s.study_id
        or spec.study_fingerprint != s.result_fingerprint
        or spec.event_build_id != s.definition.event_build_id
        or spec.event_dataset_fingerprint != s.definition.event_dataset_fingerprint
        or spec.event_window != s.definition.window
        or spec.semantics != semantics(inputs, spec.estimation_window)
        or spec.datasets != dataset_pins(inputs)
        or spec.fit_cutoff_ns > s.anchor_ns
    ):
        raise ValidationError("impact inputs/specification are not the pinned causal projection")
    # Event-period prices are the exact retained raw study prices, not a replacement dataset.
    expected = tuple(b for b in s.market.bars if s.pre_start_ns < b.end_ns <= s.post_end_ns)
    actual = tuple(b for b in inputs.target.bars if s.pre_start_ns < b.end_ns <= s.post_end_ns)
    if (
        expected != actual
        or calendar_fingerprint(inputs.target, s.anchor_ns, s.pre_start_ns, s.post_end_ns)
        != calendar_fingerprint(s.market, s.anchor_ns, s.pre_start_ns, s.post_end_ns)
        or any(
            g not in inputs.target.gaps
            for g in s.market.gaps
            if g.start_ns < s.post_end_ns and g.end_ns > s.pre_start_ns
        )
        or any(a not in inputs.target.splits for a in s.market.splits)
    ):
        raise ValidationError("impact event input differs from validated raw study")
    if inputs.benchmark is not None and not compatible(inputs.benchmark, inputs, spec):
        raise ValidationError("incompatible benchmark return/frequency/session semantics")
    if any(f.semantics != spec.semantics for f in inputs.factors):
        raise ValidationError("incompatible factor return/frequency/session semantics")
