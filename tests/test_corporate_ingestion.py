"""Offline corporate ingestion, provenance, action, identity, and replay scenarios."""

import json
from pathlib import Path

import pytest

from eie.config.settings import Settings
from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.quality import ConfirmationState, QualityFlag
from eie.core.serialization import canonical_json_bytes
from eie.corporate.models import (
    CorporateKind,
    CorporateStatus,
    Deal,
    Dividend,
    EarningsCall,
    EarningsSchedule,
    ReleaseTiming,
    SchedulePrecision,
    Split,
)
from eie.corporate.settings import CorporateSettings
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
from eie.events.revisions import RevisionKind
from eie.providers.corporate.adapter import (
    CorporateAdapter,
    CorporateFailureCategory,
    CorporateIngestor,
)
from eie.providers.sec.models import CIK, SecTickerMap
from eie.sources.models import SourceHealthState
from eie.sources.trust import DEFAULT_PRECEDENCE, SourceClass, SourcePrecedencePolicy

FIXTURES = Path(__file__).parent / "fixtures" / "corporate"
CIK_A = CIK.parse(123456)
ISSUER = sec_issuer_id(CIK_A)
SID = security_id("FIGI", "ALPHA-COMMON")
SID_B = security_id("FIGI", "ALPHA-PREFERRED")
T0 = 1_788_000_000_000_000_000
T1 = 1_788_264_000_000_000_000
T2 = 1_788_350_400_000_000_000
T3 = 1_788_436_800_000_000_000
T4 = T3 + 86_400_000_000_000
T5 = T4 + 86_400_000_000_000
EFFECTIVE = 1_793_491_200_000_000_000


def registry(*, listings: bool = False, ticker_snapshot: bool = False) -> SecEntityRegistry:
    result = SecEntityRegistry()
    result.add_issuer(SecIssuerRevision(ISSUER, CIK_A, "Alpha", (), T0, "obs_issuer"))
    result.add_security(Security(SID, ISSUER, "Common"))
    result.add_security(Security(SID_B, ISSUER, "Preferred"))
    if listings:
        result.add_listing(
            SecListingRevision(
                sec_listing_id(SID, "OLD", "NYSE"),
                ISSUER,
                SID,
                "OLD",
                "NYSE",
                T0,
                None,
                T0,
                "obs_listing",
            )
        )
    if ticker_snapshot:
        mapping = SecTickerMap(((CIK_A, "Alpha", "OLD", "NYSE"),), "a" * 64)
        result.add_ticker_map(SecTickerMapRevision(mapping, T0, T0, ("obs_map",)))
    return result


def engine(
    *, listings: bool = False, ticker_snapshot: bool = False, settings: Settings | None = None
) -> CorporateIngestor:
    return CorporateIngestor(
        settings or Settings(corporate=CorporateSettings(enabled=True)),
        registry(listings=listings, ticker_snapshot=ticker_snapshot),
    )


def adapter(name: str, source_class: SourceClass = SourceClass.LICENSED_NEWS) -> CorporateAdapter:
    return CorporateAdapter(name, source_class, frozenset(CorporateKind))


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


def item(
    kind: CorporateKind,
    data: dict[str, object],
    *,
    key: str = "stable-action-key",
    status: CorporateStatus = CorporateStatus.ANNOUNCED,
    event_id: str = "source-event-1",
    issuer: str = ISSUER,
    security: str | None = None,
    announced: int | None = T1,
    effective: int | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "kind": kind.value,
        "provider_event_id": event_id,
        "issuer_id": issuer,
        "semantic_key": key,
        "status": status.value,
        "announced_at_ns": announced,
        "effective_at_ns": effective,
        "data": data,
    }
    if security is not None:
        event["security_id"] = security
    return event


def payload(
    events: list[dict[str, object]],
    *,
    provider: str = "SYNTH_A",
    item_id: str = "announcement-1",
    published: int = T1,
    coverage: dict[str, object] | None = None,
) -> bytes:
    result: dict[str, object] = {
        "schema": "EIE_CORPORATE_EXTRACT_V1",
        "provider_id": provider,
        "source_version": "synthetic-1",
        "items": [
            {
                "item_id": item_id,
                "item_version": "1",
                "published_at_ns": published,
                "source_reference": f"https://{provider.lower()}.example/announcement/{item_id}",
                "events": events,
            }
        ],
    }
    if coverage is not None:
        result["coverage"] = coverage
    return json.dumps(result, sort_keys=True).encode()


