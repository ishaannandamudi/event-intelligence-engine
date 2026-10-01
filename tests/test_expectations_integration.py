"""Existing corporate/official event stores and stable entity binding integration."""

from dataclasses import replace
from pathlib import Path

import pytest
from expectations_helpers import ISSUER, MACRO_PERIOD, PERCENT, PERIOD, ingest, record, seeded

from eie.config.settings import Settings
from eie.core.errors import PersistenceError, ValidationError
from eie.corporate.models import CorporateKind
from eie.corporate.settings import CorporateSettings
from eie.entities.models import Security, security_id
from eie.entities.sec import SecEntityRegistry
from eie.events.identity import EventIdentity
from eie.events.models import StructuredFact
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.expectations.engine import ExpectationsEngine
from eie.expectations.models import CPI_HEADLINE_MOM, EPS_DILUTED
from eie.expectations.settings import ExpectationsSettings
from eie.providers.bls.adapter import BLSAdapter
from eie.providers.common.ingestion import OfficialSourceIngestor
from eie.providers.common.models import PeriodFrequency, ReferencePeriod
from eie.providers.common.settings import OfficialProviderSettings
from eie.providers.corporate.adapter import CorporateAdapter, CorporateIngestor
from eie.providers.estimates.adapter import EstimateIngestor
from eie.scheduled.index import ScheduledEventIndex
from eie.sources.models import SourceHealth, SourceHealthState
from eie.sources.trust import SourceClass

FIXTURES = Path(__file__).parent / "fixtures"


def test_eie4_canonical_earnings_period_and_entity_link():
    seed, _ = seeded()
    settings = Settings(
        corporate=CorporateSettings(enabled=True), expectations=ExpectationsSettings(enabled=True)
    )
    corporate = CorporateIngestor(settings, seed.engine.registry)
    at = 1_788_264_000_000_000_000
    result = corporate.ingest_payload(
        CorporateAdapter("SYNTH_A", SourceClass.LICENSED_NEWS, frozenset(CorporateKind)),
        (FIXTURES / "corporate/provider_a.json").read_bytes(),
        at,
        at,
    )
    assert result.error_category is None
    canonical = corporate.index.known_as_of(at).events[0].canonical
    expectations = ExpectationsEngine(
        settings, corporate.index.event_store, corporate.index.registry
    )
    ref = expectations.bind_event(canonical.event_id, PERIOD, at)
    assert ref.issuer_id == ISSUER
    assert ref.reference_period == PERIOD
    service = EstimateIngestor(expectations)
    ingest(service, [record(canonical.event_id, 2.4, vintage=at + 10)], at + 10)
    ingest(
        service,
        [
            record(
                canonical.event_id,
                2.5,
                kind="ACTUAL",
                record_id="actual",
                published_at_ns=at + 20,
                vintage=None,
            )
        ],
        at + 20,
    )
    assert service.engine.surprise(
        canonical.event_id, EPS_DILUTED, at + 20
    ).raw_surprise == pytest.approx(0.1)
    assert not corporate.index.event_store.get_observation(
        service.engine.actuals(canonical.event_id, EPS_DILUTED, at + 20)
        .observations[0]
        .source.source_observation_id
    )


