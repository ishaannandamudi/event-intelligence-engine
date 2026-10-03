"""Timestamp layers are semantic ties; stable IDs order only their serialization."""

from eie.interactions.models import EventSequence, InteractionEvent, SequenceStep
from eie.interactions.schema import InteractionQuality, OrderingBasis
from eie.interactions.windows import time_value


def sequence(
    rows: tuple[InteractionEvent, ...],
    dataset: str,
    entity: str,
    start: int,
    end: int,
    known: int,
    basis: OrderingBasis,
    resolution: str | None = None,
) -> EventSequence:
    groups: dict[int, list[InteractionEvent]] = {}
    unknown = []
    for row in rows:
        t = time_value(row, basis).timestamp_ns
        if t is None:
            unknown.append(row.artifact_id)
        else:
            groups.setdefault(t, []).append(row)
    layers = tuple(tuple(sorted(r.artifact_id for r in groups[t])) for t in sorted(groups))
    steps = []
    # Only adjacent singleton layers provide a precise pair step. Other exact layers
    # still retain their timestamp order, without a fabricated order within a tie.
    for a, b in zip(sorted(groups), sorted(groups)[1:], strict=False):
        if len(groups[a]) == len(groups[b]) == 1:
            left, right = groups[a][0], groups[b][0]
            x, y = left.occurrence.timestamp_ns, right.occurrence.timestamp_ns
            steps.append(
                SequenceStep(
                    left.artifact_id,
                    right.artifact_id,
                    b - a,
                    None if x is None or y is None else y - x,
                    right.known_ns - left.known_ns,
                )
            )
    quality = set().union(*(r.quality for r in rows))
    if unknown:
        quality.add(InteractionQuality.PARTIAL_ORDER)
    if any(len(layer) > 1 for layer in layers):
        quality.add(InteractionQuality.TIED)
    return EventSequence(
        dataset,
        entity,
        start,
        end,
        known,
        basis,
        tuple(sorted(r.artifact_id for r in rows)),
        layers,
        tuple(sorted(unknown)),
        tuple(steps),
        frozenset(quality),
        resolution,
    )
