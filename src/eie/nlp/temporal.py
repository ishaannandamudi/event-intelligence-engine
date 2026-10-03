"""Only explicit precision and qualified source context establish extracted time."""

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from eie.core.errors import ValidationError
from eie.corporate.types import EarningsPeriod, ReleaseTiming
from eie.evidence.claims import TimePrecision, TimeValue
from eie.nlp.documents import TextDocument
from eie.nlp.mentions import PeriodMention, TemporalMention
from eie.nlp.normalization import NormalizedText
from eie.nlp.segmentation import TextSegment
from eie.nlp.spans import EvidenceSpan, make_span
from eie.providers.common.models import parse_aware_ns

_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})"
_DATE = r"\d{4}-\d{2}-\d{2}"
_MONTH = {
    name.lower(): i
    for i, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        1,
    )
}
_TEMPORAL = re.compile(
    r"(?P<stamp>"
    + _STAMP
    + r")|(?P<date>"
    + _DATE
    + r")|(?P<month>"
    + "|".join(_MONTH)
    + r")\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?|"
    r"\b(?P<relative>today|tomorrow|next quarter)\b",
    re.I,
)


def event_time_supported(
    normalized: NormalizedText, span: EvidenceSpan, segments: tuple[TextSegment, ...]
) -> bool:
    seg = next(s for s in segments if s.segment_id == span.segment_id)
    prefix = normalized.text[max(seg.start, span.start - 50) : span.start]
    return (
        re.search(
            r"\b(?:effective|occurred(?: on)?|event date|incident date|resigned on|"
            r"appointed on|expected close(?: date| on)?)\s*$",
            prefix,
            re.I,
        )
        is not None
    )


def _publication(document: TextDocument) -> datetime | None:
    if document.raw.published_at_ns is None or document.timezone is None:
        return None
    instant = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        seconds=document.raw.published_at_ns // 1_000_000_000
    )
    return instant.astimezone(ZoneInfo(document.timezone))


def extract_temporal(
    original: str,
    normalized: NormalizedText,
    segments: tuple[TextSegment, ...],
    document: TextDocument,
) -> tuple[TemporalMention, ...]:
    result: list[TemporalMention] = []
    publication = _publication(document)
    for seg in segments:
        text = normalized.text[seg.start : seg.end]
        for m in _TEMPORAL.finditer(text):
            span_end = m.end()
            value, reason = TimeValue(TimePrecision.UNKNOWN), "UNRESOLVED_REFERENCE_TIME"
            try:
                if m.group("stamp"):
                    timestamp = parse_aware_ns(m.group("stamp").upper(), "source timestamp")
                    utc_date = (
                        (
                            datetime(1970, 1, 1, tzinfo=UTC)
                            + timedelta(seconds=timestamp // 1_000_000_000)
                        )
                        .date()
                        .isoformat()
                    )
                    value, reason = (
                        TimeValue(TimePrecision.EXACT, utc_date, timestamp),
                        "EXPLICIT_OFFSET_TIMESTAMP",
                    )
                else:
                    day: date | None = None
                    if m.group("date"):
                        day = date.fromisoformat(m.group("date"))
                    elif m.group("month"):
                        year = (
                            int(m.group("year"))
                            if m.group("year")
                            else publication.year
                            if publication
                            else None
                        )
                        # A yearless month/day is ambiguous even when publication has a year.
                        if year is not None and m.group("year"):
                            day = date(year, _MONTH[m.group("month").lower()], int(m.group("day")))
                    elif publication and m.group("relative").lower() in ("today", "tomorrow"):
                        day = publication.date() + timedelta(
                            days=m.group("relative").lower() == "tomorrow"
                        )
                    if day is not None:
                        nearby = text[m.end() : m.end() + 40].lower()
                        session = (
                            ReleaseTiming.AFTER_MARKET
                            if re.search(r"\bafter (?:market|the close)\b", nearby)
                            else ReleaseTiming.BEFORE_MARKET
                            if re.search(r"\bbefore (?:market|the open)\b", nearby)
                            else ReleaseTiming.UNKNOWN
                        )
                        if session is not ReleaseTiming.UNKNOWN:
                            session_match = re.search(
                                r"\b(?:after (?:market|the close)|before (?:market|the open))\b",
                                nearby,
                            )
                            assert session_match is not None
                            span_end += session_match.end()
                        value = TimeValue(
                            TimePrecision.DATE_ONLY
                            if session is ReleaseTiming.UNKNOWN
                            else TimePrecision.DATE_PLUS_SESSION,
                            day.isoformat(),
                            session=session,
                        )
                        reason = "SOURCE_DATE_OR_QUALIFIED_RELATIVE_DATE"
            except (ValueError, ValidationError, OverflowError):
                reason = "INVALID_SOURCE_DATE"
            result.append(
                TemporalMention(
                    make_span(
                        original, normalized, seg, seg.start + m.start(), seg.start + span_end
                    ),
                    value,
                    reason,
                )
            )
    if len(result) > normalized.mapping.settings.limits.mentions:
        raise ValidationError("temporal mention bound exceeded")
    return tuple(sorted(result, key=lambda r: r.mention_id))


def extract_periods(
    original: str, normalized: NormalizedText, segments: tuple[TextSegment, ...]
) -> tuple[PeriodMention, ...]:
    result = []
    for seg in segments:
        text = normalized.text[seg.start : seg.end]
        for m in re.finditer(
            r"\b(?:Q([1-4])\s+(?:FY\s*)?(\d{4})|FY\s*(\d{4}))"
            r"\s+(?:ended|ending|period end)\s+(\d{4}-\d{2}-\d{2})",
            text,
            re.I,
        ):
            quarter, year, annual, end = m.groups()
            try:
                period = EarningsPeriod(int(year or annual), int(quarter) if quarter else None, end)
            except ValidationError:
                continue
            result.append(
                PeriodMention(
                    make_span(
                        original, normalized, seg, seg.start + m.start(), seg.start + m.end()
                    ),
                    period,
                )
            )
    if len(result) > normalized.mapping.settings.limits.mentions:
        raise ValidationError("period mention bound exceeded")
    return tuple(sorted(result, key=lambda r: r.mention_id))
