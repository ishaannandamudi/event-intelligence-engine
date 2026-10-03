"""Structured source facts, explicit epistemic status and conservative acceptance."""

from dataclasses import replace

import pytest
from nlp_helpers import BODY, CATALOG, ISSUER, artifact, item, model, proposal, source

from eie.core.errors import ValidationError
from eie.evidence.claims import TimePrecision, TimeValue
from eie.expectations.units import Magnitude, Quantity, Unit, UnitKind
from eie.nlp.classification import classify, event_type
from eie.nlp.entities import EntityCatalog
from eie.nlp.extraction import extract
from eie.nlp.schema import (
    EpistemicStatus,
    ExtractionFailure,
    FieldName,
    NumericBound,
    UnscheduledFamily,
    VerificationReason,
    VerificationState,
)
from eie.nlp.verification import verify

CASES = [
    (UnscheduledFamily.EARNINGS_RESULT, "reported revenue $2B for Q3 2026 ended 2026-09-30"),
    (
        UnscheduledFamily.GUIDANCE_CHANGE,
        "raised revenue guidance to between $1B and $1.2B for Q4 2026 ending 2026-12-31",
    ),
    (UnscheduledFamily.MANAGEMENT_CHANGE, "CEO Jane Smith resigned effective 2026-10-01"),
    (UnscheduledFamily.M_AND_A, "announced an acquisition"),
    (UnscheduledFamily.CAPITAL_RAISE, "announced an equity offering"),
    (UnscheduledFamily.BUYBACK, "authorized a buyback of $500 million"),
    (UnscheduledFamily.DIVIDEND, "declared a dividend of $0.25 per share"),
    (UnscheduledFamily.LEGAL_REGULATORY, "disclosed an SEC investigation"),
    (UnscheduledFamily.CYBERSECURITY, "disclosed a cyberattack; systems offline"),
    (UnscheduledFamily.PRODUCT, "announced a product launch"),
    (UnscheduledFamily.CONTRACT, "was awarded a contract worth $2B"),
    (UnscheduledFamily.SUPPLY_CHAIN, "disclosed a supplier interruption"),
    (UnscheduledFamily.OPERATIONAL_DISRUPTION, "disclosed a factory shutdown"),
    (UnscheduledFamily.RECALL, "announced a recall"),
    (UnscheduledFamily.BANKRUPTCY_RESTRUCTURING, "filed for bankruptcy"),
    (UnscheduledFamily.ACTIVIST, "disclosed a proxy contest"),
    (UnscheduledFamily.OWNERSHIP, "purchased a stake"),
    (UnscheduledFamily.RATING_ACTION, "disclosed a credit rating downgrade"),
    (UnscheduledFamily.FDA_REGULATORY_DECISION, "reported FDA approved a treatment"),
    (UnscheduledFamily.OTHER, "made an unscheduled announcement"),
]


@pytest.mark.parametrize("family,text", CASES)
def test_all_families_source_verified_and_aligned(family, text):
    result = artifact("CIK 0000320193 " + text)
    candidate = next(c for c in result.candidates if c.proposal.family is family)
    verification = next(v for v in result.verifications if v.candidate_id == candidate.candidate_id)
    assert verification.state is VerificationState.VERIFIED
    assert candidate.proposal.entity_ids == (ISSUER,)
    assert event_type(family).family
    assert family in classify(text)


