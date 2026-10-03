"""Plain output JSON for offline fixtures; the strict decoder remains the trust boundary."""

from eie.core.serialization import JSONValue, canonical_json_bytes
from eie.expectations.units import Quantity
from eie.nlp.models import EventProposal, SpanProposal


def _span(value: SpanProposal) -> dict[str, JSONValue]:
    return {"start": value.start, "end": value.end, "fragment_hash": value.fragment_hash}


def _quantity(value: Quantity | None) -> JSONValue:
    if value is None:
        return None
    return {
        "value": value.value,
        "unit": {
            "kind": value.unit.kind.value,
            "magnitude": value.unit.magnitude.value,
            "currency": value.unit.currency,
        },
    }


def proposal_bytes(proposals: tuple[EventProposal, ...]) -> bytes:
    result: list[JSONValue] = []
    for p in proposals:
        temporal: JSONValue = None
        period: JSONValue = None
        if p.temporal is not None:
            t = p.temporal.value
            temporal = {
                "precision": t.precision.value,
                "date": t.date,
                "timestamp_ns": t.timestamp_ns,
                "session": t.session.value,
                "span": _span(p.temporal.span),
            }
        if p.period is not None:
            v = p.period.period
            period = {
                "fiscal_year": v.fiscal_year,
                "fiscal_quarter": v.fiscal_quarter,
                "period_end": v.period_end,
                "span": _span(p.period.span),
            }
        numerics: list[JSONValue] = [
            {
                "name": n.name.value,
                "quantity": _quantity(n.quantity),
                "upper": _quantity(n.upper),
                "bound": n.bound.value,
                "qualifiers": [q.value for q in n.qualifiers],
                "span": _span(n.span),
            }
            for n in p.numerics
        ]
        fields: list[JSONValue] = [
            {"name": f.name.value, "value": f.value, "span": _span(f.span)} for f in p.text_fields
        ]
        result.append(
            {
                "key": p.key,
                "family": p.family.value,
                "epistemic": p.epistemic.value,
                "span": _span(p.span),
                "entity_ids": list(p.entity_ids),
                "numerics": numerics,
                "text_fields": fields,
                "temporal": temporal,
                "period": period,
            }
        )
    return canonical_json_bytes({"events": result})
