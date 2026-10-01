"""Synthetic event and extract builders for offline expectations scenarios."""

import json

from eie.config.settings import Settings
from eie.core.quality import ConfirmationState
from eie.corporate.types import EarningsPeriod
from eie.entities.sec import SecEntityRegistry, SecIssuerRevision, sec_issuer_id
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent, StructuredFact
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import MACRO_CPI_US, EventFamily, EventType
from eie.expectations.engine import ExpectationsEngine
from eie.expectations.models import EPS_DILUTED
from eie.expectations.settings import ExpectationsSettings
from eie.persistence.memory import InMemoryEventStore
from eie.providers.common.models import PeriodFrequency, ReferencePeriod
from eie.providers.estimates.adapter import EstimateAdapter, EstimateIngestor
from eie.providers.estimates.models import EstimateCapability
from eie.providers.sec.models import CIK
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

ISSUER = sec_issuer_id(CIK.parse(123456))
PERIOD = EarningsPeriod(2026, 3, "2026-09-30")
MACRO_PERIOD = ReferencePeriod("2026-08-01", "2026-09-01", PeriodFrequency.MONTHLY, "2026-08")
USD_EPS = {"kind": "CURRENCY_PER_SHARE", "currency": "USD"}
USD = {"kind": "CURRENCY", "currency": "USD"}
PERCENT = {"kind": "PERCENT"}
COUNT = {"kind": "COUNT"}


def period_data(period=PERIOD):
    if isinstance(period, EarningsPeriod):
        return {
            "type": "EARNINGS",
            "fiscal_year": period.fiscal_year,
            "fiscal_quarter": period.fiscal_quarter,
            "period_end": period.period_end,
        }
    return {
        "type": "MACRO",
        "start_date": period.start_date,
        "end_date": period.end_date,
        "frequency": period.frequency.value,
        "label": period.label,
    }


def seeded(*, periods=(PERIOD,), macro=False, settings=None):
    store = InMemoryEventStore()
    registry = SecEntityRegistry()
    registry.add_issuer(SecIssuerRevision(ISSUER, CIK.parse(123456), "Alpha", (), 1, "obs_seed"))
    ids = []
    for i, period in enumerate(periods):
        raw = RawSourceObservation(
            SourceIdentity("SYNTH_SEED", "1", "1", SourceClass.OFFICIAL_PRIMARY),
            f"event-{i}",
            "1",
            f"{i + 1:064x}",
            "application/json",
            1,
            1,
        )
        store.append_observation(raw)
        kind = MACRO_CPI_US if macro else EventType(EventFamily.EARNINGS, "EARNINGS.SCHEDULE")
        scope = (
            EventScope(ScopeKind.MACRO_COUNTRY, ("US",))
            if macro
            else EventScope(ScopeKind.COMPANY, (ISSUER,))
        )
        facts = (
            (StructuredFact("reference_period", period.key),)
            if isinstance(period, EarningsPeriod)
            else ()
        )
        event = CanonicalEvent(
            EventIdentity(kind, scope, f"event-{i}"),
            EventRevision(1, RevisionKind.ORIGINAL, 1),
            LifecycleState.SCHEDULED,
            ConfirmationState.CONFIRMED_PRIMARY,
            (raw.observation_id,),
            frozenset({SourceClass.OFFICIAL_PRIMARY}),
            "Synthetic event",
            facts=facts,
        )
        store.append_event(event)
        ids.append(event.event_id)
    engine = ExpectationsEngine(
        settings or Settings(expectations=ExpectationsSettings(enabled=True, minimum_history=3)),
        store,
        registry,
    )
    for event_id, period in zip(ids, periods, strict=True):
        engine.bind_event(event_id, period, 1)
    return EstimateIngestor(engine), tuple(ids)


def adapter(provider="SYNTH_A", source_class=SourceClass.LICENSED_NEWS, capabilities=None):
    return EstimateAdapter(
        provider,
        source_class,
        capabilities if capabilities is not None else frozenset(EstimateCapability),
    )


def record(
    event_id,
    value,
    *,
    measurement=EPS_DILUTED,
    record_id="eps-consensus",
    period=PERIOD,
    kind="ESTIMATE",
    unit=None,
    vintage=100,
    **extras,
):
    data = {
        "record_id": record_id,
        "observation_type": kind,
        "event_id": event_id,
        "measurement_id": measurement.code,
        "reference_period": period_data(period),
        "quantity": {"value": value, "unit": unit or USD_EPS},
        "vintage_at_ns": vintage,
    }
    if kind == "ESTIMATE":
        data["estimate_type"] = "PROVIDER_CONSENSUS_MEAN"
    data.update(extras)
    return data


def payload(records=(), *, provider="SYNTH_A", item_id="item", published=None, coverage=None):
    data = {
        "schema": "EIE_ESTIMATES_EXTRACT_V1",
        "provider_id": provider,
        "source_version": "synthetic-1",
        "items": [
            {
                "item_id": item_id,
                "source_reference": f"https://{provider.lower()}.example/items/{item_id}",
                "published_at_ns": published,
                "records": list(records),
            }
        ]
        if records
        else [],
    }
    if coverage is not None:
        data["coverage"] = coverage
    return json.dumps(data, sort_keys=True).encode()


def ingest(
    service,
    records,
    at,
    *,
    provider="SYNTH_A",
    item_id=None,
    source_class=SourceClass.LICENSED_NEWS,
    coverage=None,
):
    result = service.ingest_payload(
        adapter(provider, source_class),
        payload(records, provider=provider, item_id=item_id or f"item-{at}", coverage=coverage),
        at,
        at,
    )
    assert result.error_category is None, result
    return result


def historical_service(surprises=(-2, -1, 1, 2, 3), *, settings=None, provider="SYNTH_A"):
    periods = tuple(
        EarningsPeriod(2020 + i, 3, f"{2020 + i}-09-30") for i in range(len(surprises) + 2)
    )
    service, ids = seeded(periods=periods, settings=settings)
    for i, value in enumerate(surprises):
        at = (i + 1) * 100
        ingest(
            service,
            [record(ids[i], 10, period=periods[i], vintage=at - 10, record_id=f"c-{i}")],
            at - 10,
            provider=provider,
        )
        ingest(
            service,
            [
                record(
                    ids[i],
                    10 + value,
                    period=periods[i],
                    kind="ACTUAL",
                    published_at_ns=at,
                    vintage=None,
                    record_id=f"a-{i}",
                )
            ],
            at,
            provider=provider,
        )
    return service, ids, periods
