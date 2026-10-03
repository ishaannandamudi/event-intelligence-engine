"""Pure deterministic selection over exact frozen pool references."""

from collections import Counter

from eie.analogues.distance import distance, scales
from eie.analogues.models import (
    AnaloguePolicy,
    AnalogueQuery,
    AnalogueResult,
    PoolDiagnostics,
    PoolMember,
)
from eie.analogues.pool import PoolInputs, unique_rows
from eie.analogues.responses import distribution
from eie.analogues.schema import ResultState
from eie.analogues.uncertainty import decompose
from eie.studies.numerics import median


def retrieve(data: PoolInputs, query: AnalogueQuery, policy: AnaloguePolicy) -> AnalogueResult:
    reasons, eligible = unique_rows(data, query, policy)
    responses = {r.feature_row_id: r for r in data.responses}
    prior_scales = scales(data.target, eligible, policy)
    measured = []
    accepted = []
    for row in eligible:
        candidate, reason = distance(data.target, row, responses[row.row_id], policy, prior_scales)
        reasons[row.row_id] = reason
        if candidate is not None:
            measured.append(candidate)
            if reason is None:
                accepted.append(candidate)
    selected = tuple(
        sorted(accepted, key=lambda c: (c.total_distance, c.event_id, c.revision, c.row_id))[
            : query.top_k
        ]
    )
    response_distribution = distribution(
        query.response,
        tuple((c.row_id, c.response_value) for c in selected),
        policy.minimum_history,
    )
    stability = tuple(
        (n, median(c.response_value for c in selected[:n]))
        for n in (5, 10, 20)
        if len(selected) >= n
    )
    context_names = tuple(
        f.selector for f in policy.features if f.source.value in ("CONTEXT", "TIMING")
    )
    contexts = tuple(
        (
            name,
            sum(
                any(c.dimension == name and c.value is not None for c in r.contexts)
                for r in data.rows
            ),
            len(data.rows),
        )
        for name in sorted(set(context_names))
    )
    diagnostics = PoolDiagnostics(
        data.total_prior_events,
        len(data.rows),
        len(eligible),
        sum(
            r.value is not None
            and r.available_ns < query.known_as_of_ns
            and r.completed_ns < query.known_as_of_ns
            for r in data.responses
        ),
        len(selected),
        tuple(sorted(Counter(r for r in reasons.values() if r is not None).items())),
        min((c.total_distance for c in measured), default=None),
        median(c.total_distance for c in selected) if selected else None,
        contexts,
        stability,
    )
    pool = tuple(
        PoolMember(
            r.row_id,
            r.fingerprint,
            None if r.row_id not in responses else responses[r.row_id].response_id,
            None if r.row_id not in responses else responses[r.row_id].fingerprint,
            reasons[r.row_id],
        )
        for r in sorted(data.rows, key=lambda r: r.row_id)
    )
    reconstruction = sum(
        bool(r.reconstruction_reasons)
        or (
            r.row_id in responses
            and bool(
                set(responses[r.row_id].reconstruction_reasons) - {"AUTHORITATIVE_POINT_IN_TIME"}
            )
        )
        for r in data.rows
    )
    contamination = sum(r.contaminated for r in data.responses)
    uncertainty = decompose(
        diagnostics, selected, response_distribution, policy, reconstruction, contamination
    )
    quality = set(response_distribution.reasons)
    if reconstruction:
        quality.add("HISTORICAL_SOURCE_RECONSTRUCTION")
    if contamination:
        quality.add("CONTAMINATION_IN_HISTORY")
    if not selected:
        quality.add("NO_ANALOGUES_WITHIN_EXPLICIT_POLICY")
    state = (
        ResultState.AVAILABLE
        if response_distribution.median is not None
        else ResultState.INSUFFICIENT_ANALOGUES
        if selected
        else ResultState.UNAVAILABLE
    )
    return AnalogueResult(
        query,
        data.target.fingerprint,
        policy.fingerprint,
        data.excluded_event_ids,
        pool,
        prior_scales,
        selected,
        response_distribution,
        uncertainty,
        diagnostics,
        state,
        tuple(sorted(quality)),
    )
