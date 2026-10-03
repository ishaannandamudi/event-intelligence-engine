"""Subsequent related events along pinned root paths, without recursive inference."""

from eie.core.errors import ValidationError
from eie.interactions.models import (
    InteractionEvent,
    InteractionPolicy,
    PathReference,
    PropagationChain,
    PropagationChronology,
)
from eie.interactions.schema import InteractionQuality as Q
from eie.interactions.windows import horizon_end, time_value


def chronology(
    root: InteractionEvent,
    rows: tuple[InteractionEvent, ...],
    paths: tuple[PathReference, ...],
    policy: InteractionPolicy,
    known: int,
) -> tuple[tuple[PropagationChronology, ...], tuple[PropagationChain, ...]]:
    t = time_value(root, policy.chronology_basis).timestamp_ns
    end = horizon_end(root, policy)
    if t is None or end is None:
        return (), ()
    entries = []
    direct = set(root.scope.identifiers)
    for row in rows:
        u = time_value(row, policy.chronology_basis).timestamp_ns
        if row.event_id == root.event_id or u is None or not t < u <= end:
            continue
        if direct & set(row.scope.identifiers):
            continue
        for path in paths:
            if path.node_ids[-1] not in row.scope.identifiers:
                continue
            quality = root.quality | row.quality
            if path.qualified:
                quality |= {Q.GRAPH_QUALIFIED}
            entries.append(
                PropagationChronology(
                    root.artifact_id,
                    row.artifact_id,
                    path.artifact_id,
                    policy.chronology_basis,
                    t,
                    u,
                    root.known_ns,
                    row.known_ns,
                    u - t,
                    frozenset(quality),
                )
            )
    if len(entries) > policy.max_chronology_entries:
        raise ValidationError("propagation chronology entry bound exceeded")
    entries.sort(key=lambda e: e.artifact_id)
    groups: dict[int, list[str]] = {}
    for entry in entries:
        groups.setdefault(entry.later_time_ns, []).append(entry.artifact_id)
    layers = tuple(tuple(sorted(groups[u])) for u in sorted(groups))
    chain = PropagationChain(
        known,
        root.artifact_id,
        policy.chronology_basis,
        end,
        tuple(e.artifact_id for e in entries),
        layers,
        frozenset({Q.TIED}) if any(len(layer) > 1 for layer in layers) else frozenset(),
    )
    return tuple(entries), (chain,)
