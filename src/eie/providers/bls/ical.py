"""Small, strict RFC 5545 subset for BLS's public calendar feed."""

import re
from datetime import UTC, date, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import datetime_to_ns
from eie.providers.bls.adapter import BLS_TYPES
from eie.providers.common.models import (
    ItemKind,
    OfficialItem,
    OfficialSnapshot,
    PeriodFrequency,
    ReferencePeriod,
    TimePrecision,
)

SUMMARY_NAMES = {
    "Consumer Price Index": "CPI",
    "Producer Price Index": "PPI",
    "Employment Situation": "EMPLOYMENT_SITUATION",
    "Job Openings and Labor Turnover Survey": "JOLTS",
    "Employment Cost Index": "ECI",
    "Productivity and Costs": "PRODUCTIVITY",
}
SUMMARY_PERIOD = re.compile(r"\bfor ([A-Z][a-z]+) ([0-9]{4})\b")
MONTHS = {
    name: number
    for number, name in enumerate(
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


def _unfold(payload: bytes) -> list[str]:
    try:
        lines = payload.decode("utf-8-sig").replace("\r\n", "\n").split("\n")
    except UnicodeDecodeError as exc:
        raise ValidationError("BLS calendar must be UTF-8 text") from exc
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _schedule(value: str, parameters: str) -> tuple[TimePrecision, int | None, str | None]:
    if "VALUE=DATE" in parameters:
        try:
            if not re.fullmatch(r"[0-9]{8}", value):
                raise ValueError("bad calendar date")
            day = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        except ValueError as exc:
            raise ValidationError("invalid BLS date-only DTSTART") from exc
        return TimePrecision.DATE_ONLY, None, day.isoformat()
    try:
        if value.endswith("Z"):
            pattern = "%Y%m%dT%H%M%SZ" if len(value) == 16 else "%Y%m%dT%H%MZ"
            stamp = datetime.strptime(value, pattern).replace(tzinfo=UTC)
        elif "TZID=America/New_York" in parameters or "TZID=US/Eastern" in parameters:
            pattern = "%Y%m%dT%H%M%S" if len(value) == 15 else "%Y%m%dT%H%M"
            stamp = datetime.strptime(value, pattern).replace(tzinfo=ZoneInfo("America/New_York"))
            if stamp.astimezone(UTC).astimezone(ZoneInfo("America/New_York")) != stamp:
                raise ValidationError("nonexistent local BLS calendar time")
        else:
            raise ValidationError("BLS DTSTART needs UTC or Eastern timezone")
    except ValueError as exc:
        raise ValidationError("invalid BLS calendar DTSTART") from exc
    return TimePrecision.EXACT_TIMESTAMP, datetime_to_ns(stamp), None


def parse_bls_ical(payload: bytes) -> OfficialSnapshot:
    lines = _unfold(payload)
    if "BEGIN:VCALENDAR" not in lines or "END:VCALENDAR" not in lines:
        raise ValidationError("BLS calendar envelope is missing")
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise ValidationError("nested BLS calendar event")
            current = {}
        elif line == "END:VEVENT":
            if current is None:
                raise ValidationError("BLS calendar event end without start")
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            if key in current and key.split(";", 1)[0] in {"UID", "SUMMARY", "DTSTART"}:
                raise ValidationError("duplicate critical BLS calendar property")
            current[key] = value
    if current is not None:
        raise ValidationError("unterminated BLS calendar event")
    items: list[OfficialItem] = []
    for event in events:
        uid, summary = event.get("UID"), event.get("SUMMARY")
        starts = [(key, value) for key, value in event.items() if key.split(";", 1)[0] == "DTSTART"]
        if not uid or not summary or len(starts) != 1:
            raise ValidationError("BLS calendar event lacks UID, summary, or DTSTART")
        name = next(
            (code for prefix, code in SUMMARY_NAMES.items() if summary.startswith(prefix)), None
        )
        if name is None:
            continue  # supported subset; unrelated official releases remain unclassified
        match = SUMMARY_PERIOD.search(summary)
        if match is None or match.group(1) not in MONTHS:
            raise ValidationError("supported BLS release lacks reference month")
        year, month = int(match.group(2)), MONTHS[match.group(1)]
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        period = ReferencePeriod(
            start.isoformat(), end.isoformat(), PeriodFrequency.MONTHLY, f"{match.group(1)} {year}"
        )
        precision, stamp, day = _schedule(starts[0][1], starts[0][0])
        fragment = sha256(canonical_json_bytes(event)).hexdigest()
        items.append(
            OfficialItem(
                "BLS",
                uid,
                event.get("SEQUENCE", "0"),
                "icalendar-2.0",
                "https://www.bls.gov/schedule/news_release/bls.ics",
                BLS_TYPES[name],
                f"bls:{name}:{period.start_date}:{period.end_date}",
                summary,
                ItemKind.SCHEDULE,
                precision,
                stamp,
                day,
                None,
                period,
                None,
                fragment,
                content_type="text/calendar",
            )
        )
    return OfficialSnapshot(
        "BLS",
        "icalendar-2.0",
        tuple(
            sorted(
                items,
                key=lambda item: (
                    item.semantic_key,
                    item.event_type.code,
                    item.kind.value,
                    item.source_item_id,
                ),
            )
        ),
    )
