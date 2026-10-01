"""Strict EIE interchange parser for synthetic or audited provider extracts."""

import json
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import parse_qsl, urlsplit

from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.corporate.models import (
    Buyback,
    CorporateIdentity,
    CorporateKind,
    CorporatePayload,
    CorporateStatus,
    Deal,
    Delisting,
    Dividend,
    EarningsCall,
    EarningsPeriod,
    EarningsRelease,
    EarningsSchedule,
    ManagementChange,
    Money,
    ReleaseTiming,
    SchedulePrecision,
    SpinOff,
    Split,
    SymbolChange,
    TenderOffer,
)
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.sources.models import SECRET_KEYS

EXTRACT_VERSION = "EIE_CORPORATE_EXTRACT_V1"


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProviderSchemaMismatchError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderSchemaMismatchError(f"{name} must be nonempty trimmed text")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderSchemaMismatchError(f"{name} must be integer")
    return value


def _optional_int(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ProviderSchemaMismatchError(f"{name} must be boolean")
    return value


def _enum(
    enum_type: type[CorporateKind]
    | type[CorporateStatus]
    | type[ReleaseTiming]
    | type[SchedulePrecision],
    value: object,
    name: str,
) -> CorporateKind | CorporateStatus | ReleaseTiming | SchedulePrecision:
    try:
        return enum_type(_text(value, name))
    except ValueError as exc:
        raise ProviderSchemaMismatchError(f"unsupported {name}") from exc


def _url(value: object, name: str) -> str:
    url = _text(value, name)
    try:
        parts = urlsplit(url)
        invalid = (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or any(key.lower() in SECRET_KEYS for key, _ in parse_qsl(parts.query))
        )
    except ValueError as exc:
        raise ValidationError(f"{name} requires credential-free HTTPS") from exc
    if invalid:
        raise ValidationError(f"{name} requires credential-free HTTPS")
    return url


def _period(data: dict[str, object]) -> EarningsPeriod:
    return EarningsPeriod(
        _integer(data.get("fiscal_year"), "fiscal_year"),
        _optional_int(data.get("fiscal_quarter"), "fiscal_quarter"),
        _text(data.get("period_end"), "period_end"),
    )


def _money(value: object, name: str) -> Money | None:
    if value is None:
        return None
    data = _object(value, name)
    return Money(
        _integer(data.get("amount_micros"), "amount_micros"),
        _text(data.get("currency"), "currency"),
        _boolean(data.get("per_share", False), "per_share"),
    )


def _payload(kind: CorporateKind, value: object) -> CorporatePayload:
    data = _object(value, "corporate payload")
    if kind is CorporateKind.EARNINGS_SCHEDULE:
        return EarningsSchedule(
            _period(data),
            _optional_text(data.get("scheduled_date"), "scheduled_date"),
            _optional_int(data.get("scheduled_for_ns"), "scheduled_for_ns"),
            ReleaseTiming(_enum(ReleaseTiming, data.get("release_timing"), "release_timing")),
            SchedulePrecision(_enum(SchedulePrecision, data.get("precision"), "precision")),
        )
    if kind is CorporateKind.EARNINGS_RELEASE:
        return EarningsRelease(
            _period(data), _integer(data.get("published_at_ns"), "published_at_ns")
        )
    if kind is CorporateKind.EARNINGS_CALL:
        return EarningsCall(
            _period(data),
            _integer(data.get("scheduled_for_ns"), "scheduled_for_ns"),
            _optional_text(data.get("webcast_url"), "webcast_url"),
        )
    if kind in (CorporateKind.DIVIDEND, CorporateKind.SPECIAL_DIVIDEND):
        return Dividend(
            _money(data.get("amount"), "amount"),
            _optional_text(data.get("record_date"), "record_date"),
            _optional_text(data.get("ex_date"), "ex_date"),
            _optional_text(data.get("payment_date"), "payment_date"),
            _optional_text(data.get("frequency"), "frequency"),
        )
    if kind in (CorporateKind.STOCK_SPLIT, CorporateKind.REVERSE_SPLIT):
        return Split(
            _integer(data.get("numerator"), "numerator"),
            _integer(data.get("denominator"), "denominator"),
            _optional_text(data.get("record_date"), "record_date"),
            _optional_text(data.get("effective_date"), "effective_date"),
            _optional_text(data.get("trading_adjustment_date"), "trading_adjustment_date"),
        )
    if kind is CorporateKind.BUYBACK:
        return Buyback(
            _money(data.get("authorization"), "authorization"),
            _optional_int(data.get("shares"), "shares"),
            _optional_int(data.get("percent_basis_points"), "percent_basis_points"),
            _optional_text(data.get("expiration_date"), "expiration_date"),
            _boolean(data.get("expanded", False), "expanded"),
        )
    if kind is CorporateKind.TENDER_OFFER:
        return TenderOffer(
            _money(data.get("price"), "price"),
            _optional_int(data.get("expiration_at_ns"), "expiration_at_ns"),
            _optional_text(data.get("tender_type"), "tender_type"),
        )
    if kind in (CorporateKind.MERGER, CorporateKind.ACQUISITION):
        return Deal(
            _optional_text(data.get("target_issuer_id"), "target_issuer_id"),
            _optional_text(data.get("acquirer_issuer_id"), "acquirer_issuer_id"),
            _optional_text(data.get("unresolved_party_name"), "unresolved_party_name"),
            _optional_text(data.get("expected_close_date"), "expected_close_date"),
            _optional_text(data.get("actual_close_date"), "actual_close_date"),
            _optional_text(data.get("termination_date"), "termination_date"),
            _optional_int(data.get("actual_close_at_ns"), "actual_close_at_ns"),
            _optional_int(data.get("terminated_at_ns"), "terminated_at_ns"),
            _money(data.get("cash_component"), "cash_component"),
            _optional_int(data.get("stock_component_shares"), "stock_component_shares"),
            _optional_text(data.get("transaction_structure"), "transaction_structure"),
            _money(data.get("offer_price"), "offer_price"),
        )
    if kind is CorporateKind.SPIN_OFF:
        return SpinOff(
            _optional_text(data.get("spun_issuer_id"), "spun_issuer_id"),
            _optional_text(data.get("record_date"), "record_date"),
            _optional_text(data.get("distribution_date"), "distribution_date"),
            _optional_int(data.get("ratio_numerator"), "ratio_numerator"),
            _optional_int(data.get("ratio_denominator"), "ratio_denominator"),
        )
    if kind is CorporateKind.SYMBOL_CHANGE:
        return SymbolChange(
            _text(data.get("security_id"), "security_id"),
            _text(data.get("old_ticker"), "old_ticker"),
            _text(data.get("old_exchange"), "old_exchange"),
            _text(data.get("new_ticker"), "new_ticker"),
            _text(data.get("new_exchange"), "new_exchange"),
            _integer(data.get("effective_at_ns"), "effective_at_ns"),
        )
    if kind is CorporateKind.DELISTING:
        return Delisting(
            _text(data.get("security_id"), "security_id"),
            _optional_text(data.get("exchange"), "exchange"),
            _optional_text(data.get("expected_date"), "expected_date"),
            _optional_text(data.get("effective_date"), "effective_date"),
            _optional_text(data.get("reason"), "reason"),
        )
    return ManagementChange(
        _text(data.get("person_name"), "person_name"),
        _text(data.get("role"), "role"),
        _optional_text(data.get("source_person_id"), "source_person_id"),
        _optional_text(data.get("effective_date"), "effective_date"),
    )


@dataclass(frozen=True, slots=True)
class CorporateProposal:
    identity: CorporateIdentity
    provider_event_id: str
    payload: CorporatePayload
    status: CorporateStatus
    announced_at_ns: int | None
    effective_at_ns: int | None
    announced_date: str | None = None


@dataclass(frozen=True, slots=True)
class CorporateSourceItem:
    item_id: str
    item_version: str
    published_at_ns: int | None
    source_reference: str
    proposals: tuple[CorporateProposal, ...]
    payload_hash: str


@dataclass(frozen=True, slots=True)
class CorporateBatch:
    provider_id: str
    source_version: str
    items: tuple[CorporateSourceItem, ...]
    covered_kinds: tuple[CorporateKind, ...]
    coverage_from_date: str | None
    coverage_to_date: str | None


def parse_batch(payload: bytes, expected_provider_id: str) -> CorporateBatch:
    try:
        root = _object(json.loads(payload), "corporate extract")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderParseError("invalid corporate JSON") from exc
    if root.get("schema") != EXTRACT_VERSION:
        raise ProviderSchemaMismatchError("corporate extract schema mismatch")
    provider_id = _text(root.get("provider_id"), "provider_id")
    if provider_id != expected_provider_id:
        raise ProviderSchemaMismatchError("corporate provider ID mismatch")
    source_version = _text(root.get("source_version"), "source_version")
    raw_items = root.get("items")
    if not isinstance(raw_items, list):
        raise ProviderSchemaMismatchError("corporate items must be list")
    items = []
    for raw_item in raw_items:
        item = _object(raw_item, "corporate item")
        raw_events = item.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            raise ProviderSchemaMismatchError("corporate source item needs events")
        proposals = []
        for raw_event in raw_events:
            event = _object(raw_event, "corporate event")
            kind = CorporateKind(_enum(CorporateKind, event.get("kind"), "kind"))
            corporate_payload = _payload(kind, event.get("data"))
            supplied_key = _optional_text(event.get("semantic_key"), "semantic_key")
            period = getattr(corporate_payload, "period", None)
            if isinstance(period, EarningsPeriod):
                key = period.key
                if supplied_key is not None and supplied_key != key:
                    raise ValidationError("earnings semantic key must equal reporting period")
            else:
                key = _text(supplied_key, "semantic_key")
            identity = CorporateIdentity(
                kind,
                _text(event.get("issuer_id"), "issuer_id"),
                key,
                _optional_text(event.get("security_id"), "security_id"),
                _optional_text(event.get("listing_id"), "listing_id"),
            )
            proposals.append(
                CorporateProposal(
                    identity,
                    _text(event.get("provider_event_id"), "provider_event_id"),
                    corporate_payload,
                    CorporateStatus(_enum(CorporateStatus, event.get("status"), "status")),
                    _optional_int(event.get("announced_at_ns"), "announced_at_ns"),
                    _optional_int(event.get("effective_at_ns"), "effective_at_ns"),
                    _optional_text(event.get("announced_date"), "announced_date"),
                )
            )
        published = _optional_int(item.get("published_at_ns"), "published_at_ns")
        if published is not None:
            require_ns(published, "published_at_ns")
        items.append(
            CorporateSourceItem(
                _text(item.get("item_id"), "item_id"),
                _text(item.get("item_version", "1"), "item_version"),
                published,
                _url(item.get("source_reference"), "source_reference"),
                tuple(proposals),
                sha256(canonical_json_bytes(item)).hexdigest(),
            )
        )
    raw_coverage = root.get("coverage")
    if raw_coverage is None:
        return CorporateBatch(provider_id, source_version, tuple(items), (), None, None)
    coverage = _object(raw_coverage, "coverage")
    raw_kinds = coverage.get("kinds")
    if not isinstance(raw_kinds, list):
        raise ProviderSchemaMismatchError("coverage kinds must be list")
    kinds = tuple(
        sorted({CorporateKind(_enum(CorporateKind, value, "coverage kind")) for value in raw_kinds})
    )
    start = _text(coverage.get("from_date"), "coverage from_date")
    end = _text(coverage.get("to_date"), "coverage to_date")
    from eie.providers.sec.models import iso_date

    if iso_date(start, "from_date") > iso_date(end, "to_date"):
        raise ValidationError("coverage date range reversed")
    return CorporateBatch(provider_id, source_version, tuple(items), kinds, start, end)
