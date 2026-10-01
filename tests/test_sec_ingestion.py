"""Offline SEC EDGAR ingestion, amendment, replay, health, and atomicity tests."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from eie.config.settings import Settings
from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.quality import QualityFlag
from eie.core.serialization import canonical_json_bytes
from eie.core.time import datetime_to_ns
from eie.entities.models import Security, security_id
from eie.entities.protocols import ResolutionState
from eie.entities.sec import SecListingRevision, sec_issuer_id, sec_listing_id
from eie.events.lifecycle import LifecycleState
from eie.events.revisions import RevisionKind
from eie.events.scope import ScopeKind
from eie.filings.models import AmendmentStatus
from eie.providers.common.errors import ProviderRateLimitedError, ProviderUnavailableError
from eie.providers.sec.adapter import (
    SecAdapter,
    SecCapability,
    SecFailureCategory,
    SecIngestor,
)
from eie.providers.sec.models import CIK, Accession, SecResourceKind
from eie.providers.sec.settings import SecSettings
from eie.sources.models import SourceHealthState
from eie.sources.trust import SourceClass

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
URL = "https://data.sec.gov/submissions/CIK0000123456.json"
HISTORY_URL = "https://data.sec.gov/submissions/CIK0000123456-submissions-001.json"
CIK_A = CIK.parse(123456)
T1 = datetime_to_ns(datetime.fromisoformat("2026-01-09T12:00:00-05:00"))
T2 = datetime_to_ns(datetime.fromisoformat("2026-01-10T14:03:12-05:00"))
T3 = datetime_to_ns(datetime.fromisoformat("2026-01-11T12:00:00-05:00"))
T4 = datetime_to_ns(datetime.fromisoformat("2026-01-12T14:00:00-05:00"))
T5 = datetime_to_ns(datetime.fromisoformat("2026-01-13T12:00:00-05:00"))


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def service(*, bootstrap: bool = True, user_agent: str | None = None) -> SecIngestor:
    return SecIngestor(
        Settings(
            sec=SecSettings(
                enabled=True,
                allow_issuer_bootstrap=bootstrap,
                user_agent=user_agent,
            )
        )
    )


def original() -> dict[str, object]:
    return json.loads(fixture("submissions.json"))


def amend_payload(
    *, accession: str = "0000123456-26-000002", accepted: str = "2026-01-12T14:00:00"
) -> bytes:
    data = original()
    recent = data["filings"]["recent"]
    recent["accessionNumber"] = [accession]
    recent["filingDate"] = ["2026-01-12"]
    recent["acceptanceDateTime"] = [accepted]
    recent["form"] = ["10-Q/A"]
    recent["reportDate"] = ["2025-12-31"]
    recent["primaryDocument"] = ["alpha10qa.htm"]
    return encoded(data)


def ticker_map(*rows: tuple[int, str, str, str]) -> bytes:
    return encoded({"fields": ["cik", "name", "ticker", "exchange"], "data": rows})


def test_sec_capabilities_and_raw_first_canonical_normalization() -> None:
    adapter = SecAdapter()
    assert {
        SecCapability.ISSUER_METADATA,
        SecCapability.HISTORICAL_FILINGS,
    } <= adapter.capabilities()
    assert adapter.parse(fixture("submissions.json"), URL).filings[0].form.base == "10-Q"
    engine = service()
    engine.ingest_ticker_map(fixture("ticker_map.json"), T1, T1)
    diagnostic = engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    assert (
        diagnostic.items_seen,
        diagnostic.raw_created,
        diagnostic.filings_created_or_updated,
    ) == (1, 2, 1)
    filing = engine.filings_for_cik(CIK_A, T2).filings[0]
    metadata = filing.metadata
    assert metadata.cik == CIK_A
    assert metadata.issuer_id == sec_issuer_id(CIK_A)
    assert metadata.form.raw == "10-Q"
    assert metadata.accepted_at_ns == T2
    assert metadata.report_period == "2025-12-31"
    assert metadata.document_url.endswith("/000012345626000001/alpha10q.htm")
    assert filing.historical_symbols == (("OLD", "NYSE"),)
    raw = engine.event_store.get_observation(metadata.source_observation_id)
    assert raw is not None
    assert raw.source.name == "SEC_EDGAR"
    assert raw.source.source_class is SourceClass.OFFICIAL_PRIMARY
    assert raw.raw_event_type == "10-Q"
    assert raw.raw_entity_identifiers == (CIK_A.digits,)
    assert raw.published_at_ns is None
    canonical = engine.event_store.known_as_of(T2)[0]
    assert canonical.lifecycle is LifecycleState.RELEASED
    assert canonical.identity.scope.identifiers == (sec_issuer_id(CIK_A),)
    assert canonical.identity.event_type.code == "SEC.10_Q"
    assert canonical.effective_at_ns == T2
    assert canonical.revision.available_from_ns == T2
    assert canonical.evidence_observation_ids == (metadata.source_observation_id,)
    assert engine.filing_store.by_accession(Accession.parse("0000123456-26-000001"), T2) == metadata
    assert engine.history_files_as_of(T2)[0].name == "CIK0000123456-submissions-001.json"
    assert engine.history_files_as_of(T1) == ()


def test_acceptance_boundary_observation_delay_and_history_file() -> None:
    engine = service()
    assert engine.filings_for_cik(CIK_A, T2 - 1).filings == ()
    observed = T2 + 60_000_000_000
    ingested = observed + 1
    engine.ingest_payload(fixture("submissions.json"), URL, observed, ingested)
    assert engine.filings_for_cik(CIK_A, T2).filings == ()
    assert engine.filings_for_cik(CIK_A, ingested - 1).filings == ()
    assert engine.filings_for_cik(CIK_A, ingested).filings[0].metadata.accepted_at_ns == T2
    assert engine.event_store.known_as_of(ingested - 1) == ()
    historical = engine.ingest_payload(
        fixture("history.json"),
        HISTORY_URL,
        T3,
        T3,
        expected_cik=CIK_A,
        kind=SecResourceKind.HISTORY,
    )
    assert historical.filings_created_or_updated == 1
    assert {view.metadata.form.base for view in engine.filings_for_cik(CIK_A, T3).filings} == {
        "10-Q",
        "10-K",
    }
    old = engine.filing_store.by_accession(Accession.parse("0000123456-24-000001"), T3)
    assert old is not None and old.accepted_at_ns < T1
    assert engine.filing_store.by_accession(Accession.parse("0000123456-24-000001"), T2) is None


def test_ticker_change_amendment_correction_and_historical_replay() -> None:
    engine = service()
    engine.ingest_ticker_map(fixture("ticker_map.json"), T1, T1)
    engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    original_view = engine.filings_for_cik(CIK_A, T2)
    original_bytes = canonical_json_bytes(original_view)
    original_id = original_view.filings[0].metadata.filing_id
    new_map = ticker_map(
        (123456, "Alpha Holdings Inc.", "NEW", "NYSE"),
        (654321, "Beta Corporation", "OLD", "NYSE"),
    )
    engine.ingest_ticker_map(new_map, T3, T3)
    assert engine.registry.resolve_symbol("NEW", "NYSE", T2).state is ResolutionState.UNRESOLVED
    assert engine.registry.resolve_symbol("NEW", "NYSE", T3).candidate_ids == (
        sec_issuer_id(CIK_A),
    )
    assert engine.registry.resolve_symbol("OLD", "NYSE", T3).candidate_ids == (
        sec_issuer_id(CIK.parse(654321)),
    )
    assert engine.filings_for_cik(CIK_A, T3).filings[0].historical_symbols == (("OLD", "NYSE"),)
    amendment = engine.ingest_payload(amend_payload(), URL, T4, T4)
    assert amendment.amendments_linked == 1
    all_filings = engine.filings_for_issuer(sec_issuer_id(CIK_A), T4).filings
    assert len(all_filings) == 2
    amended = all_filings[-1]
    assert amended.metadata.form.is_amendment
    assert amended.metadata.amends_filing_id == original_id
    assert amended.metadata.amendment_status is AmendmentStatus.LINKED
    assert amended.historical_symbols == (("NEW", "NYSE"),)
    assert (
        engine.recent_filings(T4, limit=1).filings[0].metadata.filing_id
        == amended.metadata.filing_id
    )
    with pytest.raises(ValidationError):
        engine.recent_filings(T4, limit=0)
    assert engine.filing_store.amendments_for(original_id, T3) == ()
    assert len(engine.filing_store.amendments_for(original_id, T4)) == 1
    correction = original()
    correction["filings"]["recent"]["primaryDocument"] = ["alpha10q-corrected.htm"]
    corrected = engine.ingest_payload(encoded(correction), URL, T5, T5)
    assert corrected.filings_created_or_updated == 1
    final_original = engine.filing_store.get(original_id, T5)
    assert final_original is not None and final_original.revision_number == 2
    assert QualityFlag.CORRECTED in final_original.quality_flags
    assert final_original.primary_document == "alpha10q-corrected.htm"
    assert engine.filing_store.get(original_id, T4).primary_document == "alpha10q.htm"
    assert (
        engine.event_store.get_revisions(engine.event_store.known_as_of(T2)[0].event_id)[
            -1
        ].revision.kind
        is RevisionKind.CORRECTION
    )
    assert canonical_json_bytes(engine.filings_for_cik(CIK_A, T2)) == original_bytes
    assert (
        engine.filings_for_symbol("NEW", "NYSE", T4, T4)
        .query.filings[-1]
        .metadata.form.is_amendment
    )
    assert engine.filings_for_symbol("OLD", "NYSE", T4, T4).query.filings == ()
    assert (
        engine.filings_for_symbol("OLD", "NYSE", T4, T2).query.filings[0].metadata.issuer_id
        == original_view.filings[0].metadata.issuer_id
    )


def test_later_learned_verified_listing_refines_only_later_knowledge_view() -> None:
    engine = service()
    engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    before = canonical_json_bytes(engine.filings_for_cik(CIK_A, T2))
    assert engine.filings_for_cik(CIK_A, T2).filings[0].historical_symbols == ()
    issuer = sec_issuer_id(CIK_A)
    sid = security_id("FIGI", "ALPHA-COMMON")
    engine.registry.add_security(Security(sid, issuer, "Alpha common"))
    engine.registry.add_listing(
        SecListingRevision(
            sec_listing_id(sid, "OLD", "NYSE"),
            issuer,
            sid,
            "OLD",
            "NYSE",
            T1,
            T3,
            T3,
            "obs_verified_listing",
        )
    )
    assert canonical_json_bytes(engine.filings_for_cik(CIK_A, T2)) == before
    assert engine.filings_for_cik(CIK_A, T3).filings[0].historical_symbols == (("OLD", "NYSE"),)


def test_idempotent_reingestion_and_changed_payload_same_filing_identity() -> None:
    engine = service()
    first = engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    event_bytes = engine.canonical_state_bytes()
    second = engine.ingest_payload(fixture("submissions.json"), URL, T3, T3)
    assert first.filings_created_or_updated == 1
    assert (
        second.raw_created,
        second.filings_created_or_updated,
        second.duplicates_suppressed,
    ) == (0, 0, 1)
    assert engine.canonical_state_bytes() == event_bytes
    changed = original()
    changed["filings"]["recent"]["fileNumber"] = ["001-00002"]
    third = engine.ingest_payload(encoded(changed), URL, T4, T4)
    assert (third.raw_created, third.filings_created_or_updated) == (1, 1)
    before = engine.filings_for_cik(CIK_A, T3).filings[0].metadata
    after = engine.filings_for_cik(CIK_A, T4).filings[0].metadata
    assert before.filing_id == after.filing_id
    assert before.file_number == "001-00001"
    assert after.file_number == "001-00002"
    assert len(engine.filing_store.history(after.filing_id)) == 2
    assert (
        len(engine.event_store.get_revisions(engine.event_store.known_as_of(T4)[0].event_id)) == 2
    )


def test_two_amendments_link_original_and_ambiguous_target_stays_unlinked() -> None:
    engine = service()
    engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    first = engine.ingest_payload(amend_payload(), URL, T4, T4)
    second = engine.ingest_payload(
        amend_payload(accession="0000123456-26-000003", accepted="2026-01-13T12:00:00"),
        URL,
        T5,
        T5,
    )
    assert first.amendments_linked == second.amendments_linked == 1
    original_id = engine.filings_for_cik(CIK_A, T2).filings[0].metadata.filing_id
    amendments = engine.filing_store.amendments_for(original_id, T5)
    assert len(amendments) == 2
    assert len({item.filing_id for item in amendments}) == 2
    assert engine.filing_store.get(original_id, T5).form.raw == "10-Q"
    ambiguous = service()
    first_two = original()
    columns = first_two["filings"]["recent"]
    for values in columns.values():
        values.append(values[0])
    columns["accessionNumber"][1] = "0000123456-26-000004"
    columns["acceptanceDateTime"][1] = "2026-01-10T15:00:00"
    ambiguous.ingest_payload(encoded(first_two), URL, T3, T3)
    ambiguous.ingest_payload(amend_payload(), URL, T4, T4)
    target = ambiguous.filings_for_cik(CIK_A, T4, amendment=True).filings[0].metadata
    assert target.amendment_status is AmendmentStatus.AMBIGUOUS
    assert target.amends_filing_id is None
    assert len(target.amendment_candidates) == 2


def test_unresolved_issuer_retains_raw_and_degraded_health() -> None:
    engine = service(bootstrap=False)
    diagnostic = engine.ingest_payload(
        fixture("history.json"),
        HISTORY_URL,
        T3,
        T3,
        expected_cik=CIK_A,
        kind=SecResourceKind.HISTORY,
    )
    assert diagnostic.unresolved_issuers == 1
    assert diagnostic.health.state is SourceHealthState.DEGRADED
    assert diagnostic.error_category is SecFailureCategory.UNRESOLVED_ISSUER
    assert engine.filings_for_cik(CIK_A, T3).filings == ()
    assert engine.event_store.known_as_of(T3) == ()
    assert (
        engine.event_store.get_observation(next(iter(engine.event_store._observations))) is not None
    )


def test_mid_batch_failure_rolls_back_raw_issuer_filing_event_and_history() -> None:
    engine = service()
    data = original()
    columns = data["filings"]["recent"]
    for values in columns.values():
        values.append(values[0])
    columns["accessionNumber"][1] = "0000123456-26-000002"
    columns["acceptanceDateTime"][1] = "2026-01-20T14:03:12"
    baseline = engine.canonical_state_bytes()
    failure = engine.ingest_payload(encoded(data), URL, T2, T2)
    assert failure.error_category is SecFailureCategory.NORMALIZATION_ERROR
    assert engine.canonical_state_bytes() == baseline
    assert engine.filing_store.known_as_of(T2) == ()
    assert engine.registry.issuer_by_cik(CIK_A, T2) is None
    assert engine.history_files_as_of(T2) == ()
    assert engine.event_store.known_as_of(T2) == ()


def test_source_health_no_filings_vs_failures_staleness_and_payload_limit() -> None:
    engine = service()
    empty = original()
    empty["filings"]["recent"] = {key: [] for key in empty["filings"]["recent"]}
    healthy = engine.ingest_payload(encoded(empty), URL, T1, T1)
    assert healthy.items_seen == 0
    assert healthy.health.state is SourceHealthState.HEALTHY
    assert engine.health_as_of(T1 - 1).state is SourceHealthState.UNKNOWN
    failure = engine.ingest_payload(b"{bad", URL, T2, T2)
    assert failure.health.state is SourceHealthState.DEGRADED
    assert failure.error_category is SecFailureCategory.PARSE_ERROR
    schema = engine.ingest_payload(b"{}", URL, T3, T3)
    assert schema.health.state is SourceHealthState.SCHEMA_MISMATCH
    assert schema.health.parse_failures == 2
    unavailable = engine.report_failure(T4, SecFailureCategory.UNAVAILABLE)
    assert unavailable.health.state is SourceHealthState.UNAVAILABLE
    rate = engine.report_failure(T5, SecFailureCategory.RATE_LIMITED)
    assert rate.health.state is SourceHealthState.RATE_LIMITED
    late = T5 + 8 * 86_400_000_000_000
    recovered = engine.ingest_payload(encoded(empty), URL, late, late)
    assert recovered.health.state is SourceHealthState.HEALTHY
    assert engine.health_as_of(late + 8 * 86_400_000_000_000).state is SourceHealthState.STALE
    limited = SecIngestor(Settings(sec=SecSettings(enabled=True, max_payload_bytes=1)))
    with pytest.raises(ValidationError):
        limited.ingest_payload(fixture("submissions.json"), URL, T2, T2)


def test_form_filters_recent_order_and_unresolved_symbol_query() -> None:
    engine = service()
    engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    engine.ingest_payload(amend_payload(), URL, T4, T4)
    issuer = sec_issuer_id(CIK_A)
    assert len(engine.filings_for_issuer(issuer, T4, base_form="10-Q").filings) == 2
    assert len(engine.filings_for_issuer(issuer, T4, exact_form="10-Q/A").filings) == 1
    assert len(engine.filings_for_issuer(issuer, T4, amendment=False).filings) == 1
    assert len(engine.filings_for_issuer(issuer, T4, report_period="2025-12-31").filings) == 2
    assert engine.filing_store.recent(T4, 1)[0].form.is_amendment
    unresolved = engine.filings_for_symbol("MISS", "NYSE", T4, T4)
    assert unresolved.resolution.state is ResolutionState.UNRESOLVED
    assert unresolved.query.filings == ()
    with pytest.raises(ValueError):
        engine.filing_store.recent(T4, 0)


def test_ambiguous_symbol_mapping_never_selects_first_issuer() -> None:
    engine = service()
    engine.ingest_ticker_map(
        ticker_map(
            (123456, "Alpha Holdings Inc.", "ABC", "NYSE"),
            (654321, "Beta Corporation", "ABC", "NYSE"),
        ),
        T1,
        T1,
    )
    engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    result = engine.filings_for_symbol("ABC", "NYSE", T2, T2)
    assert result.resolution.state is ResolutionState.AMBIGUOUS
    assert len(result.resolution.candidate_ids) == 2
    assert result.query.filings == ()


def test_transport_requires_contact_and_has_bounded_failure_states() -> None:
    class FakeTransport:
        def __init__(self, body: bytes | Exception) -> None:
            self.body = body
            self.calls: list[tuple[str, int, str]] = []

        def get(self, url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
            self.calls.append((url, int(timeout_seconds), user_agent))
            if isinstance(self.body, Exception):
                raise self.body
            return self.body

    no_contact = service()
    with pytest.raises(ValidationError):
        no_contact.fetch_and_ingest(URL, FakeTransport(fixture("submissions.json")), T2, T2)
    contact = service(user_agent="EIE research contact@example.test")
    transport = FakeTransport(fixture("submissions.json"))
    result = contact.fetch_and_ingest(URL, transport, T2, T2)
    assert result.filings_created_or_updated == 1
    assert transport.calls == [(URL, 10, "EIE research contact@example.test")]
    assert len(transport.calls) == 1
    unavailable = contact.fetch_and_ingest(
        URL, FakeTransport(ProviderUnavailableError("offline")), T3, T3
    )
    assert unavailable.health.state is SourceHealthState.UNAVAILABLE
    limited = contact.fetch_and_ingest(URL, FakeTransport(ProviderRateLimitedError("429")), T4, T4)
    assert limited.health.state is SourceHealthState.RATE_LIMITED


def test_repeated_builds_are_byte_identical_and_settings_affect_fingerprint() -> None:
    def build() -> tuple[bytes, bytes]:
        engine = service()
        engine.ingest_ticker_map(fixture("ticker_map.json"), T1, T1)
        engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
        engine.ingest_payload(amend_payload(), URL, T4, T4)
        return engine.canonical_state_bytes(), canonical_json_bytes(
            engine.filings_for_cik(CIK_A, T4)
        )

    assert build() == build()
    assert Settings(sec=SecSettings(enabled=True)).fingerprint != Settings().fingerprint
    assert (
        Settings(sec=SecSettings(enabled=True)).fingerprint
        != Settings(sec=SecSettings(enabled=True, allow_issuer_bootstrap=False)).fingerprint
    )


def test_configuration_and_time_preflight_reject_unsafe_ingestion() -> None:
    disabled = SecIngestor(Settings())
    with pytest.raises(ValidationError):
        disabled.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    engine = service()
    with pytest.raises(TemporalConsistencyError):
        engine.ingest_payload(fixture("submissions.json"), URL, T2 + 1, T2)
    engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)
    with pytest.raises(TemporalConsistencyError):
        engine.ingest_payload(fixture("submissions.json"), URL, T2, T2)


def test_form_four_filer_scope_does_not_invent_issuer_identity() -> None:
    data = original()
    data["cik"] = "0000765432"
    data["name"] = "Reporting Person"
    data["entityType"] = "individual"
    data["formerNames"] = []
    data["tickers"] = []
    data["exchanges"] = []
    data["filings"]["files"] = []
    row = data["filings"]["recent"]
    row["accessionNumber"] = ["0000765432-26-000001"]
    row["form"] = ["4"]
    row["reportDate"] = [""]
    url = "https://data.sec.gov/submissions/CIK0000765432.json"
    engine = service()
    diagnostic = engine.ingest_payload(encoded(data), url, T2, T2)
    assert diagnostic.filings_created_or_updated == 1
    assert engine.registry.filer_by_cik(CIK.parse(765432), T2) is not None
    assert engine.registry.issuer_by_cik(CIK.parse(765432), T2) is None
    filing = engine.filings_for_cik(CIK.parse(765432), T2).filings[0].metadata
    assert filing.issuer_id is None
    assert filing.filer_id.startswith("flr_")
    assert filing.form.event_code == "SEC.FORM_4"
    assert QualityFlag.ENTITY_UNRESOLVED in filing.quality_flags
    event = engine.event_store.known_as_of(T2)[0]
    assert event.identity.scope.kind is ScopeKind.SEC_FILER
    assert event.identity.scope.identifiers == (filing.filer_id,)


def test_unknown_corporate_entity_type_retains_raw_without_guessing_issuer() -> None:
    data = original()
    data.pop("entityType")
    engine = service()
    result = engine.ingest_payload(encoded(data), URL, T2, T2)
    assert result.unresolved_issuers == 1
    assert engine.registry.filer_by_cik(CIK_A, T2) is not None
    assert engine.registry.issuer_by_cik(CIK_A, T2) is None
    assert engine.event_store.known_as_of(T2) == ()
    assert engine.filings_for_cik(CIK_A, T2).filings == ()
