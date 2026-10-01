"""Strict EIE_ESTIMATES_EXTRACT_V1 synthetic/audited interchange parser."""

import json
from hashlib import sha256
from urllib.parse import parse_qsl, urlsplit

from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.corporate.types import EarningsPeriod
from eie.expectations.models import MeasurementContext, MeasurementId, Period, SeasonalAdjustment
from eie.expectations.observations import ActualRole, EstimateKind
from eie.expectations.units import Magnitude, Quantity, Unit, UnitKind
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.common.models import PeriodFrequency, ReferencePeriod
from eie.providers.estimates.models import EstimateBatch, EstimateItem, EstimateRecord, RecordKind
from eie.sources.models import SECRET_KEYS

EXTRACT_VERSION = "EIE_ESTIMATES_EXTRACT_V1"


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProviderSchemaMismatchError("estimate field requires object")
    return value


def string(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderSchemaMismatchError("estimate field requires trimmed text")
    return value


def integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderSchemaMismatchError("estimate field requires integer")
    return value


def optional_time(value: object) -> int | None:
    return None if value is None else require_ns(integer(value))


def period(value: object) -> Period:
    data = obj(value)
    if data.get("type") == "EARNINGS":
        quarter = data.get("fiscal_quarter")
        return EarningsPeriod(
            integer(data.get("fiscal_year")),
            None if quarter is None else integer(quarter),
            string(data.get("period_end")),
        )
    if data.get("type") == "MACRO":
        return ReferencePeriod(
            string(data.get("start_date")),
            string(data.get("end_date")),
            PeriodFrequency(string(data.get("frequency"))),
            string(data.get("label")),
        )
    raise ProviderSchemaMismatchError("unsupported estimate period type")


def quantity(value: object) -> Quantity:
    data = obj(value)
    raw = data.get("value")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ProviderSchemaMismatchError("estimate value requires finite numeric JSON")
    unit = obj(data.get("unit"))
    currency = unit.get("currency")
    return Quantity(
        raw,
        Unit(
            UnitKind(string(unit.get("kind"))),
            Magnitude(string(unit.get("magnitude", "ONE"))),
            None if currency is None else string(currency),
        ),
    )


def url(value: object) -> str:
    reference = string(value)
    try:
        parsed = urlsplit(reference)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and not any(k.lower() in SECRET_KEYS for k, _ in parse_qsl(parsed.query))
        )
    except ValueError as exc:
        raise ValidationError("malformed estimate source reference") from exc
    if not valid:
        raise ValidationError("estimate source reference requires credential-free HTTPS")
    return reference


def record(value: object) -> EstimateRecord:
    data = obj(value)
    kind = RecordKind(string(data.get("observation_type")))
    stage = data.get("release_stage")
    context = MeasurementContext(
        string(data.get("event_id")),
        MeasurementId(string(data.get("measurement_id"))),
        period(data.get("reference_period")),
        None if stage is None else string(stage),
        SeasonalAdjustment(string(data.get("seasonal_adjustment", "UNSPECIFIED"))),
    )
    common = (
        string(data.get("record_id")),
        kind,
        context,
        optional_time(data.get("vintage_at_ns")),
    )
    if kind is RecordKind.ESTIMATE:
        contributor = data.get("contributor_id")
        count = data.get("contributor_count")
        return EstimateRecord(
            *common,
            value=quantity(data.get("quantity")),
            estimate_type=EstimateKind(string(data.get("estimate_type"))),
            contributor_id=None if contributor is None else string(contributor),
            contributor_count=None if count is None else integer(count),
        )
    if kind is RecordKind.ACTUAL:
        previous = data.get("previous_reference_period")
        return EstimateRecord(
            *common,
            value=quantity(data.get("quantity")),
            role=ActualRole(string(data.get("role", "ACTUAL"))),
            published_at_ns=require_ns(integer(data.get("published_at_ns"))),
            previous_reference_period=None if previous is None else period(previous),
        )
    return EstimateRecord(
        *common,
        issuer_id=string(data.get("issuer_id")),
        published_at_ns=require_ns(integer(data.get("published_at_ns"))),
        low=None if data.get("low") is None else quantity(data["low"]),
        high=None if data.get("high") is None else quantity(data["high"]),
        point=None if data.get("point") is None else quantity(data["point"]),
    )


def parse_batch(payload: bytes, provider_id: str) -> EstimateBatch:
    try:
        data = obj(json.loads(payload))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderParseError("invalid estimate JSON") from exc
    if data.get("schema") != EXTRACT_VERSION or data.get("provider_id") != provider_id:
        raise ProviderSchemaMismatchError("estimate extract schema/provider mismatch")
    items_data = data.get("items")
    if not isinstance(items_data, list):
        raise ProviderSchemaMismatchError("estimate items require list")
    items = []
    for value in items_data:
        item = obj(value)
        records = item.get("records")
        if not isinstance(records, list) or not records:
            raise ProviderSchemaMismatchError("estimate item needs records")
        try:
            parsed = tuple(record(v) for v in records)
        except ValueError as exc:
            raise ProviderSchemaMismatchError("unsupported estimate enum") from exc
        items.append(
            EstimateItem(
                string(item.get("item_id")),
                string(item.get("item_version", "1")),
                optional_time(item.get("published_at_ns")),
                url(item.get("source_reference")),
                parsed,
                sha256(canonical_json_bytes(item)).hexdigest(),
            )
        )
    coverage = data.get("coverage")
    events: tuple[str, ...] = ()
    measurements: tuple[str, ...] = ()
    vintages: bool | None = None
    if coverage is not None:
        covered = obj(coverage)
        raw_events, raw_measurements = covered.get("event_ids"), covered.get("measurements")
        if (
            not isinstance(raw_events, list)
            or not isinstance(raw_measurements, list)
            or not isinstance(covered.get("has_vintages"), bool)
        ):
            raise ProviderSchemaMismatchError("invalid estimate coverage")
        events = tuple(sorted({string(v) for v in raw_events}))
        measurements = tuple(sorted({MeasurementId(string(v)).code for v in raw_measurements}))
        vintage_value = covered["has_vintages"]
        assert isinstance(vintage_value, bool)
        vintages = vintage_value
    return EstimateBatch(
        provider_id,
        string(data.get("source_version")),
        tuple(items),
        events,
        measurements,
        vintages,
    )
