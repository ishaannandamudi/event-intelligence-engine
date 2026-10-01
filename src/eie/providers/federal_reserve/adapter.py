"""Pure parser for small, versioned extracts of official FOMC calendar items."""

from eie.core.errors import ValidationError
from eie.events.taxonomy import (
    FOMC_DECISION_US,
    FOMC_MINUTES_US,
    FOMC_PRESS_CONFERENCE_US,
    FOMC_SEP_US,
    FOMC_STATEMENT_US,
    EventType,
)
from eie.providers.common.models import (
    OfficialItem,
    OfficialSnapshot,
    component_codes,
    fragment_hash,
    load_extract,
    optional_time,
    parse_date,
    parse_kind,
    required_string,
    schedule_fields,
)
from eie.providers.common.protocols import ProviderCapability

FOMC_TYPES: dict[str, EventType] = {
    "DECISION": FOMC_DECISION_US,
    "STATEMENT": FOMC_STATEMENT_US,
    "SEP": FOMC_SEP_US,
    "PRESS_CONFERENCE": FOMC_PRESS_CONFERENCE_US,
    "MINUTES": FOMC_MINUTES_US,
}


class FederalReserveAdapter:
    provider_id = "FEDERAL_RESERVE"
    adapter_version = "fed-extract-1"

    def capabilities(self) -> frozenset[ProviderCapability]:
        return frozenset(
            {
                ProviderCapability.SCHEDULE_EXTRACT,
                ProviderCapability.RELEASE_EXTRACT,
                ProviderCapability.DATE_ONLY,
                ProviderCapability.HISTORICAL_FIXTURE,
            }
        )

    def parse(self, payload: bytes) -> OfficialSnapshot:
        version, entries = load_extract(payload, self.provider_id)
        items: list[OfficialItem] = []
        for entry in entries:
            meeting_id = required_string(entry, "meeting_id")
            meeting_start = parse_date(entry.get("meeting_start_date"), "meeting_start_date")
            meeting_end = parse_date(entry.get("meeting_end_date"), "meeting_end_date")
            if meeting_end < meeting_start:
                raise ValidationError("FOMC meeting span is reversed")
            reference = required_string(entry, "source_reference")
            item_version = required_string(entry, "item_version")
            events = entry.get("events")
            if not isinstance(events, list) or not events:
                raise ValidationError("FOMC entry requires distinct event items")
            for event in events:
                if not isinstance(event, dict):
                    raise ValidationError("FOMC event must be an object")
                name = required_string(event, "event")
                try:
                    event_type = FOMC_TYPES[name]
                except KeyError as exc:
                    raise ValidationError("unsupported FOMC event") from exc
                precision, stamp, day = schedule_fields(event)
                if (
                    event.get("status") in ("SCHEDULE", "RESCHEDULE", "SCHEDULE_CORRECTION")
                    and precision.value == "UNKNOWN"
                ):
                    raise ValidationError("FOMC schedule requires date or exact time")
                items.append(
                    OfficialItem(
                        self.provider_id,
                        f"{meeting_id}:{name}",
                        item_version,
                        version,
                        reference,
                        event_type,
                        f"fomc:{meeting_id}:{name}",
                        f"FOMC {name.replace('_', ' ').title()} ({meeting_id})",
                        parse_kind(event),
                        precision,
                        stamp,
                        day,
                        optional_time(event, "published_at"),
                        None,
                        None,
                        fragment_hash(
                            {
                                "meeting_id": meeting_id,
                                "meeting_start_date": meeting_start,
                                "meeting_end_date": meeting_end,
                                "event": event,
                            }
                        ),
                        components=component_codes(event, frozenset()),
                        source_metadata=(
                            ("fed_meeting_end_date", meeting_end),
                            ("fed_meeting_start_date", meeting_start),
                        ),
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