def test_earnings_three_provider_conflict_exact_time_call_and_historical_replay() -> None:
    service = engine()
    a = adapter("SYNTH_A")
    b = adapter("SYNTH_B")
    company = adapter("SYNTH_COMPANY", SourceClass.COMPANY_PRIMARY)
    assert service.index.upcoming_earnings(T1 - 1).events == ()
    first = service.ingest_payload(a, fixture("provider_a"), T1, T1)
    assert (first.items_seen, first.raw_created, first.events_created_or_updated) == (1, 1, 1)
    prior = service.index.upcoming_earnings(T1)
    assert len(prior.events) == 1
    first_view = prior.events[0]
    assert isinstance(first_view.corporate.selected_claim.payload, EarningsSchedule)
    assert first_view.corporate.selected_claim.payload.scheduled_date == "2026-10-20"
    assert first_view.corporate.selected_claim.payload.scheduled_for_ns is None
    assert first_view.corporate.selected_claim.payload.release_timing is ReleaseTiming.AFTER_MARKET
    assert (
        first_view.corporate.selected_claim.payload.precision is SchedulePrecision.DATE_PLUS_SESSION
    )
    prior_bytes = canonical_json_bytes(prior)
    second = service.ingest_payload(b, fixture("provider_b"), T2, T2)
    assert second.conflicts == 1
    conflict = service.index.upcoming_earnings(T2).events[0].corporate
    assert conflict.corporate_id == first_view.corporate.corporate_id
    assert len(conflict.claims) == 2
    assert len({claim.observation_id for claim in conflict.claims}) == 2
    assert conflict.confirmation is ConfirmationState.DISPUTED
    assert QualityFlag.CONFLICTING_SOURCES in conflict.quality_flags
    assert "EQUAL_RANK_DISAGREEMENT" in conflict.conflict_reason_codes
    assert conflict.selected_claim.payload.scheduled_date == "2026-10-20"
    assert canonical_json_bytes(service.index.upcoming_earnings(T1)) == prior_bytes
    third = service.ingest_payload(company, fixture("company"), T3, T3)
    assert (third.raw_created, third.events_created_or_updated) == (1, 2)
    latest = service.index.upcoming_earnings(T3).events[0]
    assert latest.corporate.selected_claim.provider_id == "SYNTH_COMPANY"
    assert latest.corporate.selected_claim.payload.scheduled_for_ns == 1_792_585_800_000_000_000
    assert len(latest.corporate.claims) == 3
    assert latest.corporate.revision.number == 3
    call = service.index.known_as_of(T3, kind=CorporateKind.EARNINGS_CALL).events[0]
    assert isinstance(call.corporate.selected_claim.payload, EarningsCall)
    assert (
        call.corporate.selected_claim.payload.scheduled_for_ns != latest.canonical.scheduled_for_ns
    )
    assert call.corporate.corporate_id != latest.corporate.corporate_id
    assert (
        call.corporate.selected_claim.observation_id
        == latest.corporate.selected_claim.observation_id
    )
    assert call.canonical.evidence_observation_ids == (
        call.corporate.selected_claim.observation_id,
    )
    assert service.index.next_earnings(T3, issuer_id=ISSUER) == latest
    assert len(service.index.known_as_of(T3).coverage) == 3
    assert canonical_json_bytes(service.index.upcoming_earnings(T1)) == prior_bytes
    assert service.index.canonical_state_bytes() == service.index.canonical_state_bytes()


def test_exact_earnings_and_call_reschedule_cancellation_and_corrections() -> None:
    service = engine()
    company = adapter("SYNTH_COMPANY", SourceClass.COMPANY_PRIMARY)
    service.ingest_payload(company, fixture("company"), T3, T3)
    base = json.loads(fixture("company"))
    before = canonical_json_bytes(service.index.upcoming_earnings(T3))
    base["items"][0]["events"][0]["data"]["scheduled_for_ns"] += 3_600_000_000_000
    base["items"][0]["events"][1]["data"]["scheduled_for_ns"] += 3_600_000_000_000
    base["items"][0]["events"][1]["data"]["webcast_url"] = (
        "https://synthetic-company.example/ir/new-webcast"
    )
    service.ingest_payload(company, json.dumps(base).encode(), T4, T4)
    earnings = service.index.upcoming_earnings(T4).events[0]
    call = service.index.known_as_of(T4, kind=CorporateKind.EARNINGS_CALL).events[0]
    assert earnings.corporate.revision.kind is RevisionKind.RESCHEDULE
    assert call.corporate.revision.kind is RevisionKind.RESCHEDULE
    assert (
        service.index.known_as_of(T3, kind=CorporateKind.EARNINGS_CALL)
        .events[0]
        .corporate.selected_claim.payload.webcast_url.endswith("webcast")
    )
    assert canonical_json_bytes(service.index.upcoming_earnings(T3)) == before
    base["items"][0]["events"][0]["status"] = "CANCELLED"
    base["items"][0]["events"][1]["status"] = "CANCELLED"
    service.ingest_payload(company, json.dumps(base).encode(), T5, T5)
    assert service.index.upcoming_earnings(T5).events == ()
    assert (
        service.index.known_as_of(T5, kind=CorporateKind.EARNINGS_SCHEDULE)
        .events[0]
        .corporate.revision.kind
        is RevisionKind.CANCELLATION
    )
    assert canonical_json_bytes(service.index.upcoming_earnings(T3)) == before


