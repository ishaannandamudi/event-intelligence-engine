"""Pure deterministic planning; no database, archive or network activity."""

from eie.backfill.models import Partition, TimeRange
from eie.core.errors import ValidationError
from eie.datasets.models import BackfillJob


def plan(job: BackfillJob) -> tuple[Partition, ...]:
    result: list[Partition] = []
    definition = job.definition
    for source in definition.sources:
        span = min(definition.partition_policy.span_ns, source.capability.max_range_ns)
        points = sorted(
            {
                definition.interval.start_ns,
                definition.interval.end_ns,
                *[
                    t
                    for t in (source.capability.earliest_ns, source.capability.latest_ns)
                    if t is not None
                    and definition.interval.start_ns < t < definition.interval.end_ns
                ],
            }
        )
        count = sum((b - a + span - 1) // span for a, b in zip(points, points[1:], strict=False))
        if len(result) + count * len(source.resources) > definition.partition_policy.max_partitions:
            raise ValidationError("historical plan exceeds partition bound")
        for resource in source.resources:
            for a, b in zip(points, points[1:], strict=False):
                for start in range(a, b, span):
                    result.append(
                        Partition(
                            job.job_id,
                            source.provider_id,
                            resource,
                            TimeRange(start, min(b, start + span)),
                        )
                    )
    return tuple(result)


def split(partition: Partition, maximum_depth: int) -> tuple[Partition, Partition]:
    interval = partition.interval
    if partition.depth >= maximum_depth or interval.end_ns - interval.start_ns < 2:
        raise ValidationError("historical split bound exhausted")
    middle = interval.start_ns + (interval.end_ns - interval.start_ns) // 2

    def child(bounds: TimeRange) -> Partition:
        return Partition(
            partition.job_id,
            partition.provider_id,
            partition.resource_id,
            bounds,
            partition.partition_id,
            partition.depth + 1,
        )

    return child(TimeRange(interval.start_ns, middle)), child(TimeRange(middle, interval.end_ns))
