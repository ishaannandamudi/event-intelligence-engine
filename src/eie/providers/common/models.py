"""Typed, provider-neutral facts extracted from official source artifacts."""

import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from urllib.parse import parse_qsl, urlsplit

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.schema import OFFICIAL_SOURCE_SCHEMA_VERSION, require_schema
from eie.core.serialization import canonical_json_bytes
from eie.core.time import datetime_to_ns, require_ns
from eie.events.taxonomy import EventType
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.sources.models import SECRET_KEYS


class ItemKind(StrEnum):
    SCHEDULE = "SCHEDULE"
    RESCHEDULE = "RESCHEDULE"
    SCHEDULE_CORRECTION = "SCHEDULE_CORRECTION"
    RELEASE = "RELEASE"
    CORRECTION = "CORRECTION"
    CANCELLATION = "CANCELLATION"


class TimePrecision(StrEnum):
    EXACT_TIMESTAMP = "EXACT_TIMESTAMP"
    DATE_ONLY = "DATE_ONLY"
    UNKNOWN = "UNKNOWN"


class PeriodFrequency(StrEnum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    WEEKLY = "WEEKLY"


@dataclass(frozen=True, slots=True)
class ReferencePeriod:
    start_date: str
    end_date: str  # exclusive
    frequency: PeriodFrequency
    label: str

    def __post_init__(self) -> None:
        try:
            start, end = date.fromisoformat(self.start_date), date.fromisoformat(self.end_date)
        except (TypeError, ValueError) as exc:
            raise ValidationError("reference period dates must be ISO calendar dates") from exc
        if (
            start >= end
            or start.isoformat() != self.start_date
            or end.isoformat() != self.end_date
            or not isinstance(self.frequency, PeriodFrequency)
        ):
            raise ValidationError("invalid reference period interval or frequency")
        if not isinstance(self.label, str) or not self.label or self.label != self.label.strip():
            raise ValidationError("reference period label required")


def parse_aware_ns(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise ValidationError(f"{field} requires an aware ISO timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"invalid {field}") from exc
    return datetime_to_ns(stamp)


def validate_source_url(provider_id: str, url: str) -> None:
    expected = {
        "FEDERAL_RESERVE": {"federalreserve.gov", "www.federalreserve.gov"},
        "BLS": {"bls.gov", "www.bls.gov"},
        "BEA": {"bea.gov", "www.bea.gov", "apps.bea.gov"},
    }
    if not isinstance(url, str):
        raise ValidationError("official source reference must be a URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in expected.get(provider_id, set())
        or parsed.username is not None
        or parsed.password is not None
        or any(key.lower() in SECRET_KEYS for key, _ in parse_qsl(parsed.query))
    ):
        raise ValidationError("official reference requires an approved credential-free HTTPS host")


def parse_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} requires an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"invalid {field}") from exc
    if parsed.isoformat() != value:
        raise ValidationError(f"noncanonical {field}")
    return value


@dataclass(frozen=True, slots=True)
class OfficialItem:
    provider_id: str
    source_item_id: str
    source_item_version: str
    source_version: str
    source_reference: str
    event_type: EventType
    semantic_key: str
    title: str
    kind: ItemKind
    precision: TimePrecision
    scheduled_for_ns: int | None
    scheduled_date: str | None
    published_at_ns: int | None
    reference_period: ReferencePeriod | None
    release_stage: str | None
    raw_fragment_hash: str
    source_schema: str = OFFICIAL_SOURCE_SCHEMA_VERSION
    components: tuple[str, ...] = ()
    content_type: str = "application/vnd.eie.official-extract+json"
    source_metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_schema(self.source_schema, OFFICIAL_SOURCE_SCHEMA_VERSION)
        if not isinstance(self.event_type, EventType) or not isinstance(self.kind, ItemKind):
            raise ValidationError("typed official event and item kind required")
        for value in (
            self.provider_id,
            self.source_item_id,
            self.source_item_version,
            self.source_version,
            self.semantic_key,
            self.title,
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValidationError("official item identity fields must be trimmed")
        validate_source_url(self.provider_id, self.source_reference)
        if not isinstance(self.precision, TimePrecision):
            raise ValidationError("typed time precision required")
        if self.precision is TimePrecision.EXACT_TIMESTAMP:
            if self.scheduled_for_ns is None or self.scheduled_date is not None:
                raise ValidationError("exact schedule requires timestamp only")
            require_ns(self.scheduled_for_ns, "scheduled_for_ns")
        elif self.precision is TimePrecision.DATE_ONLY:
            if self.scheduled_for_ns is not None or self.scheduled_date is None:
                raise ValidationError("date-only schedule requires date only")
            parse_date(self.scheduled_date, "scheduled_date")
        elif self.scheduled_for_ns is not None or self.scheduled_date is not None:
            raise ValidationError("unknown schedule time cannot carry date or timestamp")
        if self.published_at_ns is not None:
            require_ns(self.published_at_ns, "published_at_ns")
        if self.reference_period is not None and not isinstance(
            self.reference_period, ReferencePeriod
        ):
            raise ValidationError("typed reference period required")
        if self.release_stage is not None and (
            not isinstance(self.release_stage, str) or not self.release_stage
        ):
            raise ValidationError("invalid release stage")
        if len(self.raw_fragment_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.raw_fragment_hash
        ):
            raise ValidationError("official raw fragment hash must be SHA-256")
        if (
            not isinstance(self.components, tuple)
            or any(not isinstance(code, str) or not code for code in self.components)
            or self.components != tuple(sorted(set(self.components)))
        ):
            raise ValidationError("component codes must be sorted unique strings")
        if not isinstance(self.content_type, str) or not self.content_type:
            raise ValidationError("official item content type required")
        if (
            not isinstance(self.source_metadata, tuple)
            or any(
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(part, str) and part for part in pair)
                for pair in self.source_metadata
            )
            or tuple(key for key, _ in self.source_metadata)
            != tuple(sorted({key for key, _ in self.source_metadata}))
        ):
            raise ValidationError("source metadata must be sorted unique string pairs")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:official-item:v1", self)


@dataclass(frozen=True, slots=True)
class OfficialSnapshot:
    provider_id: str
    source_version: str
    items: tuple[OfficialItem, ...]
    schema_version: str = OFFICIAL_SOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, OFFICIAL_SOURCE_SCHEMA_VERSION)
        if (
            not isinstance(self.provider_id, str)
            or not self.provider_id
            or not isinstance(self.source_version, str)
            or not self.source_version
        ):
            raise ValidationError("snapshot provider and source version required")
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, OfficialItem) or item.provider_id != self.provider_id
            for item in self.items
        ):
            raise ValidationError("snapshot contains invalid provider item")
        keys = tuple(
            (item.semantic_key, item.event_type.code, item.kind.value, item.source_item_id)
            for item in self.items
        )
        if keys != tuple(sorted(set(keys))):
            raise ValidationError("official items must be sorted and unique")