def test_dividend_dates_amount_correction_and_special_dividend() -> None:
    service = engine()
    source = adapter("SYNTH_A")
    first = payload([item(CorporateKind.DIVIDEND, {})])
    service.ingest_payload(source, first, T1, T1)
    early = service.index.known_as_of(T1, kind=CorporateKind.DIVIDEND)
    early_bytes = canonical_json_bytes(early)
    assert isinstance(early.events[0].corporate.selected_claim.payload, Dividend)
    assert early.events[0].corporate.selected_claim.payload.ex_date is None
    data = {
        "record_date": "2026-10-10",
        "ex_date": "2026-10-09",
        "payment_date": "2026-11-01",
        "amount": {"amount_micros": 250000, "currency": "USD", "per_share": True},
    }
    service.ingest_payload(source, payload([item(CorporateKind.DIVIDEND, data)]), T2, T2)
    assert len(service.index.effective_dates_between("2026-10-09", "2026-10-10", T2).events) == 1
    assert len(service.index.announced_between(T1, T1 + 1, T2).events) == 1
    assert service.index.effective_between(T1, T1 + 1, T2).events == ()
    data["amount"]["amount_micros"] = 300000
    service.ingest_payload(source, payload([item(CorporateKind.DIVIDEND, data)]), T3, T3)
    latest = service.index.known_as_of(T3, kind=CorporateKind.DIVIDEND).events[0]
    assert latest.corporate.revision.kind is RevisionKind.CORRECTION
    assert latest.corporate.selected_claim.payload.amount.amount_micros == 300000
    assert QualityFlag.CORRECTED in latest.corporate.quality_flags
    assert (
        canonical_json_bytes(service.index.known_as_of(T1, kind=CorporateKind.DIVIDEND))
        == early_bytes
    )
    special = item(CorporateKind.SPECIAL_DIVIDEND, data, key="special-2026", event_id="special")
    service.ingest_payload(source, payload([special], item_id="special"), T4, T4)
    assert len(service.index.known_as_of(T4, kind=CorporateKind.SPECIAL_DIVIDEND).events) == 1


