"""Offline SEC identity, source schema, and listing history contracts."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from eie.core.errors import PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.time import datetime_to_ns
from eie.entities.models import Security, security_id
from eie.entities.protocols import ResolutionState
from eie.entities.sec import (
    SecEntityRegistry,
    SecIssuerRevision,
    SecListingRevision,
    SecTickerMapRevision,
    sec_issuer_id,
    sec_listing_id,
)
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.sec.models import (
    CIK,
    Accession,
    SecForm,
    SecResourceKind,
    SecTickerMap,
)
from eie.providers.sec.parser import (
    acceptance_ns,
    parse_submissions,
    parse_ticker_map,
    validate_sec_url,
)
from eie.providers.sec.settings import SecSettings

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
URL = "https://data.sec.gov/submissions/CIK0000123456.json"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def ns(value: str) -> int:
    return datetime_to_ns(datetime.fromisoformat(value))


@pytest.mark.parametrize("value", [123456, "123456", "0000123456"])
def test_cik_normalizes_numeric_identity(value: object) -> None:
    cik = CIK.parse(value)
    assert cik.digits == "0000123456"
    assert cik.number == 123456
    assert cik.fingerprint == CIK.parse("0000123456").fingerprint


@pytest.mark.parametrize(
    "value", [0, "0", "", "0000000000", " 123", "12x", "12345678901", True, 1.5, None]
)
def test_cik_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValidationError):
        CIK.parse(value)


def test_accession_canonicalizes_and_rejects_malformed() -> None:
    one = Accession.parse("0000123456-26-000001")
    assert one == Accession.parse("000012345626000001")
    assert one.compact == "000012345626000001"
    for bad in ("123456-26-1", "0000123456-2X-000001", " 0000123456-26-000001", 1):
        with pytest.raises(ValidationError):
            Accession.parse(bad)


@pytest.mark.parametrize(
    "raw,base,amended,code",
    [
        ("8-K", "8-K", False, "SEC.8_K"),
        ("10-Q/A", "10-Q", True, "SEC.10_Q"),
        ("10-K", "10-K", False, "SEC.10_K"),
        ("6-K", "6-K", False, "SEC.6_K"),
        ("20-F", "20-F", False, "SEC.20_F"),
        ("S-1/A", "S-1", True, "SEC.S_1"),
        ("424B3", "424B3", False, "SEC.424B"),
        ("SC 13D/A", "13D", True, "SEC.13D"),
        ("SC 13G", "13G", False, "SEC.13G"),
        ("4", "4", False, "SEC.FORM_4"),
    ],
)
def test_sec_form_taxonomy(raw: str, base: str, amended: bool, code: str) -> None:
    form = SecForm.parse(raw)
    assert (form.base, form.is_amendment, form.event_code) == (base, amended, code)
    with pytest.raises(ValidationError):
        replace(form, base="8-K") if form.base != "8-K" else replace(form, is_amendment=True)


@pytest.mark.parametrize("raw", ["", "bad", "10-Q/B", "10-Q ", "SC 14D", "424C", 10])
def test_sec_form_rejects_unsupported(raw: object) -> None:
    with pytest.raises(ValidationError):
        SecForm.parse(raw)


def test_native_submissions_recent_history_and_ticker_map_parse() -> None:
    parsed = parse_submissions(fixture("submissions.json"), URL)
    assert parsed.cik == CIK.parse(123456)
    assert parsed.company_name == "Alpha Holdings Inc."
    assert parsed.aliases == ("Alpha Corporation",)
    assert parsed.ticker_exchange == (("OLD", "NYSE"),)
    assert parsed.filings[0].report_period == "2025-12-31"
    assert parsed.filings[0].accepted_at_ns == ns("2026-01-10T14:03:12-05:00")
    assert parsed.filings[0].filing_date == "2026-01-10"
    assert parsed.filings[0].primary_document == "alpha10q.htm"
    assert parsed.history_files[0].source_reference.endswith("CIK0000123456-submissions-001.json")
    old = parse_submissions(
        fixture("history.json"),
        parsed.history_files[0].source_reference,
        expected_cik=parsed.cik,
        kind=SecResourceKind.HISTORY,
    )
    assert old.company_name is None
    assert old.filings[0].form.base == "10-K"
    mapping = parse_ticker_map(fixture("ticker_map.json"))
    assert mapping.rows[0][:3] == (parsed.cik, "Alpha Holdings Inc.", "OLD")
    assert mapping == parse_ticker_map(fixture("ticker_map.json"))


def test_sec_schema_drift_fails_closed_and_extra_fields_are_harmless() -> None:
    original = json.loads(fixture("submissions.json"))
    extra = json.loads(fixture("submissions.json"))
    extra["unrelatedFutureField"] = {"safe": True}
    assert (
        parse_submissions(json.dumps(extra).encode(), URL).filings
        == parse_submissions(fixture("submissions.json"), URL).filings
    )
    for mutate in (
        lambda value: value.pop("cik"),
        lambda value: value["filings"].pop("recent"),
        lambda value: value["filings"]["recent"].pop("accessionNumber"),
        lambda value: value["filings"]["recent"]["form"].append("10-Q"),
        lambda value: value["filings"]["recent"]["accessionNumber"].__setitem__(0, "bad"),
        lambda value: value["filings"]["recent"]["acceptanceDateTime"].__setitem__(0, "bad"),
        lambda value: value["filings"]["recent"]["primaryDocument"].__setitem__(0, "../bad"),
        lambda value: value.update({"tickers": ["A", "B"]}),
    ):
        data = json.loads(json.dumps(original))
        mutate(data)
        with pytest.raises((ValidationError, ProviderSchemaMismatchError)):
            parse_submissions(json.dumps(data).encode(), URL)
    with pytest.raises(ProviderParseError):
        parse_submissions(b"{bad", URL)
    with pytest.raises(ProviderSchemaMismatchError):
        parse_submissions(b"[]", URL)
    with pytest.raises(ProviderSchemaMismatchError):
        parse_ticker_map(b"{}")
    with pytest.raises(ProviderSchemaMismatchError):
        parse_submissions(
            fixture("submissions.json"), "https://data.sec.gov/submissions/CIK0000654321.json"
        )


def test_sec_timestamp_eastern_dst_and_url_policy() -> None:
    assert acceptance_ns("2026-07-01T14:03:12") == ns("2026-07-01T14:03:12-04:00")
    assert acceptance_ns("2026-01-10T19:03:12Z") == ns("2026-01-10T14:03:12-05:00")
    for value in ("2026-03-08T02:30:00", "2026-11-01T01:30:00", "2026-01-10", "bad"):
        with pytest.raises(ValidationError):
            acceptance_ns(value)
    validate_sec_url(URL)
    for value in (
        "http://data.sec.gov/x",
        "https://evil.example/x",
        "https://u:p@data.sec.gov/x",
        "https://data.sec.gov/x?token=abc",
    ):
        with pytest.raises(ValidationError):
            validate_sec_url(value)


def test_historical_listing_security_separation_reuse_and_ambiguity() -> None:
    registry = SecEntityRegistry()
    cik_a, cik_b = CIK.parse(123456), CIK.parse(654321)
    t1, t2, t3 = 10, 20, 30
    a, b = sec_issuer_id(cik_a), sec_issuer_id(cik_b)
    registry.add_issuer(SecIssuerRevision(a, cik_a, "Alpha", (), t1, "obs_a"))
    registry.add_issuer(SecIssuerRevision(b, cik_b, "Beta", (), t1, "obs_b"))
    assert registry.issuer_by_cik(cik_a, t1 - 1) is None
    assert registry.issuer_by_cik(cik_a, t1).issuer_id == a
    one, two = security_id("FIGI", "A1"), security_id("FIGI", "A2")
    registry.add_security(Security(one, a, "Class A"))
    registry.add_security(Security(two, a, "Class B"))
    assert len(registry.securities_by_issuer(a)) == 2
    old_id = sec_listing_id(one, "OLD", "NYSE")
    old = SecListingRevision(old_id, a, one, "OLD", "NYSE", t1, None, t1, "obs_a")
    registry.add_listing(old)
    registry.add_listing(
        SecListingRevision(
            sec_listing_id(two, "ALT", "NYSE"), a, two, "ALT", "NYSE", t1, None, t1, "obs_a"
        )
    )
    assert registry.symbols_for_issuer(a, t1, t1) == (("ALT", "NYSE"), ("OLD", "NYSE"))
    assert registry.resolve_symbol("OLD", "NYSE", t1).candidate_ids == (a,)
    registry.add_listing(
        replace(old, valid_to_ns=t2, available_from_ns=t2, source_observation_id="obs_close")
    )
    registry.add_listing(
        SecListingRevision(
            sec_listing_id(one, "NEW", "NYSE"), a, one, "NEW", "NYSE", t2, None, t2, "obs_new"
        )
    )
    assert registry.resolve_symbol("OLD", "NYSE", t1).candidate_ids == (a,)
    assert registry.resolve_symbol("OLD", "NYSE", t2).state is ResolutionState.UNRESOLVED
    assert registry.resolve_symbol("OLD", "NYSE", t3, valid_at_ns=t1).candidate_ids == (a,)
    beta_sec = security_id("FIGI", "B1")
    registry.add_security(Security(beta_sec, b, "Common"))
    registry.add_listing(
        SecListingRevision(
            sec_listing_id(beta_sec, "OLD", "NYSE"),
            b,
            beta_sec,
            "OLD",
            "NYSE",
            t2,
            None,
            t2,
            "obs_beta",
        )
    )
    assert registry.resolve_symbol("OLD", "NYSE", t3).candidate_ids == (b,)
    registry.add_listing(
        SecListingRevision(
            sec_listing_id(two, "OLD", "NYSE"), a, two, "OLD", "NYSE", t3, None, t3, "obs_overlap"
        )
    )
    assert registry.resolve_symbol("OLD", "NYSE", t3).state is ResolutionState.AMBIGUOUS
    assert registry.canonical_state_bytes() == registry.canonical_state_bytes()
    with pytest.raises(PersistenceError):
        registry.add_security(Security(one, b, "Wrong issuer"))
    with pytest.raises(TemporalConsistencyError):
        registry.add_listing(replace(old, valid_to_ns=t3, available_from_ns=t1))


def test_ticker_map_history_and_issuer_name_revision() -> None:
    registry = SecEntityRegistry()
    cik = CIK.parse(123456)
    issuer = sec_issuer_id(cik)
    registry.add_issuer(SecIssuerRevision(issuer, cik, "Old Name", (), 10, "obs_old"))
    registry.add_issuer(SecIssuerRevision(issuer, cik, "New Name", ("Old Name",), 20, "obs_new"))
    assert registry.issuer_by_cik(cik, 15).legal_name == "Old Name"
    assert registry.issuer_by_cik(cik, 20).legal_name == "New Name"
    old = SecTickerMap(((cik, "Old Name", "OLD", "NYSE"),), "a" * 64)
    new = SecTickerMap(((cik, "New Name", "NEW", "NYSE"),), "b" * 64)
    registry.add_ticker_map(SecTickerMapRevision(old, 10, 10, ("obs_old",)))
    registry.add_ticker_map(SecTickerMapRevision(new, 20, 20, ("obs_new",)))
    assert registry.resolve_symbol("OLD", "NYSE", 15).candidate_ids == (issuer,)
    assert registry.resolve_symbol("NEW", "NYSE", 15).state is ResolutionState.UNRESOLVED
    assert registry.resolve_symbol("NEW", "NYSE", 25).candidate_ids == (issuer,)
    assert registry.resolve_symbol("OLD", "NYSE", 25, valid_at_ns=15).candidate_ids == (issuer,)
    assert registry.symbols_for_issuer(issuer, 25, 15) == (("OLD", "NYSE"),)
    assert not registry.add_ticker_map(SecTickerMapRevision(new, 30, 30, ("obs_new",)))
    with pytest.raises(TemporalConsistencyError):
        registry.add_ticker_map(SecTickerMapRevision(old, 19, 19, ("obs_old",)))


def test_sec_settings_validation() -> None:
    assert not SecSettings().enabled
    for change in (
        {"max_payload_bytes": 0},
        {"max_filings_per_ingest": True},
        {"staleness_ns": 0},
        {"timeout_seconds": 121},
        {"user_agent": "bad\nagent"},
    ):
        with pytest.raises(ValidationError):
            replace(SecSettings(), **change)
