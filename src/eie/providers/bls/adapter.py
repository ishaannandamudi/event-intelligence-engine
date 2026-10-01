"""Pure parser for official BLS release metadata extracts."""

from eie.core.errors import ValidationError
from eie.events.taxonomy import MACRO_CPI_US, MACRO_NFP_US, EventFamily, EventType
from eie.providers.common.models import (
    ItemKind,
    OfficialItem,
    OfficialSnapshot,
    component_codes,
    fragment_hash,
    load_extract,
    optional_time,
    parse_kind,
    period_from_item,
    required_string,
    schedule_fields,
)
from eie.providers.common.protocols import ProviderCapability

BLS_TYPES = {
    "CPI": MACRO_CPI_US,
    "PPI": EventType(EventFamily.MACRO, "MACRO.PPI.US"),
    "EMPLOYMENT_SITUATION": MACRO_NFP_US,
    "JOLTS": EventType(EventFamily.MACRO, "MACRO.JOLTS.US"),
    "ECI": EventType(EventFamily.MACRO, "MACRO.ECI.US"),
    "PRODUCTIVITY": EventType(EventFamily.MACRO, "MACRO.PRODUCTIVITY.US"),
}
BLS_COMPONENTS = frozenset({"CORE_CPI", "UNEMPLOYMENT_RATE", "AVERAGE_HOURLY_EARNINGS"})


class BLSAdapter:
    provider_id = "BLS"
    adapter_version = "bls-extract-1"

    def capabilities(self) -> frozenset[ProviderCapability]:
        return frozenset(
            {
                ProviderCapability.SCHEDULE_EXTRACT,
                ProviderCapability.RELEASE_EXTRACT,
                ProviderCapability.HISTORICAL_FIXTURE,
            }
        )

    def parse(self, payload: bytes) -> OfficialSnapshot:
        if payload.lstrip().startswith(b"BEGIN:VCALENDAR"):
            from eie.providers.bls.ical import parse_bls_ical

            return parse_bls_ical(payload)
        version, entries = load_extract(payload, self.provider_id)
        items: list[OfficialItem] = []
        for entry in entries:
            name = required_string(entry, "release")
            try:
                event_type = BLS_TYPES[name]
            except KeyError as exc:
                raise ValidationError("unsupported BLS release") from exc
            period = period_from_item(entry)
            if period is None:
                raise ValidationError("BLS release requires reference period")
            precision, stamp, day = schedule_fields(entry)
            kind = parse_kind(entry)
            if kind in (ItemKind.SCHEDULE, ItemKind.RESCHEDULE, ItemKind.SCHEDULE_CORRECTION) and (
                precision.value == "UNKNOWN"
            ):
                raise ValidationError("BLS schedule requires official date or time")
            items.append(
                OfficialItem(
                    self.provider_id,
                    required_string(entry, "release_id"),
                    required_string(entry, "item_version"),
                    version,
                    required_string(entry, "source_reference"),
                    event_type,
                    f"bls:{name}:{period.start_date}:{period.end_date}",
                    required_string(entry, "title"),
                    kind,
                    precision,
                    stamp,
                    day,
                    optional_time(entry, "published_at"),
                    period,
                    None,
                    fragment_hash(entry),
                    components=component_codes(entry, BLS_COMPONENTS),
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
