"""Financial precision, contested contextual binding, bounds and rejection isolation."""

from dataclasses import replace

import pytest
from nlp_helpers import CATALOG, DATASET, artifact, item, model, proposal, source

from eie.core.errors import PersistenceError, ValidationError
from eie.expectations.units import Quantity
from eie.nlp.engine import TextEngine
from eie.nlp.extraction import extract, proposed_span
from eie.nlp.models import NumericFieldProposal, PeriodProposal, TemporalFieldProposal
from eie.nlp.normalization import NormalizationSettings, TextLimits
from eie.nlp.queries import TextQueries
from eie.nlp.repository import TextRepository
from eie.nlp.schema import (
    SourceAction,
    UnscheduledFamily,
    VerificationReason,
    VerificationState,
)
from eie.nlp.verification import verify
from eie.persistence.sqlite import SQLiteEventStore


@pytest.mark.parametrize(
    "literal,value",
    [
        ("-$2 billion", -2),
        ("$-2 billion", -2),
        ("+$2 billion", 2),
        ("$9,007,199,254,740,993", 9007199254740993),
    ],
)
def test_signs_and_large_integer_precision(literal, value):
    m = artifact("CIK 0000320193 announced buyback " + literal).prepared.numerics[0]
    assert m.quantity.value == value


@pytest.mark.parametrize("literal", ["$1.0000000000000001", "$-+2B", "-$-2B", "$" + "9" * 65 + "B"])
def test_inexact_or_malformed_number_is_not_fabricated(literal):
    assert not artifact(literal).prepared.numerics


@pytest.mark.parametrize(
    "text", ["between $1 and $1.2B", "between 7% and 5%", "between $1B and €2B"]
)
def test_ambiguous_ranges_never_verify_as_single_point(text):
    a = artifact("CIK 0000320193 reported revenue " + text + " for Q3 2026 ended 2026-09-30")
    assert a.prepared.numerics and all(m.ambiguous for m in a.prepared.numerics)
    c = a.candidates[0]
    m = a.prepared.numerics[0]
    p = replace(
        c.proposal, numerics=(NumericFieldProposal(m.field, m.quantity, proposed_span(m.span)),)
    )
    v, _ = verify(a.prepared, replace(c, proposal=p))
    assert (
        VerificationReason.CONTEXT_AMBIGUOUS in v.reasons
        and v.state is not VerificationState.VERIFIED
    )


def test_a_prior_number_cannot_be_substituted_for_current_value():
    a = artifact(
        "CIK 0000320193 reported current revenue $2B versus prior revenue $3B "
        "for Q3 2026 ended 2026-09-30"
    )
    c = a.candidates[0]
    wrong = next(m for m in a.prepared.numerics if m.quantity.value == 3)
    p = replace(
        c.proposal,
        numerics=(NumericFieldProposal(wrong.field, wrong.quantity, proposed_span(wrong.span)),),
    )
    v, _ = verify(a.prepared, replace(c, proposal=p))
    assert VerificationReason.CONTEXT_AMBIGUOUS in v.reasons


def test_metric_definitions_remain_distinct_and_source_bound(tmp_path):
    with SQLiteEventStore(tmp_path / "definitions.sqlite") as store:
        repo = TextRepository(store)
        engine = TextEngine(repo)
        p = source()
        ids = []
        for i, definition in enumerate(("gross margin", "operating margin", "adjusted EPS")):
            unit = "20%" if "margin" in definition else "$1.25 per share"
            d, b = item(
                p,
                f"CIK 0000320193 reported {definition} {unit} for Q3 2026 ended 2026-09-30",
                100 + i * 100,
                item_id=f"definition-{i}",
            )
            run = engine.ingest(p, d, b, CATALOG, 150 + i * 100)
            ids.extend(
                i
                for link in run.accepted
                for i in link.claim_ids
                if repo.evidence.claim(i).measurement is not None
            )
        assert len(ids) == len(set(ids)) == 3
        assert {repo.evidence.claim(i).measurement.measurement_id.code for i in ids} == {
            "EARNINGS.MARGIN.GROSS",
            "EARNINGS.MARGIN.OPERATING",
            "EARNINGS.EPS.ADJUSTED",
        }