@pytest.mark.parametrize(
    "literal,value,kind,magnitude,bound,upper",
    [
        ("$5.2 billion", 5.2, UnitKind.CURRENCY, Magnitude.BILLION, NumericBound.POINT, None),
        (
            "EUR 1,234.5 million",
            1234.5,
            UnitKind.CURRENCY,
            Magnitude.MILLION,
            NumericBound.POINT,
            None,
        ),
        ("£2 thousand", 2, UnitKind.CURRENCY, Magnitude.THOUSAND, NumericBound.POINT, None),
        ("5%–7%", 5, UnitKind.PERCENT, Magnitude.ONE, NumericBound.CLOSED_RANGE, 7),
        (
            "between $1B and $1.2B",
            1,
            UnitKind.CURRENCY,
            Magnitude.BILLION,
            NumericBound.CLOSED_RANGE,
            1.2,
        ),
        ("at least 10%", 10, UnitKind.PERCENT, Magnitude.ONE, NumericBound.AT_LEAST, None),
        ("more than 5%", 5, UnitKind.PERCENT, Magnitude.ONE, NumericBound.MORE_THAN, None),
        ("at most 15 bps", 15, UnitKind.BASIS_POINTS, Magnitude.ONE, NumericBound.AT_MOST, None),
        (
            "less than 20 basis points",
            20,
            UnitKind.BASIS_POINTS,
            Magnitude.ONE,
            NumericBound.LESS_THAN,
            None,
        ),
        ("($2 million)", -2, UnitKind.CURRENCY, Magnitude.MILLION, NumericBound.POINT, None),
        (
            "$1.25 per share",
            1.25,
            UnitKind.CURRENCY_PER_SHARE,
            Magnitude.ONE,
            NumericBound.POINT,
            None,
        ),
        ("2 million", 2, UnitKind.COUNT, Magnitude.MILLION, NumericBound.POINT, None),
    ],
)
def test_numeric_literals(literal, value, kind, magnitude, bound, upper):
    result = artifact("CIK 0000320193 announced buyback " + literal)
    assert len(result.prepared.numerics) == 1
    m = result.prepared.numerics[0]
    assert (
        m.quantity.value == value
        and m.quantity.unit.kind is kind
        and m.quantity.unit.magnitude is magnitude
    )
    assert m.bound is bound and (None if m.upper is None else m.upper.value) == upper


@pytest.mark.parametrize(
    "literal", ["$12,34 million", "$5x", "2026-09-30", "$1e20", "$NaN", "($2B", "CIK 0000320193"]
)
def test_malformed_or_nonfinancial_numbers(literal):
    assert not artifact(literal).prepared.numerics


def test_no_currency_invention_or_missing_period_upgrade():
    p = source()
    p = replace(p, currency=None)
    assert not artifact("revenue $2B", provider=p).prepared.numerics
    result = artifact("CIK 0000320193 reported revenue $2B")
    assert result.verifications[0].state is VerificationState.PARTIALLY_VERIFIED
    assert VerificationReason.REFERENCE_PERIOD_MISMATCH in result.verifications[0].reasons


@pytest.mark.parametrize(
    "text,status",
    [
        ("CEO did not resign", EpistemicStatus.NEGATED),
        ("denied that the CEO resigned", EpistemicStatus.DENIED),
        ("sources say company may acquire a target", EpistemicStatus.RUMORED),
        ("plans to acquire a target", EpistemicStatus.PLANNED),
        ("may acquire a target if approved", EpistemicStatus.CONDITIONAL),
        ("expects an acquisition", EpistemicStatus.EXPECTED),
        ('CEO allegedly "resigned"', EpistemicStatus.ALLEGED),
    ],
)
def test_epistemic_status_cannot_become_occurrence(text, status):
    a = artifact("CIK 0000320193 " + text)
    assert a.candidates and all(c.proposal.epistemic is status for c in a.candidates)
    assert all(v.state is VerificationState.PARTIALLY_VERIFIED for v in a.verifications)
    c = a.candidates[0]
    v, _ = verify(
        a.prepared, replace(c, proposal=replace(c.proposal, epistemic=EpistemicStatus.CONFIRMED))
    )
    assert (
        v.state is VerificationState.REJECTED and VerificationReason.NEGATION_CONFLICT in v.reasons
    )


def test_multiple_source_events_and_fields():
    result = artifact()
    assert len(result.candidates) == 3
    assert len({c.proposal.family for c in result.candidates}) == 3
    assert all(v.state is VerificationState.VERIFIED for v in result.verifications)
    assert all(c.document_id == result.run.document_id for c in result.candidates)
    management = next(
        c for c in result.candidates if c.proposal.family is UnscheduledFamily.MANAGEMENT_CHANGE
    )
    assert {f.name: f.value for f in management.proposal.text_fields}[
        FieldName.PERSON
    ] == "Jane Smith"
    assert result.prepared.temporal and result.prepared.entities and result.prepared.periods