@pytest.mark.parametrize(
    "kind,data",
    [
        (
            CorporateKind.STOCK_SPLIT,
            {
                "numerator": 4,
                "denominator": 1,
                "effective_date": "2026-11-01",
                "record_date": "2026-10-20",
                "trading_adjustment_date": "2026-11-02",
            },
        ),
        (
            CorporateKind.REVERSE_SPLIT,
            {"numerator": 1, "denominator": 10, "effective_date": "2026-11-01"},
        ),
        (
            CorporateKind.BUYBACK,
            {
                "authorization": {"amount_micros": 1000000000000, "currency": "USD"},
                "shares": 5000000,
                "percent_basis_points": 500,
                "expiration_date": "2027-01-01",
                "expanded": True,
            },
        ),
        (
            CorporateKind.TENDER_OFFER,
            {
                "price": {"amount_micros": 50000000, "currency": "USD", "per_share": True},
                "expiration_at_ns": EFFECTIVE,
                "tender_type": "issuer",
            },
        ),
        (
            CorporateKind.MERGER,
            {
                "target_issuer_id": ISSUER,
                "acquirer_issuer_id": None,
                "unresolved_party_name": "Unknown Acquirer",
                "expected_close_date": "2026-11-01",
            },
        ),
        (
            CorporateKind.ACQUISITION,
            {
                "target_issuer_id": ISSUER,
                "acquirer_issuer_id": None,
                "unresolved_party_name": "Unknown Acquirer",
            },
        ),
        (
            CorporateKind.SPIN_OFF,
            {
                "spun_issuer_id": None,
                "record_date": "2026-10-20",
                "distribution_date": "2026-11-01",
                "ratio_numerator": 1,
                "ratio_denominator": 2,
            },
        ),
        (
            CorporateKind.DELISTING,
            {
                "security_id": SID,
                "exchange": "NYSE",
                "expected_date": "2026-11-01",
                "effective_date": "2026-11-02",
                "reason": "voluntary",
            },
        ),
        (
            CorporateKind.CEO_DEPARTURE,
            {
                "person_name": "Ada Example",
                "role": "CEO",
                "effective_date": "2026-11-01",
                "source_person_id": "person-1",
            },
        ),
        (
            CorporateKind.CEO_APPOINTMENT,
            {"person_name": "Ada Example", "role": "CEO", "effective_date": "2026-11-01"},
        ),
        (CorporateKind.CFO_DEPARTURE, {"person_name": "Ada Example", "role": "CFO"}),
        (CorporateKind.CFO_APPOINTMENT, {"person_name": "Ada Example", "role": "CFO"}),
        (CorporateKind.BOARD_CHANGE, {"person_name": "Ada Example", "role": "Director"}),
        (CorporateKind.AUDITOR_CHANGE, {"person_name": "Ada Example", "role": "Auditor"}),
    ],
)
def test_all_structured_action_and_governance_variants(
    kind: CorporateKind, data: dict[str, object]
) -> None:
    service = engine()
    result = service.ingest_payload(
        adapter("SYNTH_A"),
        payload([item(kind, data, security=SID if kind is CorporateKind.DELISTING else None)]),
        T1,
        T1,
    )
    assert result.error_category is None
    view = service.index.known_as_of(T1, kind=kind).events[0]
    assert view.corporate.identity.issuer_id == ISSUER
    if kind in (CorporateKind.MERGER, CorporateKind.ACQUISITION, CorporateKind.SPIN_OFF):
        assert QualityFlag.ENTITY_UNRESOLVED in view.corporate.quality_flags
    if kind in (CorporateKind.STOCK_SPLIT, CorporateKind.REVERSE_SPLIT):
        assert isinstance(view.corporate.selected_claim.payload, Split)
    assert service.index.known_as_of(T1 - 1).events == ()


def test_deal_status_history_and_termination_are_causal() -> None:
    service = engine()
    source = adapter("SYNTH_A")
    deal = {
        "target_issuer_id": ISSUER,
        "acquirer_issuer_id": None,
        "unresolved_party_name": "Acquirer",
        "expected_close_date": "2026-11-01",
    }
    service.ingest_payload(source, payload([item(CorporateKind.MERGER, deal)]), T1, T1)
    original = canonical_json_bytes(service.index.known_as_of(T1))
    service.ingest_payload(
        source, payload([item(CorporateKind.MERGER, deal, status=CorporateStatus.APPROVED)]), T2, T2
    )
    deal["actual_close_date"] = "2026-11-03"
    service.ingest_payload(
        source, payload([item(CorporateKind.MERGER, deal, status=CorporateStatus.CLOSED)]), T3, T3
    )
    view = service.index.known_as_of(T3).events[0]
    assert isinstance(view.corporate.selected_claim.payload, Deal)
    assert view.corporate.selected_claim.status is CorporateStatus.CLOSED
    assert view.corporate.revision.number == 3
    assert (
        service.index.known_as_of(T2).events[0].corporate.selected_claim.status
        is CorporateStatus.APPROVED
    )
    assert canonical_json_bytes(service.index.known_as_of(T1)) == original
    deal["actual_close_date"] = "2026-11-04"
    service.ingest_payload(
        source, payload([item(CorporateKind.MERGER, deal, status=CorporateStatus.CLOSED)]), T4, T4
    )
    assert (
        service.index.known_as_of(T4).events[0].corporate.revision.kind is RevisionKind.CORRECTION
    )
    tender = item(
        CorporateKind.TENDER_OFFER,
        {},
        key="tender-1",
        event_id="tender",
        status=CorporateStatus.WITHDRAWN,
    )
    service.ingest_payload(source, payload([tender], item_id="withdrawn"), T5, T5)
    assert (
        service.index.known_as_of(T5, kind=CorporateKind.TENDER_OFFER)
        .events[0]
        .corporate.selected_claim.status
        is CorporateStatus.WITHDRAWN
    )