def test_reference_period_date_cannot_become_effective_time():
    a = artifact()
    c = next(c for c in a.candidates if c.proposal.family is UnscheduledFamily.EARNINGS_RESULT)
    m = next(t for t in a.prepared.temporal if t.value.date == "2026-09-30")
    p = replace(c.proposal, temporal=TemporalFieldProposal(m.value, proposed_span(m.span)))
    v, _ = verify(a.prepared, replace(c, proposal=p))
    assert VerificationReason.CONTEXT_AMBIGUOUS in v.reasons


@pytest.mark.parametrize(
    "limit,value", [("mentions", 4), ("candidates", 1), ("spans", 1), ("accepted_claims", 1)]
)
def test_ingestion_bounds_rollback_complete_item(tmp_path, limit, value):
    p = source()
    p = replace(
        p,
        settings=replace(
            p.settings,
            normalization=NormalizationSettings(limits=replace(TextLimits(), **{limit: value})),
        ),
    )
    d, b = item(p)
    with SQLiteEventStore(tmp_path / (limit + ".sqlite")) as store:
        with pytest.raises(ValidationError):
            TextEngine(TextRepository(store)).ingest(p, d, b, CATALOG, 200)
        assert store._rows("SELECT COUNT(*) FROM te_runs") == [(0,)]
        assert store._rows("SELECT COUNT(*) FROM ce_receipts") == [(0,)]


def test_combined_candidate_and_model_output_bounds():
    p = source()
    d, b = item(p, "CIK 0000320193 announced a buyback $2B")
    valid = proposal(b.decode(), UnscheduledFamily.BUYBACK)
    settings = replace(
        p.settings, normalization=NormalizationSettings(limits=replace(TextLimits(), candidates=1))
    )
    with pytest.raises(ValidationError):
        extract(d, b, CATALOG, 200, settings=settings, model=model((valid,)))
    tiny = replace(
        settings,
        normalization=NormalizationSettings(limits=replace(TextLimits(), model_output_bytes=10)),
    )
    outcome = extract(d, b, CATALOG, 200, settings=tiny, model=model((valid,)))
    assert outcome.run.output_hash is None and outcome.run.failures


def test_incomplete_period_and_multiple_event_contexts_are_preserved():
    a = artifact("CIK 0000320193 CEO Jane Smith resigned; CEO John Doe appointed")
    assert len(a.candidates) == 2
    assert all(v.state is VerificationState.VERIFIED for v in a.verifications)
    a = artifact(
        "CIK 0000320193 reported revenue $2B for Q3 2026 ended 2026-09-30 "
        "and Q4 2026 ending 2026-12-31"
    )
    c = a.candidates[0]
    period = a.prepared.periods[0]
    p = replace(c.proposal, period=PeriodProposal(period.period, proposed_span(period.span)))
    assert (
        VerificationReason.CONTEXT_AMBIGUOUS
        in verify(a.prepared, replace(c, proposal=p))[0].reasons
    )


def test_invalid_withdrawal_and_cross_thread_predecessor_roll_back(tmp_path):
    with SQLiteEventStore(tmp_path / "withdrawal.sqlite") as store:
        repo = TextRepository(store)
        engine = TextEngine(repo)
        p = source()
        d, b = item(p)
        run = engine.ingest(p, d, b, CATALOG, 200)
        bad, payload = item(
            p,
            "A changed article with no withdrawal statement.",
            300,
            version="2",
            action=SourceAction.RETRACTED,
            previous=d,
        )
        with pytest.raises(ValidationError):
            engine.ingest(p, bad, payload, CATALOG, 400)
        other, body = item(
            p,
            "Provider retracted an article.",
            300,
            item_id="another",
            version="2",
            action=SourceAction.RETRACTED,
            previous=d,
        )
        with pytest.raises(ValidationError):
            engine.ingest(p, other, body, CATALOG, 400)
        assert TextQueries(repo, DATASET.dataset_id).extractions_as_of(400) == (run,)
        with pytest.raises(PersistenceError):
            repo.artifact("missing")


def test_integer_precision_hallucination_is_rejected():
    a = artifact(
        "CIK 0000320193 reported revenue $9,007,199,254,740,993 for Q3 2026 ended 2026-09-30"
    )
    c = a.candidates[0]
    n = c.proposal.numerics[0]
    p = replace(
        c.proposal,
        numerics=(replace(n, quantity=Quantity(float(n.quantity.value), n.quantity.unit)),),
    )
    assert (
        VerificationReason.NUMERIC_MISMATCH in verify(a.prepared, replace(c, proposal=p))[0].reasons
    )
