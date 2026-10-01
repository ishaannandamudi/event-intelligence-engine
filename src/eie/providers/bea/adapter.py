"""Pure parser for official BEA release metadata extracts."""

import json
from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.events.taxonomy import EventFamily, EventType
from eie.providers.common.models import (
    ItemKind,
    OfficialItem,
    OfficialSnapshot,
    component_codes,
    fragment_hash,
    load_extract,
    optional_time,
    parse_aware_ns,
    parse_kind,
    period_from_item,
    required_string,
    schedule_fields,
)
from eie.providers.common.protocols import ProviderCapability

BEA_TYPES = {
    "GDP": EventType(EventFamily.MACRO, "MACRO.GDP.US"),
    "PERSONAL_INCOME_AND_OUTLAYS": EventType(EventFamily.MACRO, "MACRO.PCE.US"),
    "TRADE": EventType(EventFamily.MACRO, "MACRO.TRADE.US"),
}
BEA_COMPONENTS = frozenset({"CORE_PCE", "PERSONAL_INCOME", "PERSONAL_SPENDING"})
GDP_STAGES = frozenset({"ADVANCE", "SECOND", "THIRD", "UPDATED"})
MACHINE_CALENDAR_URL = "https://apps.bea.gov/API/signup/release_dates.json"


@dataclass(frozen=True, slots=True)
class BEACalendarSlot:
    """Time-only candidate from BEA JSON; it has no reference period or stage."""

    release_name: str
    scheduled_for_ns: int
    source_reference: str = MACHINE_CALENDAR_URL

    def __post_init__(self) -> None:
        if self.release_name not in BEA_TYPES or self.source_reference != MACHINE_CALENDAR_URL:
            raise ValidationError("invalid BEA machine-calendar slot identity")
        require_ns(self.scheduled_for_ns, "scheduled_for_ns")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:bea-calendar-slot:v1", self)


class BEAAdapter:
    provider_id = "BEA"
    adapter_version = "bea-extract-1"

    def capabilities(self) -> frozenset[ProviderCapability]:
        return frozenset(
            {
                ProviderCapability.SCHEDULE_EXTRACT,
                ProviderCapability.RELEASE_EXTRACT,
                ProviderCapability.HISTORICAL_FIXTURE,
            }
        )

    def parse(self, payload: bytes) -> OfficialSnapshot:
        version, entries = load_extract(payload, self.provider_id)
        items: list[OfficialItem] = []
        for entry in entries:
            name = required_string(entry, "release")
            try:
                event_type = BEA_TYPES[name]
            except KeyError as exc:
                raise ValidationError("unsupported BEA release") from exc
            period = period_from_item(entry)
            if period is None:
                raise ValidationError("BEA release requires reference period")
            stage = entry.get("stage")
            if name == "GDP" and stage not in GDP_STAGES:
                raise ValidationError("GDP release requires supported stage")
            if stage is not None and not isinstance(stage, str):
                raise ValidationError("invalid BEA release stage")
            precision, stamp, day = schedule_fields(entry)
            kind = parse_kind(entry)
            if kind in (ItemKind.SCHEDULE, ItemKind.RESCHEDULE, ItemKind.SCHEDULE_CORRECTION) and (
                precision.value == "UNKNOWN"
            ):
                raise ValidationError("BEA schedule requires official date or time")
            items.append(
                OfficialItem(
                    self.provider_id,
                    required_string(entry, "release_id"),
                    required_string(entry, "item_version"),
                    version,
                    required_string(entry, "source_reference"),
                    event_type,
                    f"bea:{name}:{period.start_date}:{period.end_date}:{stage or 'STANDARD'}",
                    required_string(entry, "title"),
                    kind,
                    precision,
                    stamp,
                    day,
                    optional_time(entry, "published_at"),
                    period,
                    stage,
                    fragment_hash(entry),
                    components=component_codes(entry, BEA_COMPONENTS),
                )
            )
        return OfficialSnapshot(
            self.provider_id,
            version,
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

    def parse_machine_calendar(self, payload: bytes) -> tuple[BEACalendarSlot, ...]:
        """Parse BEA's native release_dates.json without inventing period or GDP stage."""
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("invalid BEA machine calendar JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("BEA machine calendar must be an object")
        names = {
            "Gross Domestic Product": "GDP",
            "Personal Income and Outlays": "PERSONAL_INCOME_AND_OUTLAYS",
            "U.S. International Trade in Goods and Services": "TRADE",
        }
        slots: set[tuple[str, int]] = set()
        for official_name, normalized_name in names.items():
            entry = value.get(official_name)
            if entry is None:
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("release_dates"), list):
                raise ValidationError("BEA release_dates layout changed")
            for stamp in entry["release_dates"]:
                slots.add((normalized_name, parse_aware_ns(stamp, "release_dates")))
        return tuple(BEACalendarSlot(name, stamp) for name, stamp in sorted(slots))

    def verify_extract_against_calendar(
        self, snapshot: OfficialSnapshot, slots: tuple[BEACalendarSlot, ...]
    ) -> None:
        if snapshot.provider_id != self.provider_id:
            raise ValidationError("BEA calendar can only verify BEA extracts")
        available = {(slot.release_name, slot.scheduled_for_ns) for slot in slots}
        for item in snapshot.items:
            parts = item.semantic_key.split(":", 2)
            if len(parts) < 3 or parts[0] != "bea":
                raise ValidationError("invalid BEA extract semantic key")
            name = parts[1]
            if item.scheduled_for_ns is not None and (name, item.scheduled_for_ns) not in available:
                raise ValidationError("BEA extract time absent from machine calendar")