def test_symbol_change_updates_verified_listing_without_rewriting_past() -> None:
    service = engine(listings=True, ticker_snapshot=True)
    source = adapter("SYNTH_A")
    before = service.index.registry.resolve_symbol("OLD", "NYSE", T0, valid_at_ns=EFFECTIVE)
    assert before.state is ResolutionState.RESOLVED
    change = {
        "security_id": SID,
        "old_ticker": "OLD",
        "old_exchange": "NYSE",
        "new_ticker": "NEW",
        "new_exchange": "NYSE",
        "effective_at_ns": EFFECTIVE,
    }
    result = service.ingest_payload(
        source,
        payload([item(CorporateKind.SYMBOL_CHANGE, change, security=SID, effective=EFFECTIVE)]),
        T1,
        T1,
    )
    assert result.error_category is None
    assert service.index.registry.resolve_symbol(
        "OLD", "NYSE", T1, valid_at_ns=EFFECTIVE - 1
    ).candidate_ids == (ISSUER,)
    assert (
        service.index.registry.resolve_symbol("OLD", "NYSE", T1, valid_at_ns=EFFECTIVE).state
        is ResolutionState.UNRESOLVED
    )
    assert service.index.registry.resolve_symbol(
        "NEW", "NYSE", T1, valid_at_ns=EFFECTIVE
    ).candidate_ids == (ISSUER,)
    assert (
        service.index.registry.resolve_symbol("NEW", "NYSE", T0, valid_at_ns=EFFECTIVE).state
        is ResolutionState.UNRESOLVED
    )
    assert (
        service.index.events_for_symbol("NEW", "NYSE", T1, EFFECTIVE)
        .query.events[0]
        .corporate.identity.security_id
        == SID
    )
    assert service.index.events_for_symbol("OLD", "NYSE", T1, EFFECTIVE).query.events == ()
    assert service.index.events_for_security(SID_B, T1).events == ()


def test_multi_security_scope_and_corporate_action_ranges() -> None:
    service = engine(listings=True)
    source = adapter("SYNTH_A")
    issuer_dividend = item(
        CorporateKind.DIVIDEND, {"ex_date": "2026-11-01"}, key="div-1", event_id="div"
    )
    specific_split = item(
        CorporateKind.STOCK_SPLIT,
        {"numerator": 2, "denominator": 1, "effective_date": "2026-11-01"},
        key="split-1",
        event_id="split",
        security=SID,
        effective=EFFECTIVE,
    )
    service.ingest_payload(source, payload([issuer_dividend, specific_split]), T1, T1)
    assert len(service.index.events_for_security(SID, T1).events) == 2
    assert len(service.index.events_for_security(SID_B, T1).events) == 1
    assert len(service.index.announced_between(T1, T1 + 1, T1).events) == 2
    assert len(service.index.effective_between(EFFECTIVE, EFFECTIVE + 1, T1).events) == 1
    assert len(service.index.effective_dates_between("2026-11-01", "2026-11-02", T1).events) == 2
    with pytest.raises(TemporalConsistencyError):
        service.index.effective_between(T2, T1, T1)


def test_failed_batch_is_atomic_and_unresolved_entity_is_explicit() -> None:
    service = engine()
    source = adapter("SYNTH_A")
    good = item(CorporateKind.DIVIDEND, {}, key="good", event_id="good")
    bad = item(
        CorporateKind.DIVIDEND,
        {},
        key="bad",
        event_id="bad",
        issuer=sec_issuer_id(CIK.parse(654321)),
    )
    baseline = service.index.canonical_state_bytes()
    outcome = service.ingest_payload(source, payload([good, bad]), T1, T1)
    assert outcome.error_category is CorporateFailureCategory.UNRESOLVED_ENTITY
    assert service.index.canonical_state_bytes() == baseline
    assert service.index.known_as_of(T1).events == ()
    assert service.index.event_store.known_as_of(T1) == ()
    success = service.ingest_payload(source, payload([good]), T2, T2)
    assert success.events_created_or_updated == 1


