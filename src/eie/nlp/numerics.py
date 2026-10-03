"""Explicit financial literals use EIE-5 units; proximity alone cannot bind a model field."""

import re
from decimal import Decimal, InvalidOperation

from eie.core.errors import ValidationError
from eie.expectations.units import Magnitude, Quantity, Unit, UnitKind
from eie.nlp.mentions import NumericMention
from eie.nlp.normalization import NormalizedText
from eie.nlp.schema import FieldName, MetricQualifier, NumericBound
from eie.nlp.segmentation import TextSegment
from eie.nlp.spans import make_span

_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_LITERAL = re.compile(
    r"(?<![\w.,])(?P<prefix>at least\s+|more than\s+|at most\s+|less than\s+|"
    r"up to\s+|greater than\s+|no less than\s+|no more than\s+|not less than\s+|not more than\s+)?"
    r"(?P<open>\()?(?P<leading_sign>[+\-])?(?P<currency>USD\s*|EUR\s*|GBP\s*|[$€£])?"
    r"(?P<sign>[+\-])?(?P<number>" + _NUMBER + r")\s*(?P<scale>thousand|million|billion|[KMB])?"
    r"(?P<unit>\s*(?:%|percent|basis points|bps|per[- ]share|/share))?(?P<close>\))?(?![\w,]|\.\d)",
    re.I,
)
_METRIC = re.compile(
    r"\b(revenue|EPS|earnings per share|margin|buyback|dividend|consideration|fine|"
    r"contract value|costs?|net income|operating income|profit|authorization|amount)\b",
    re.I,
)


def _field(text: str, start: int, end: int, unit: Unit) -> FieldName:
    labels = [
        (abs(m.end() - start), m.group().lower())
        for m in _METRIC.finditer(text[:start])
        if start - m.end() <= 100 and not re.search(r"[.;]", text[m.end() : start])
    ]
    if not labels:
        labels = [
            (m.start() + end - start, m.group().lower())
            for m in _METRIC.finditer(text[end : end + 30])
        ]
    name = min(labels)[1] if labels else "amount"
    # A later guidance clause cannot relabel an earlier reported actual value.
    cues = tuple(
        re.finditer(r"\b(reported|reports|actual|guidance|expects|forecast)\b", text[:start], re.I)
    )
    guidance = bool(cues) and cues[-1].group().lower() in ("guidance", "expects", "forecast")
    if name == "revenue" and unit.kind is UnitKind.CURRENCY:
        return FieldName.GUIDANCE_REVENUE if guidance else FieldName.REVENUE
    if name in ("eps", "earnings per share") and unit.kind is UnitKind.CURRENCY_PER_SHARE:
        return FieldName.GUIDANCE_EPS if guidance else FieldName.EPS
    if name == "margin" and unit.kind is UnitKind.PERCENT:
        return FieldName.MARGIN
    return FieldName.AMOUNT


