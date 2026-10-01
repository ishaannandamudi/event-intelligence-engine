"""Fail-closed SEC models, parser drift, filing index, and transaction edges."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from eie.config.settings import Settings
from eie.core.errors import (
    PersistenceError,
    SchemaCompatibilityError,
    TemporalConsistencyError,
    ValidationError,
)
from eie.core.quality import QualityFlag
from eie.entities.models import Security, security_id
from eie.entities.sec import (
    SecEntityRegistry,
    SecFilerRevision,
    SecIssuerRevision,
    SecListingRevision,
    SecTickerMapRevision,
    sec_filer_id,
    sec_issuer_id,
    sec_listing_id,
)
from eie.filings.models import (
    AmendmentStatus,
    FilingQueryResult,
    FilingView,
    SymbolFilingQueryResult,
)
from eie.filings.store import InMemoryFilingStore
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.sec.adapter import SecIngestor
from eie.providers.sec.models import (
    CIK,
    SecResourceKind,
    SecTickerMap,
    filing_id_for,
    iso_date,
)
from eie.providers.sec.parser import parse_submissions, parse_ticker_map
from eie.providers.sec.settings import SecSettings

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
URL = "https://data.sec.gov/submissions/CIK0000123456.json"
CIK_A = CIK.parse(123456)
CIK_B = CIK.parse(654321)
T1, T2, T3 = 10, 20, 30


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def base_filing():
    engine = SecIngestor(Settings(sec=SecSettings(enabled=True)))
    real_t = 1_768_071_792_000_000_000
    result = engine.ingest_payload(fixture("submissions.json"), URL, real_t, real_t)
    assert result.filings_created_or_updated == 1
    return engine.filings_for_cik(CIK_A, real_t).filings[0].metadata


@pytest.mark.parametrize(
    "change",
    [
        {"filing_id": "fil_wrong"},
        {"issuer_id": "wrong"},
        {"filer_id": "wrong"},
        {"form": "10-Q"},
        {"filing_date": "2026-99-01"},
        {"report_period": "2025-99-01"},
        {"primary_document": "../escape"},
        {"file_number": ""},
        {"source_reference": "http://data.sec.gov/unsafe"},
        {"accepted_at_ns": 2**63 - 1},
        {"revision_number": 0},
        {"revision_number": True},
        {"source_observation_id": "bad"},
        {"amendment_status": "LINKED"},
        {"amendment_status": AmendmentStatus.LINKED},
        {"amendment_status": AmendmentStatus.AMBIGUOUS},
        {"amends_filing_id": "fil_other"},
        {"amendment_candidates": ("fil_other",)},
        {"schema_version": "2.0"},
    ],
)
def test_filing_metadata_rejects_inconsistent_fields(change: dict[str, object]) -> None:
    base = base_filing()
    with pytest.raises((ValidationError, SchemaCompatibilityError)):
        replace(base, **change)


def test_filing_query_and_fingerprint_contracts() -> None:
    metadata = base_filing()
    view = FilingView(metadata, (("OLD", "NYSE"),))
    query = FilingQueryResult(metadata.available_from_ns, (view,), "config")
    assert query.result_fingerprint == replace(query).result_fingerprint
    assert view.result_fingerprint == replace(view).result_fingerprint
    assert query.result_fingerprint != replace(query, filings=()).result_fingerprint
    with pytest.raises(ValidationError):
        FilingQueryResult(metadata.available_from_ns - 1, (view,), "config")
    from eie.entities.protocols import ResolutionResult, ResolutionState

    symbol = SymbolFilingQueryResult(
        metadata.accepted_at_ns,
        ResolutionResult(ResolutionState.RESOLVED, (metadata.issuer_id,), "exact"),
        query,
    )
    assert symbol.result_fingerprint == replace(symbol).result_fingerprint


def test_filing_store_rejects_conflicts_and_filters_exactly() -> None:
    base = base_filing()
    store = InMemoryFilingStore()
    assert store.get(base.filing_id, base.available_from_ns) is None
    assert store.by_accession(base.accession, base.available_from_ns) is None
    assert store.append(base)
    assert not store.append(base)
    with pytest.raises(PersistenceError):
        store.append(replace(base, primary_document="changed.htm"))
    with pytest.raises(PersistenceError):
        store.append(replace(base, revision_number=3, available_from_ns=base.available_from_ns + 2))
    with pytest.raises(TemporalConsistencyError):
        store.append(replace(base, revision_number=2))
    other = replace(
        base,
        cik=CIK_B,
        filing_id=filing_id_for(CIK_B, base.accession),
        issuer_id=sec_issuer_id(CIK_B),
        filer_id=sec_filer_id(CIK_B),
    )
    with pytest.raises(PersistenceError):
        store.append(other)
    t = base.available_from_ns
    assert store.known_as_of(t - 1) == ()
    assert store.known_as_of(t, issuer_id="wrong") == ()
    assert store.known_as_of(t, cik=CIK_B) == ()
    assert store.known_as_of(t, exact_form="10-K") == ()
    assert store.known_as_of(t, base_form="10-K") == ()
    assert store.known_as_of(t, amendment=True) == ()
    assert store.known_as_of(t, report_period="2020-01-01") == ()
    assert store.known_as_of(t, issuer_id=base.issuer_id) == (base,)
    assert store.history(base.filing_id) == (base,)
    assert store.canonical_state_bytes() == store.canonical_state_bytes()


def test_source_model_validation_and_canonical_dates() -> None:
    base = parse_submissions(fixture("submissions.json"), URL)
    item = base.filings[0]
    mapping = parse_ticker_map(fixture("ticker_map.json"))
    assert iso_date("2026-01-01", "date") == "2026-01-01"
    for value in (1, "2026-99-01", "20260101"):
        with pytest.raises(ValidationError):
            iso_date(value, "date")
    for change in (
        {"form": "10-Q"},
        {"issuer_cik": "123456"},
        {"filing_date": "bad"},
        {"report_period": "bad"},
        {"primary_document": "../escape"},
        {"file_number": ""},
        {"fragment_hash": "bad"},
        {"source_reference": "https://evil.example/x"},
    ):
        with pytest.raises(ValidationError):
            replace(item, **change)
    for change in (
        {"schema_version": "2.0"},
        {"kind": "SUBMISSIONS"},
        {"company_name": None},
        {"aliases": ("z", "a")},
        {"ticker_exchange": (("Z", "NYSE"), ("A", "NYSE"))},
        {"source_hash": "bad"},
        {"entity_type": ""},
        {"filings": (item, item)},
    ):
        with pytest.raises(ValidationError):
            replace(base, **change)
    with pytest.raises(ValidationError):
        replace(mapping, rows=(mapping.rows[0], mapping.rows[0]))
    with pytest.raises(ValidationError):
        replace(mapping, source_hash="bad")
    with pytest.raises(ValidationError):
        replace(base.history_files[0], filing_to="2023-01-01")
    with pytest.raises(ValidationError):
        replace(base.history_files[0], name="bad.json")


def test_filer_issuer_listing_validation_and_registry_conflicts() -> None:
    registry = SecEntityRegistry()
    filer = SecFilerRevision(sec_filer_id(CIK_A), CIK_A, "Alpha", (), T1, "obs_f")
    issuer = SecIssuerRevision(sec_issuer_id(CIK_A), CIK_A, "Alpha", (), T1, "obs_i")
    assert registry.add_filer(filer)
    assert not registry.add_filer(filer)
    assert registry.filer_by_cik(CIK_A, T1 - 1) is None
    assert registry.filer_by_cik(CIK_A, T1) == filer
    assert registry.add_issuer(issuer)
    assert not registry.add_issuer(issuer)
    assert registry.issuer_by_id(issuer.issuer_id, T1) == issuer
    with pytest.raises(TemporalConsistencyError):
        registry.add_filer(replace(filer, name="New", available_from_ns=T1))
    with pytest.raises(TemporalConsistencyError):
        registry.add_issuer(replace(issuer, legal_name="New", available_from_ns=T1))
    for change in (
        {"filer_id": "wrong"},
        {"name": ""},
        {"aliases": ("z", "a")},
        {"source_observation_id": "wrong"},
        {"schema_version": "2.0"},
    ):
        with pytest.raises(ValidationError):
            replace(filer, **change)
    for change in (
        {"issuer_id": "wrong"},
        {"legal_name": ""},
        {"aliases": ("z", "a")},
        {"source_observation_id": "wrong"},
        {"schema_version": "2.0"},
    ):
        with pytest.raises(ValidationError):
            replace(issuer, **change)
    sid = security_id("FIGI", "VALID")
    with pytest.raises(PersistenceError):
        registry.add_security(Security(sid, sec_issuer_id(CIK_B), "Unknown"))
    assert registry.add_security(Security(sid, issuer.issuer_id, "Common"))
    assert not registry.add_security(Security(sid, issuer.issuer_id, "Common"))
    lid = sec_listing_id(sid, "OLD", "NYSE")
    listing = SecListingRevision(lid, issuer.issuer_id, sid, "OLD", "NYSE", T1, None, T1, "obs_l")
    with pytest.raises(PersistenceError):
        registry.add_listing(
            replace(
                listing,
                security_id=security_id("FIGI", "OTHER"),
                listing_id=sec_listing_id(security_id("FIGI", "OTHER"), "OLD", "NYSE"),
            )
        )
    assert registry.add_listing(listing)
    assert not registry.add_listing(listing)
    with pytest.raises(PersistenceError):
        registry.add_listing(
            replace(listing, valid_from_ns=T2, valid_to_ns=T3, available_from_ns=T2)
        )
    with pytest.raises(PersistenceError):
        registry.add_listing(
            replace(
                listing, valid_to_ns=None, available_from_ns=T2, source_observation_id="obs_new"
            )
        )
    for change in (
        {"listing_id": "wrong"},
        {"issuer_id": "wrong"},
        {"valid_to_ns": T1},
        {"source_observation_id": "wrong"},
        {"schema_version": "2.0"},
    ):
        with pytest.raises(ValidationError):
            replace(listing, **change)
    with pytest.raises(ValidationError):
        sec_listing_id("bad", "OLD", "NYSE")
    with pytest.raises(ValidationError):
        registry.resolve_symbol("bad symbol", "NYSE", T2)


def test_sec_ticker_map_revision_validation_and_parser_edge_cases() -> None:
    mapping = parse_ticker_map(fixture("ticker_map.json"))
    base = SecTickerMapRevision(mapping, T1, T1, ("obs_m",))
    assert base.available_from_ns == T1
    with pytest.raises(TemporalConsistencyError):
        replace(base, observed_at_ns=T2)
    with pytest.raises(ValidationError):
        replace(base, source_observation_ids=("bad",))
    with pytest.raises(ProviderSchemaMismatchError):
        parse_ticker_map(b'{"fields": ["ticker"], "data": []}')
    with pytest.raises(ProviderSchemaMismatchError):
        parse_ticker_map(b'{"fields": ["cik", "name", "ticker", "exchange"], "data": [{}]}')
    with pytest.raises(ProviderParseError):
        parse_ticker_map(b"bad")
    with pytest.raises(ValidationError):
        SecTickerMap((("bad", "Name", "X", "NYSE"),), "a" * 64)


def test_submissions_parser_history_and_optional_fields_fail_closed() -> None:
    base = json.loads(fixture("submissions.json"))

    def changed(fn):
        data = json.loads(json.dumps(base))
        fn(data)
        return json.dumps(data).encode()

    for mutate in (
        lambda data: data["filings"].update({"files": {}}),
        lambda data: data["filings"]["files"].append("bad"),
        lambda data: data["filings"]["files"][0].update({"filingFrom": "bad"}),
        lambda data: data.update({"formerNames": {}}),
        lambda data: data["formerNames"].append("bad"),
        lambda data: data["formerNames"][0].update({"name": ""}),
        lambda data: data.update({"exchanges": ["NYSE", "NYSE"]}),
        lambda data: data["filings"]["recent"].update({"reportDate": ["bad"]}),
        lambda data: data["filings"]["recent"].update({"fileNumber": [" "]}),
        lambda data: data["filings"]["recent"].update({"issuerCik": ["bad"]}),
        lambda data: data["filings"]["recent"].update({"reportDate": ["x", "y"]}),
    ):
        with pytest.raises((ValidationError, ProviderSchemaMismatchError)):
            parse_submissions(changed(mutate), URL)
    history_url = "https://data.sec.gov/submissions/CIK0000123456-submissions-001.json"
    with pytest.raises(ValidationError):
        parse_submissions(fixture("history.json"), history_url, kind=SecResourceKind.HISTORY)
    with pytest.raises(ProviderSchemaMismatchError):
        parse_submissions(
            fixture("history.json"),
            "https://data.sec.gov/submissions/CIK0000654321-submissions-001.json",
            expected_cik=CIK_A,
            kind=SecResourceKind.HISTORY,
        )
    with pytest.raises(ProviderSchemaMismatchError):
        parse_submissions(fixture("submissions.json"), URL, expected_cik=CIK_B)


def test_amendment_without_report_period_is_explicitly_unresolved() -> None:
    engine = SecIngestor(Settings(sec=SecSettings(enabled=True)))
    t2 = 1_768_071_792_000_000_000
    original = json.loads(fixture("submissions.json"))
    engine.ingest_payload(json.dumps(original).encode(), URL, t2, t2)
    am = json.loads(json.dumps(original))
    am["filings"]["recent"]["accessionNumber"] = ["0000123456-26-000002"]
    am["filings"]["recent"]["acceptanceDateTime"] = ["2026-01-11T14:03:12"]
    am["filings"]["recent"]["form"] = ["10-Q/A"]
    am["filings"]["recent"]["reportDate"] = [""]
    t3 = t2 + 86_400_000_000_000
    engine.ingest_payload(json.dumps(am).encode(), URL, t3, t3)
    filing = engine.filings_for_cik(CIK_A, t3, amendment=True).filings[0].metadata
    assert filing.amendment_status is AmendmentStatus.UNRESOLVED
    assert filing.amends_filing_id is None
    assert QualityFlag.ENTITY_UNRESOLVED not in filing.quality_flags