def test_idempotency_schema_drift_health_staleness_and_precedence_override() -> None:
    order = (SourceClass.LICENSED_NEWS,) + tuple(
        value for value in DEFAULT_PRECEDENCE if value is not SourceClass.LICENSED_NEWS
    )
    policy = SourcePrecedencePolicy(fact_overrides=(("corporate.earnings_schedule", order),))
    service = engine(
        settings=Settings(corporate=CorporateSettings(enabled=True), source_precedence=policy)
    )
    a = adapter("SYNTH_A")
    company = adapter("SYNTH_COMPANY", SourceClass.COMPANY_PRIMARY)
    service.ingest_payload(a, fixture("provider_a"), T1, T1)
    state = service.index.canonical_state_bytes()
    duplicate = service.ingest_payload(a, fixture("provider_a"), T2, T2)
    assert duplicate.events_created_or_updated == 0
    assert service.index.canonical_state_bytes() == state
    company_result = service.ingest_payload(company, fixture("company"), T3, T3)
    assert company_result.conflicts == 1
    assert (
        service.index.upcoming_earnings(T3).events[0].corporate.selected_claim.provider_id
        == "SYNTH_A"
    )
    broken = json.loads(fixture("provider_a"))
    broken["items"][0]["events"][0].pop("kind")
    assert (
        service.ingest_payload(a, json.dumps(broken).encode(), T4, T4).health.state
        is SourceHealthState.SCHEMA_MISMATCH
    )
    assert (
        service.report_failure("SYNTH_A", T5, CorporateFailureCategory.UNAVAILABLE).health.state
        is SourceHealthState.UNAVAILABLE
    )
    assert service.health_as_of("SYNTH_A", T1 - 1).state is SourceHealthState.UNKNOWN
    assert (
        service.health_as_of("SYNTH_COMPANY", T3 + 8 * 86_400_000_000_000).state
        is SourceHealthState.STALE
    )


def test_healthy_empty_coverage_and_provider_capabilities() -> None:
    service = engine()
    source = CorporateAdapter(
        "SYNTH_A", SourceClass.LICENSED_NEWS, frozenset({CorporateKind.DIVIDEND})
    )
    empty = json.dumps(
        {
            "schema": "EIE_CORPORATE_EXTRACT_V1",
            "provider_id": "SYNTH_A",
            "source_version": "synthetic-1",
            "items": [],
            "coverage": {"kinds": ["DIVIDEND"], "from_date": "2026-10-01", "to_date": "2026-10-31"},
        }
    ).encode()
    result = service.ingest_payload(source, empty, T1, T1)
    assert result.health.state is SourceHealthState.HEALTHY
    assert result.items_seen == 0
    assert service.index.known_as_of(T1).coverage[0].kinds == (CorporateKind.DIVIDEND,)
    assert service.index.known_as_of(T1 - 1).coverage == ()
    assert (
        service.ingest_payload(source, fixture("provider_a"), T2, T2).health.state
        is SourceHealthState.SCHEMA_MISMATCH
    )


def test_repeated_runs_are_byte_identical() -> None:
    def build() -> tuple[bytes, bytes]:
        service = engine()
        service.ingest_payload(adapter("SYNTH_A"), fixture("provider_a"), T1, T1)
        service.ingest_payload(adapter("SYNTH_B"), fixture("provider_b"), T2, T2)
        service.ingest_payload(
            adapter("SYNTH_COMPANY", SourceClass.COMPANY_PRIMARY), fixture("company"), T3, T3
        )
        return service.index.canonical_state_bytes(), canonical_json_bytes(
            service.index.known_as_of(T3)
        )

    assert build() == build()
    assert Settings().fingerprint != Settings(corporate=CorporateSettings(enabled=True)).fingerprint


def test_date_only_announcements_and_action_effective_dates() -> None:
    service = engine()
    events = [
        item(
            CorporateKind.MERGER,
            {
                "target_issuer_id": ISSUER,
                "acquirer_issuer_id": None,
                "actual_close_date": "2026-11-03",
            },
            key="deal",
            event_id="deal",
            announced=None,
        ),
        item(
            CorporateKind.DELISTING,
            {"security_id": SID, "exchange": "NYSE", "effective_date": "2026-11-03"},
            key="delisting",
            event_id="delisting",
            security=SID,
            announced=None,
        ),
        item(
            CorporateKind.CEO_APPOINTMENT,
            {"person_name": "Ada Example", "role": "CEO", "effective_date": "2026-11-03"},
            key="ceo",
            event_id="ceo",
            announced=None,
        ),
    ]
    for event in events:
        event["announced_date"] = "2026-09-01"
    result = service.ingest_payload(adapter("SYNTH_A"), payload(events), T1, T1)
    assert result.events_created_or_updated == 3
    assert len(service.index.announced_dates_between("2026-09-01", "2026-09-02", T1).events) == 3
    assert service.index.announced_between(T1, T1 + 1, T1).events == ()
    assert len(service.index.effective_dates_between("2026-11-03", "2026-11-04", T1).events) == 3
    assert service.index.announced_dates_between("2026-09-01", "2026-09-02", T1 - 1).events == ()
    with pytest.raises(ValidationError):
        service.index.announced_dates_between("2026-09-02", "2026-09-01", T1)