def test_eie2_official_macro_context_is_exact_and_component_specific():
    settings = Settings(
        official=OfficialProviderSettings(enabled_ids=("BLS",)),
        expectations=ExpectationsSettings(enabled=True),
    )
    index = ScheduledEventIndex(settings)
    official = OfficialSourceIngestor(index)
    at = 1_788_264_000_000_000_000
    result = official.ingest_payload(
        BLSAdapter(), (FIXTURES / "official/bls_schedule.json").read_bytes(), at, at
    )
    assert result.error_category is None
    event = next(
        v for v in index.store.known_as_of(at) if v.identity.event_type.code == "MACRO.CPI.US"
    )
    period = ReferencePeriod("2026-08-01", "2026-09-01", PeriodFrequency.MONTHLY, "August 2026")
    engine = ExpectationsEngine(settings, index.store)
    engine.bind_event(event.event_id, period, at)
    with pytest.raises(ValidationError, match="period"):
        engine.bind_event(event.event_id, MACRO_PERIOD, at)
    service = EstimateIngestor(engine)
    ingest(
        service,
        [
            record(
                event.event_id,
                0.3,
                measurement=CPI_HEADLINE_MOM,
                unit=PERCENT,
                period=period,
                vintage=at + 10,
            )
        ],
        at + 10,
    )
    ingest(
        service,
        [
            record(
                event.event_id,
                0.2,
                measurement=CPI_HEADLINE_MOM,
                unit=PERCENT,
                period=period,
                kind="ACTUAL",
                record_id="actual",
                published_at_ns=at + 20,
                vintage=None,
            )
        ],
        at + 20,
    )
    result = service.engine.surprise(event.event_id, CPI_HEADLINE_MOM, at + 20)
    assert result.raw_surprise == pytest.approx(-0.001)


@pytest.mark.parametrize(
    "known_security,known_issuer", [(True, True), (False, True), (True, False)]
)
def test_security_scope_requires_verified_security_and_issuer(known_security, known_issuer):
    service, (eid,) = seeded()
    sid = security_id("FIGI", "SYNTHETIC")
    registry = service.engine.registry if known_issuer else SecEntityRegistry()
    if not known_issuer:
        registry.add_issuer(
            replace(service.engine.registry.issuer_by_id(ISSUER, 1), available_from_ns=10)
        )
    if known_security:
        registry.add_security(Security(sid, ISSUER, "Synthetic common"))
    event = service.engine.event_store.get_event(eid, 1)
    event = replace(
        event,
        identity=EventIdentity(
            event.identity.event_type, EventScope(ScopeKind.SECURITY, (sid,)), "security-earnings"
        ),
    )
    service.engine.event_store.append_event(event)
    engine = ExpectationsEngine(service.engine.settings, service.engine.event_store, registry)
    if known_security and known_issuer:
        ref = engine.bind_event(event.event_id, PERIOD, 1)
        assert ref.issuer_id == ISSUER and ref.security_id == sid
    else:
        with pytest.raises(ValidationError, match="unresolved"):
            engine.bind_event(event.event_id, PERIOD, 1)


def test_multi_issuer_unresolved_issuer_and_changed_event_reference_fail_closed():
    service, (eid,) = seeded()
    event = service.engine.event_store.get_event(eid, 1)
    engine = ExpectationsEngine(service.engine.settings, service.engine.event_store)
    with pytest.raises(ValidationError, match="issuer"):
        engine.bind_event(eid, PERIOD, 1)
    multi = replace(
        event,
        identity=EventIdentity(
            event.identity.event_type,
            EventScope(ScopeKind.COMPANY, tuple(sorted((ISSUER, "iss_" + "f" * 64)))),
            "multi",
        ),
    )
    service.engine.event_store.append_event(multi)
    with pytest.raises(ValidationError, match="single"):
        service.engine.bind_event(multi.event_id, PERIOD, 1)
    corrected = replace(
        event,
        revision=EventRevision(2, RevisionKind.CORRECTION, 200, 1),
        facts=tuple(
            sorted((*event.facts, StructuredFact("release_stage", "ADVANCE")), key=lambda v: v.name)
        ),
    )
    service.engine.event_store.append_event(corrected)
    with pytest.raises(PersistenceError, match="identity changed"):
        service.engine.bind_event(eid, PERIOD, 200)
    invalid_stage = replace(
        corrected,
        revision=EventRevision(3, RevisionKind.CORRECTION, 300, 2),
        facts=(StructuredFact("release_stage", 1),),
    )
    service.engine.event_store.append_event(invalid_stage)
    with pytest.raises(ValidationError, match="stage"):
        service.engine.bind_event(eid, PERIOD, 300)
    with pytest.raises(ValidationError, match="health"):
        service.engine.record_health(
            "SYNTH_A", 100, SourceHealth(SourceHealthState.HEALTHY, 99, 99)
        )