@pytest.mark.parametrize(
    "change,reason",
    [
        ("numeric", VerificationReason.NUMERIC_MISMATCH),
        ("unit", VerificationReason.UNIT_MISMATCH),
        ("field", VerificationReason.CONTEXT_AMBIGUOUS),
        ("entity", VerificationReason.ENTITY_MISMATCH),
        ("span", VerificationReason.SPAN_INVALID),
        ("period", VerificationReason.REFERENCE_PERIOD_MISMATCH),
    ],
)
def test_hallucinations_are_rejected(change, reason):
    a = artifact()
    c = next(c for c in a.candidates if c.proposal.family is UnscheduledFamily.EARNINGS_RESULT)
    p = c.proposal
    n = p.numerics[0]
    if change == "numeric":
        p = replace(p, numerics=(replace(n, quantity=replace(n.quantity, value=999)),))
    if change == "unit":
        p = replace(
            p,
            numerics=(
                replace(
                    n, quantity=Quantity(n.quantity.value, Unit(UnitKind.CURRENCY, currency="EUR"))
                ),
            ),
        )
    if change == "field":
        p = replace(p, numerics=(replace(n, name=FieldName.AMOUNT),))
    if change == "entity":
        p = replace(p, entity_ids=("iss_" + "b" * 64,))
    if change == "span":
        p = replace(p, span=replace(p.span, fragment_hash="a" * 64))
    if change == "period":
        p = replace(p, period=replace(p.period, period=replace(p.period.period, fiscal_year=2025)))
    v, _ = verify(a.prepared, replace(c, proposal=p))
    assert reason in v.reasons and v.state is not VerificationState.VERIFIED


def test_nearby_actual_guidance_and_buyback_cannot_cross_bind():
    a = artifact(
        "CIK 0000320193 reported revenue $2B and buyback $3B; revenue guidance $4B "
        "for Q4 2026 ending 2026-12-31"
    )
    assert {m.quantity.value: m.field for m in a.prepared.numerics} == {
        2: FieldName.REVENUE,
        3: FieldName.AMOUNT,
        4: FieldName.GUIDANCE_REVENUE,
    }


def test_empty_and_unsupported_language_are_explicit_failures():
    assert artifact("").run.failures == (ExtractionFailure.NO_EVENT_EXTRACTED,)
    assert artifact(BODY, language="fr").run.failures == (ExtractionFailure.UNSUPPORTED_LANGUAGE,)
    p = source()
    d, b = item(p)
    with pytest.raises(ValidationError):
        extract(d, b, EntityCatalog(), 99)
    with pytest.raises(ValidationError):
        extract(d, b + b"changed", CATALOG, 200)
    noentity = extract(d, b, EntityCatalog(), 200)
    assert all(v.state is VerificationState.UNVERIFIED for v in noentity.verifications)


def test_precision_hallucination():
    a = artifact()
    c = next(c for c in a.candidates if c.proposal.family is UnscheduledFamily.MANAGEMENT_CHANGE)
    t = c.proposal.temporal
    assert t.value.precision is TimePrecision.DATE_ONLY
    from eie.providers.common.models import parse_aware_ns

    fake = replace(
        t,
        value=TimeValue(
            TimePrecision.EXACT, "2026-10-01", parse_aware_ns("2026-10-01T09:00:00Z", "fake")
        ),
    )
    v, _ = verify(a.prepared, replace(c, proposal=replace(c.proposal, temporal=fake)))
    assert (
        VerificationReason.TIME_UNSUPPORTED in v.reasons and v.state is VerificationState.REJECTED
    )


def test_fake_model_is_proposal_only():
    p = source()
    d, b = item(p)
    base = proposal()
    fake = replace(
        base,
        key="fake",
        numerics=(
            replace(base.numerics[0], quantity=replace(base.numerics[0].quantity, value=999)),
        ),
    )
    result = extract(d, b, CATALOG, 200, model=model((base, fake)))
    assert (
        len(result.candidates) == 5
        and ExtractionFailure.VERIFICATION_FAILURE in result.run.failures
    )
    assert sum(v.state is VerificationState.REJECTED for v in result.verifications) == 1