def test_listing_identity_is_verified_and_failure_is_atomic() -> None:
    service = engine(listings=True)
    good = item(CorporateKind.DIVIDEND, {}, key="good", event_id="good")
    bad = item(CorporateKind.DIVIDEND, {}, key="bad", event_id="bad", security=SID_B)
    bad["listing_id"] = sec_listing_id(SID, "OLD", "NYSE")
    baseline = service.index.canonical_state_bytes()
    result = service.ingest_payload(adapter("SYNTH_A"), payload([good, bad]), T1, T1)
    assert result.error_category is CorporateFailureCategory.UNRESOLVED_ENTITY
    assert service.index.canonical_state_bytes() == baseline
    good["security_id"] = SID
    good["listing_id"] = sec_listing_id(SID, "OLD", "NYSE")
    accepted = service.ingest_payload(adapter("SYNTH_A"), payload([good]), T2, T2)
    assert accepted.events_created_or_updated == 1
    assert (
        service.index.events_for_security(SID, T2).events[0].corporate.identity.listing_id
        == good["listing_id"]
    )


def test_parser_failures_and_identity_query_boundaries() -> None:
    service = engine()
    source = adapter("SYNTH_A")
    assert (
        service.ingest_payload(source, b"{", T1, T1).error_category
        is CorporateFailureCategory.PARSE_ERROR
    )
    assert service.ingest_payload(source, fixture("provider_a"), T2, T2).error_category is None
    assert service.index.events_for_security("unknown", T2).events == ()
    assert service.index.upcoming_earnings(T2, security_id="unknown").events == ()
    assert service.index.upcoming_earnings(T2, issuer_id="wrong", security_id=SID).events == ()
    assert service.index.known_as_of(T2, issuer_id="wrong").events == ()
    assert service.index.known_as_of(T2, security_id=SID).events == ()
    assert service.index.known_as_of(T2, kind=CorporateKind.DIVIDEND).events == ()
    event_id = service.index.known_as_of(T2).events[0].corporate.corporate_id
    assert len(service.index.history(event_id)) == 1
    assert service.index.get(event_id, T1) is None
    assert service.index.get("missing", T2) is None
    with pytest.raises(TemporalConsistencyError):
        service.index.announced_between(T2, T1, T2)


def test_optional_action_terms_and_pending_deal_status() -> None:
    service = engine()
    dividend = item(
        CorporateKind.DIVIDEND,
        {
            "frequency": "quarterly",
            "amount": {
                "amount_micros": 125_000,
                "currency": "USD",
                "per_share": True,
            },
        },
        key="dividend-terms",
        event_id="dividend-terms",
    )
    deal = item(
        CorporateKind.MERGER,
        {
            "target_issuer_id": ISSUER,
            "acquirer_issuer_id": None,
            "transaction_structure": "cash and stock",
            "offer_price": {"amount_micros": 9_500_000, "currency": "USD", "per_share": True},
        },
        key="deal-terms",
        event_id="deal-terms",
        status=CorporateStatus.PENDING,
    )
    delisting = item(
        CorporateKind.DELISTING,
        {"security_id": SID},
        key="delisting-no-exchange",
        event_id="delisting-no-exchange",
        security=SID,
    )
    assert (
        service.ingest_payload(
            adapter("SYNTH_A"), payload([dividend, deal, delisting]), T1, T1
        ).events_created_or_updated
        == 3
    )
    assert (
        service.index.known_as_of(T1, kind=CorporateKind.DIVIDEND)
        .events[0]
        .corporate.selected_claim.payload.frequency
        == "quarterly"
    )
    pending = (
        service.index.known_as_of(T1, kind=CorporateKind.MERGER).events[0].corporate.selected_claim
    )
    assert pending.status is CorporateStatus.PENDING
    assert isinstance(pending.payload, Deal)
    assert pending.payload.offer_price.amount_micros == 9_500_000
    assert (
        service.index.known_as_of(T1, kind=CorporateKind.DELISTING)
        .events[0]
        .corporate.selected_claim.payload.exchange
        is None
    )


