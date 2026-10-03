"""Strict plain-JSON proposals; source-controlled keys cannot redefine runtime schemas."""

import re
from typing import cast

from eie.core.errors import EIEError, ValidationError
from eie.corporate.types import EarningsPeriod, ReleaseTiming
from eie.evidence.claims import TimePrecision, TimeValue
from eie.expectations.units import Magnitude, Quantity, Unit, UnitKind
from eie.nlp.models import (
    EventProposal,
    NumericFieldProposal,
    PeriodProposal,
    SpanProposal,
    TemporalFieldProposal,
    TextFieldProposal,
)
from eie.nlp.normalization import TextLimits
from eie.nlp.schema import (
    EpistemicStatus,
    FieldName,
    MetricQualifier,
    NumericBound,
    UnscheduledFamily,
)


class OutputSchemaError(ValidationError):
    """Safe error category; never includes untrusted output or provider secrets."""


def instruction_like(text: str) -> bool:
    return (
        re.search(
            r"ignore (?:all |previous )?instructions|(?:system|developer) (?:message|prompt)\s*:|"
            r"(?:run|execute) (?:this |the )?(?:command|shell)|send credentials|"
            r"(?:call|visit|fetch) (?:this |the )?https?://|\btool_calls?\b|"
            r'"(?:tool|command)"\s*:|'
            r"mark .{0,60}\ba buy\b|redefine .{0,30}schema",
            text,
            re.I,
        )
        is not None
    )


def _object(
    value: object, required: set[str], optional: frozenset[str] = frozenset()
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or any(not isinstance(k, str) for k in value)
        or not required <= set(value)
        or not set(value) <= required | optional
    ):
        raise OutputSchemaError("strict extraction output fields required")
    return cast(dict[str, object], value)


def _array(value: object, maximum: int) -> tuple[object, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise OutputSchemaError("bounded extraction output array required")
    return tuple(value)


def _string(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise OutputSchemaError("bounded extraction output string required")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OutputSchemaError("extraction output integer required")
    return value


def _span(value: object) -> SpanProposal:
    d = _object(value, {"start", "end", "fragment_hash"})
    return SpanProposal(_integer(d["start"]), _integer(d["end"]), _string(d["fragment_hash"]))


def _quantity(value: object) -> Quantity:
    d = _object(value, {"value", "unit"})
    number = d["value"]
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        raise OutputSchemaError("numeric proposal value required")
    u = _object(d["unit"], {"kind", "magnitude", "currency"})
    currency = u["currency"]
    if currency is not None and not isinstance(currency, str):
        raise OutputSchemaError("typed currency required")
    return Quantity(
        number, Unit(UnitKind(_string(u["kind"])), Magnitude(_string(u["magnitude"])), currency)
    )


def _numeric(value: object) -> NumericFieldProposal:
    d = _object(value, {"name", "quantity", "span", "upper", "bound", "qualifiers"})
    return NumericFieldProposal(
        FieldName(_string(d["name"])),
        _quantity(d["quantity"]),
        _span(d["span"]),
        None if d["upper"] is None else _quantity(d["upper"]),
        NumericBound(_string(d["bound"])),
        tuple(MetricQualifier(_string(q)) for q in _array(d["qualifiers"], 7)),
    )


def _text_field(value: object) -> TextFieldProposal:
    d = _object(value, {"name", "value", "span"})
    return TextFieldProposal(FieldName(_string(d["name"])), _string(d["value"]), _span(d["span"]))


def _temporal(value: object) -> TemporalFieldProposal | None:
    if value is None:
        return None
    d = _object(value, {"precision", "date", "timestamp_ns", "session", "span"})
    day = d["date"]
    if day is not None and not isinstance(day, str):
        raise OutputSchemaError("typed proposed date required")
    timestamp = None if d["timestamp_ns"] is None else _integer(d["timestamp_ns"])
    return TemporalFieldProposal(
        TimeValue(
            TimePrecision(_string(d["precision"])),
            day,
            timestamp,
            ReleaseTiming(_string(d["session"])),
        ),
        _span(d["span"]),
    )


def _period(value: object) -> PeriodProposal | None:
    if value is None:
        return None
    d = _object(value, {"fiscal_year", "fiscal_quarter", "period_end", "span"})
    quarter = None if d["fiscal_quarter"] is None else _integer(d["fiscal_quarter"])
    return PeriodProposal(
        EarningsPeriod(_integer(d["fiscal_year"]), quarter, _string(d["period_end"])),
        _span(d["span"]),
    )


def decode_proposals(payload: bytes, limits: TextLimits) -> tuple[EventProposal, ...]:
    from eie.persistence.codec import strict_json

    try:
        if not isinstance(payload, bytes) or len(payload) > limits.model_output_bytes:
            raise OutputSchemaError("bounded model response bytes required")
        root = _object(strict_json(payload), {"events"})
        result = []
        for value in _array(root["events"], min(limits.candidates, 128)):
            d = _object(
                value,
                {
                    "key",
                    "family",
                    "epistemic",
                    "span",
                    "entity_ids",
                    "numerics",
                    "text_fields",
                    "temporal",
                    "period",
                },
            )
            result.append(
                EventProposal(
                    _string(d["key"]),
                    UnscheduledFamily(_string(d["family"])),
                    EpistemicStatus(_string(d["epistemic"])),
                    _span(d["span"]),
                    tuple(_string(x) for x in _array(d["entity_ids"], 16)),
                    tuple(_numeric(x) for x in _array(d["numerics"], 32)),
                    tuple(_text_field(x) for x in _array(d["text_fields"], 32)),
                    _temporal(d["temporal"]),
                    _period(d["period"]),
                )
            )
        if len({p.key for p in result}) != len(result):
            raise OutputSchemaError("distinct source-local event keys required")
        return tuple(sorted(result, key=lambda p: p.key))
    except (EIEError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise OutputSchemaError("invalid schema-constrained model output") from exc