def load_extract(payload: bytes, provider_id: str) -> tuple[str, list[dict[str, object]]]:
    """Parse the versioned, minimal official-source extraction fixture format."""
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderParseError("official extract is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("format") != "EIE_OFFICIAL_EXTRACT_V1":
        raise ProviderSchemaMismatchError("unrecognized official extract schema")
    if value.get("provider") != provider_id:
        raise ProviderSchemaMismatchError("official extract provider mismatch")
    version, items = value.get("source_version"), value.get("items")
    if not isinstance(version, str) or not version or not isinstance(items, list):
        raise ProviderSchemaMismatchError("official extract requires source version and items")
    if any(not isinstance(item, dict) for item in items):
        raise ProviderSchemaMismatchError("official extract items must be objects")
    return version, items


def required_string(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"official item requires {key}")
    return value


def optional_time(item: dict[str, object], key: str) -> int | None:
    value = item.get(key)
    return None if value is None else parse_aware_ns(value, key)


def schedule_fields(item: dict[str, object]) -> tuple[TimePrecision, int | None, str | None]:
    timestamp, day = item.get("scheduled_at"), item.get("scheduled_date")
    if timestamp is not None and day is not None:
        raise ValidationError("schedule cannot have both date and timestamp")
    if timestamp is not None:
        return TimePrecision.EXACT_TIMESTAMP, parse_aware_ns(timestamp, "scheduled_at"), None
    if day is not None:
        return TimePrecision.DATE_ONLY, None, parse_date(day, "scheduled_date")
    return TimePrecision.UNKNOWN, None, None


def period_from_item(item: dict[str, object]) -> ReferencePeriod | None:
    value = item.get("reference_period")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("reference period must be an object")
    try:
        frequency = PeriodFrequency(required_string(value, "frequency"))
    except ValueError as exc:
        raise ValidationError("unknown reference-period frequency") from exc
    return ReferencePeriod(
        required_string(value, "start_date"),
        required_string(value, "end_date"),
        frequency,
        required_string(value, "label"),
    )


def fragment_hash(item: dict[str, object]) -> str:
    return sha256(canonical_json_bytes(item)).hexdigest()


def component_codes(item: dict[str, object], allowed: frozenset[str]) -> tuple[str, ...]:
    value = item.get("components", [])
    if not isinstance(value, list) or any(not isinstance(code, str) for code in value):
        raise ValidationError("components must be strings")
    codes = tuple(sorted(set(value)))
    if len(codes) != len(value) or any(code not in allowed for code in codes):
        raise ValidationError("duplicate or unknown official component")
    return codes


def parse_kind(item: dict[str, object]) -> ItemKind:
    try:
        return ItemKind(required_string(item, "status"))
    except ValueError as exc:
        raise ValidationError("unknown official item status") from exc