def test_corroborating_provider_and_query_limits() -> None:
    settings = Settings(corporate=CorporateSettings(enabled=True, max_results=1))
    service = engine(settings=settings)
    service.ingest_payload(adapter("SYNTH_A"), fixture("provider_a"), T1, T1)
    matching = json.loads(fixture("provider_b"))
    matching["items"][0]["events"][0]["data"] = json.loads(fixture("provider_a"))["items"][0][
        "events"
    ][0]["data"]
    service.ingest_payload(adapter("SYNTH_B"), json.dumps(matching).encode(), T2, T2)
    view = service.index.upcoming_earnings(T2).events[0].corporate
    assert view.confirmation is ConfirmationState.CORROBORATED
    assert view.conflict_reason_codes == ()
    assert len(view.claims) == 2
    second = item(CorporateKind.DIVIDEND, {}, key="second", event_id="second")
    service.ingest_payload(adapter("SYNTH_A"), payload([second], item_id="second"), T3, T3)
    assert len(service.index.upcoming_earnings(T3).events) == 1
    assert len(service.index.known_as_of(T3, kind=CorporateKind.DIVIDEND).events) == 1
    assert service.index.effective_dates_between("2026-11-01", "2026-11-02", T3).events == ()
    with pytest.raises(ValidationError):
        service.index.known_as_of(T3)
    with pytest.raises(ValidationError):
        service.index.effective_dates_between("2026-11-02", "2026-11-01", T3)


def test_earnings_security_filter_unknown_date_and_past_exact_time() -> None:
    service = engine()
    source = adapter("SYNTH_COMPANY", SourceClass.COMPANY_PRIMARY)
    service.ingest_payload(source, fixture("company"), T3, T3)
    other = json.loads(fixture("company"))
    other["items"][0]["item_id"] = "preferred-calendar"
    other["items"][0]["events"] = [other["items"][0]["events"][0]]
    event = other["items"][0]["events"][0]
    event["security_id"] = SID_B
    event["provider_event_id"] = "preferred-calendar"
    service.ingest_payload(source, json.dumps(other).encode(), T4, T4)
    assert len(service.index.upcoming_earnings(T4, security_id=SID).events) == 1
    assert len(service.index.upcoming_earnings(T4, security_id=SID_B).events) == 2
    exact = 1_792_585_800_000_000_000
    assert service.index.upcoming_earnings(exact + 1, security_id=SID).events == ()
    assert service.index.next_earnings(exact + 1, security_id=SID) is None
    unknown = item(
        CorporateKind.EARNINGS_SCHEDULE,
        {
            "fiscal_year": 2027,
            "fiscal_quarter": 1,
            "period_end": "2026-12-31",
            "release_timing": "UNKNOWN",
            "precision": "UNKNOWN",
        },
        status=CorporateStatus.SCHEDULED,
        event_id="unknown-calendar",
    )
    unknown.pop("semantic_key")
    service.ingest_payload(
        source, payload([unknown], provider="SYNTH_COMPANY", item_id="unknown"), T5, T5
    )
    assert len(service.index.known_as_of(T5, kind=CorporateKind.EARNINGS_SCHEDULE).events) == 3
    assert len(service.index.upcoming_earnings(T5).events) == 2


def test_inconsistent_action_identity_and_duplicate_claim_fail_atomically() -> None:
    change = {
        "security_id": SID,
        "old_ticker": "OLD",
        "old_exchange": "NYSE",
        "new_ticker": "NEW",
        "new_exchange": "NYSE",
        "effective_at_ns": EFFECTIVE,
    }
    mismatched_time = item(
        CorporateKind.SYMBOL_CHANGE, change, security=SID, effective=EFFECTIVE + 1
    )
    mismatched_security = item(
        CorporateKind.SYMBOL_CHANGE, change, security=SID_B, effective=EFFECTIVE
    )
    delisting = item(CorporateKind.DELISTING, {"security_id": SID}, security=SID_B)
    duplicate_a = item(CorporateKind.DIVIDEND, {"ex_date": "2026-11-01"})
    duplicate_b = item(CorporateKind.DIVIDEND, {"ex_date": "2026-11-02"})
    for events in (
        [mismatched_time],
        [mismatched_security],
        [delisting],
        [duplicate_a, duplicate_b],
    ):
        service = engine(listings=True)
        baseline = service.index.canonical_state_bytes()
        result = service.ingest_payload(adapter("SYNTH_A"), payload(events), T1, T1)
        assert result.error_category is CorporateFailureCategory.NORMALIZATION_ERROR
        assert service.index.canonical_state_bytes() == baseline
