"""Signed exact deltas or coarse intervals; no clamping, trimming or substitution."""

from eie.core.errors import ValidationError
from eie.evidence.claims import TimePrecision
from eie.reliability.clocks import bounds
from eie.reliability.models import InformationLatency, LatencyComponent, SourceHistoryRow
from eie.reliability.schema import ClockKind as C
from eie.reliability.schema import LatencyQuality as Q

PAIRS = (
    (C.VALID_TIME, C.PUBLICATION_TIME),
    (C.PUBLICATION_TIME, C.SOURCE_AVAILABLE_TIME),
    (C.SOURCE_AVAILABLE_TIME, C.OBSERVED_TIME),
    (C.PUBLICATION_TIME, C.OBSERVED_TIME),
    (C.OBSERVED_TIME, C.INGESTED_TIME),
    (C.INGESTED_TIME, C.NORMALIZED_TIME),
    (C.NORMALIZED_TIME, C.SNAPSHOT_AVAILABLE_TIME),
    (C.PUBLICATION_TIME, C.SNAPSHOT_AVAILABLE_TIME),
    (C.SOURCE_AVAILABLE_TIME, C.SNAPSHOT_AVAILABLE_TIME),
)


def calculate(row: SourceHistoryRow, cutoff: int) -> InformationLatency:
    if row.metric_available_ns > cutoff:
        raise ValidationError("latency instrumentation is unavailable at cutoff")
    components = []
    quality: set[Q] = set()
    for start, end in PAIRS:
        av, bv = row.clocks.get(start), row.clocks.get(end)
        a, b = bounds(av, row.clocks.coarse_timezone), bounds(bv, row.clocks.coarse_timezone)
        flags: set[Q] = set()
        delta = lo = hi = None
        if a is None or b is None:
            if any(v is None or v.precision is TimePrecision.UNKNOWN for v in (av, bv)):
                flags.add(Q.MISSING_CLOCK)
            if any(
                v is not None
                and v.precision in (TimePrecision.DATE_ONLY, TimePrecision.DATE_PLUS_SESSION)
                for v in (av, bv)
            ):
                flags.add(Q.COARSE_TIMESTAMP)
                if row.clocks.coarse_timezone is None:
                    flags.add(Q.MISSING_TIMEZONE)
        else:
            lo, hi = b[0] - a[1], b[1] - a[0]
            if a[0] == a[1] and b[0] == b[1]:
                delta = lo
            else:
                flags.add(Q.COARSE_TIMESTAMP)
            # Valid time may legitimately follow announcement/publication.
            if start is not C.VALID_TIME and hi < 0:
                flags.add(Q.NEGATIVE_LATENCY)
                if start in (C.PUBLICATION_TIME, C.SOURCE_AVAILABLE_TIME):
                    flags.add(Q.CLOCK_SKEW)
        components.append(LatencyComponent(start, end, delta, lo, hi, frozenset(flags)))
        quality.update(flags)
    if row.reconstructed:
        quality.add(Q.RECONSTRUCTED_AVAILABILITY)
    return InformationLatency(
        row.artifact_id,
        cutoff,
        row.clocks,
        tuple(components),
        tuple(
            c
            for c in sorted(C, key=lambda c: c.value)
            if (clock := row.clocks.get(c)) is None or clock.precision is TimePrecision.UNKNOWN
        ),
        frozenset(quality),
    )