def extract_numerics(
    original: str,
    normalized: NormalizedText,
    segments: tuple[TextSegment, ...],
    currency: str | None = None,
) -> tuple[NumericMention, ...]:
    result: list[NumericMention] = []
    for seg in segments:
        text = normalized.text[seg.start : seg.end]
        values: list[tuple[re.Match[str], Quantity, NumericBound]] = []
        for m in _LITERAL.finditer(text):
            raw = m.group("number")
            if len(raw) > 64 or bool(m.group("open")) != bool(m.group("close")):
                continue
            # Dates, times, CIKs and unlabelled integers are not financial measurements.
            if not (m.group("currency") or m.group("scale") or m.group("unit")):
                continue
            try:
                decimal = Decimal(raw.replace(",", ""))
                value = int(decimal) if decimal == decimal.to_integral_value() else float(decimal)
                if isinstance(value, float) and Decimal(str(value)) != decimal:
                    continue  # A rounded proposal must not verify an absent exact source value.
                if m.group("leading_sign") and m.group("sign"):
                    continue
                if m.group("sign") == "-" or m.group("leading_sign") == "-" or m.group("open"):
                    value = -value
                suffix = (m.group("unit") or "").strip().lower()
                cur = (m.group("currency") or "").strip()
                code = {"€": "EUR", "£": "GBP", "$": currency}.get(cur, cur.upper() or None)
                if cur and code is None:
                    continue
                kind = (
                    UnitKind.CURRENCY_PER_SHARE
                    if suffix in ("per-share", "per share", "/share")
                    else UnitKind.CURRENCY
                    if cur
                    else UnitKind.BASIS_POINTS
                    if suffix in ("basis points", "bps")
                    else UnitKind.PERCENT
                    if suffix in ("%", "percent")
                    else UnitKind.COUNT
                )
                scale = (m.group("scale") or "").lower()
                magnitude = {
                    "k": Magnitude.THOUSAND,
                    "thousand": Magnitude.THOUSAND,
                    "m": Magnitude.MILLION,
                    "million": Magnitude.MILLION,
                    "b": Magnitude.BILLION,
                    "billion": Magnitude.BILLION,
                }.get(scale, Magnitude.ONE)
                quantity = Quantity(value, Unit(kind, magnitude, code))
            except (ValidationError, InvalidOperation, ValueError, OverflowError):
                continue
            prefix = (m.group("prefix") or "").strip().lower()
            bound = {
                "at least": NumericBound.AT_LEAST,
                "more than": NumericBound.MORE_THAN,
                "at most": NumericBound.AT_MOST,
                "less than": NumericBound.LESS_THAN,
                "up to": NumericBound.AT_MOST,
                "greater than": NumericBound.MORE_THAN,
                "no less than": NumericBound.AT_LEAST,
                "not less than": NumericBound.AT_LEAST,
                "no more than": NumericBound.AT_MOST,
                "not more than": NumericBound.AT_MOST,
            }.get(prefix, NumericBound.POINT)
            values.append((m, quantity, bound))
        i = 0
        ambiguous_indices: set[int] = set()
        while i < len(values):
            m, quantity, bound = values[i]
            ambiguous = i in ambiguous_indices
            start, end, upper = m.start(), m.end(), None
            if i + 1 < len(values) and bound is NumericBound.POINT:
                n, other, next_bound = values[i + 1]
                between = text[m.end() : n.start()].strip()
                if (
                    between in ("–", "-", "to", "and")
                    and next_bound is NumericBound.POINT
                    and (between != "and" or re.search(r"between\s*$", text[: m.start()], re.I))
                ):
                    if (
                        quantity.unit.kind is other.unit.kind
                        and quantity.unit.currency == other.unit.currency
                        and not (
                            (quantity.unit.magnitude is Magnitude.ONE)
                            != (other.unit.magnitude is Magnitude.ONE)
                        )
                        and quantity.canonical_value <= other.canonical_value
                    ):
                        end, upper, bound = n.end(), other, NumericBound.CLOSED_RANGE
                        i += 1
                    else:
                        ambiguous_indices.update((i, i + 1))
                        ambiguous = True
            result.append(
                NumericMention(
                    make_span(original, normalized, seg, seg.start + start, seg.start + end),
                    quantity,
                    upper,
                    bound,
                    _field(text, start, end, quantity.unit),
                    _qualifiers(text, start),
                    ambiguous,
                )
            )
            i += 1
    if len(result) > normalized.mapping.settings.limits.mentions:
        raise ValidationError("numeric mention bound exceeded")
    return tuple(sorted(result, key=lambda r: r.mention_id))


def _qualifiers(text: str, start: int) -> tuple[MetricQualifier, ...]:
    labels = tuple(_METRIC.finditer(text[:start]))
    if not labels or start - labels[-1].end() > 100:
        return ()
    prefix = text[max(0, labels[-1].start() - 30) : labels[-1].start()]
    # Only the contiguous adjective sequence modifying this metric is authoritative.
    match = re.search(
        r"((?:(?:non[- ]GAAP|GAAP|adjusted|basic|diluted|gross|operating)\s+)+)$", prefix, re.I
    )
    if match is None:
        return ()
    return tuple(
        sorted(
            {
                MetricQualifier(m.group().upper().replace("-", "_").replace(" ", "_"))
                for m in re.finditer(
                    r"non[- ]GAAP|GAAP|adjusted|basic|diluted|gross|operating", match.group(), re.I
                )
            }
        )
    )
