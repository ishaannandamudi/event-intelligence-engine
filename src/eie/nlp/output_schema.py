"""Fixed JSON schema supplied by trusted code, independent of document content."""

from typing import cast

from eie.core.serialization import JSONValue, canonical_json_bytes
from eie.expectations.units import UnitKind
from eie.nlp.schema import (
    EpistemicStatus,
    FieldName,
    MetricQualifier,
    NumericBound,
    UnscheduledFamily,
)


def obj(properties: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def ref(name: str) -> dict[str, JSONValue]:
    return {"$ref": "#/$defs/" + name}


def nullable(value: JSONValue) -> dict[str, JSONValue]:
    return {"anyOf": [value, {"type": "null"}]}


SPAN = obj(
    {
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        "fragment_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
)
STRING: dict[str, JSONValue] = {"type": "string", "minLength": 1, "maxLength": 256}
QUANTITY = obj(
    {
        "value": {"type": "number"},
        "unit": obj(
            {
                "kind": {"enum": [k.value for k in UnitKind]},
                "magnitude": {"enum": ["ONE", "THOUSAND", "MILLION", "BILLION"]},
                "currency": nullable(STRING),
            }
        ),
    }
)
NUMERIC = obj(
    {
        "name": {"enum": [cast(JSONValue, n.value) for n in FieldName][:6]},
        "quantity": ref("quantity"),
        "span": ref("span"),
        "upper": nullable(ref("quantity")),
        "bound": {"enum": [n.value for n in NumericBound]},
        "qualifiers": {
            "type": "array",
            "maxItems": 7,
            "uniqueItems": True,
            "items": {"enum": [q.value for q in MetricQualifier]},
        },
    }
)
TEXT = obj(
    {
        "name": {"enum": [cast(JSONValue, n.value) for n in FieldName][6:]},
        "value": STRING,
        "span": ref("span"),
    }
)
TIME = obj(
    {
        "precision": {"enum": ["UNKNOWN", "DATE_ONLY", "DATE_PLUS_SESSION", "EXACT"]},
        "date": nullable(STRING),
        "timestamp_ns": nullable({"type": "integer"}),
        "session": {"enum": ["UNKNOWN", "BEFORE_MARKET", "DURING_MARKET", "AFTER_MARKET"]},
        "span": ref("span"),
    }
)
PERIOD = obj(
    {
        "fiscal_year": {"type": "integer"},
        "fiscal_quarter": nullable({"type": "integer", "minimum": 1, "maximum": 4}),
        "period_end": STRING,
        "span": ref("span"),
    }
)
EVENT = obj(
    {
        "key": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_.:-]{0,79}$"},
        "family": {"enum": [n.value for n in UnscheduledFamily]},
        "epistemic": {"enum": [n.value for n in EpistemicStatus]},
        "span": ref("span"),
        "entity_ids": {
            "type": "array",
            "maxItems": 16,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^iss_[0-9a-f]{64}$"},
        },
        "numerics": {"type": "array", "maxItems": 32, "items": ref("numeric")},
        "text_fields": {"type": "array", "maxItems": 32, "items": ref("text")},
        "temporal": nullable(ref("time")),
        "period": nullable(ref("period")),
    }
)
OUTPUT_SCHEMA = canonical_json_bytes(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **obj({"events": {"type": "array", "maxItems": 128, "items": ref("event")}}),
        "$defs": {
            "span": SPAN,
            "quantity": QUANTITY,
            "numeric": NUMERIC,
            "text": TEXT,
            "time": TIME,
            "period": PERIOD,
            "event": EVENT,
        },
    }
).decode()
