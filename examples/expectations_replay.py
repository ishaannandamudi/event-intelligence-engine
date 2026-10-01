"""Run from the checkout with PYTHONPATH=src; all inputs are synthetic and offline."""

import json
from pathlib import Path

from eie.config.settings import Settings
from eie.core.quality import ConfirmationState
from eie.core.serialization import canonical_json_bytes
from eie.corporate.types import EarningsPeriod
from eie.entities.sec import SecEntityRegistry, SecIssuerRevision, sec_issuer_id
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import CanonicalEvent, StructuredFact
from eie.events.revisions import EventRevision, RevisionKind
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.expectations.engine import ExpectationsEngine
from eie.expectations.models import EPS_DILUTED
from eie.expectations.settings import ExpectationsSettings
from eie.persistence.memory import InMemoryEventStore
from eie.providers.estimates.adapter import EstimateAdapter, EstimateIngestor
from eie.providers.estimates.models import EstimateCapability
from eie.providers.sec.models import CIK
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/estimates"


def build_example() -> tuple[EstimateIngestor, str]:
    """Build five prior releases and the T1/T2/T3/T4 scenario, checking replay bytes."""
    store = InMemoryEventStore()
    registry = SecEntityRegistry()
    issuer = sec_issuer_id(CIK.parse(123456))
    registry.add_issuer(SecIssuerRevision(issuer, CIK.parse(123456), "Alpha", (), 1, "obs_seed"))
    settings = Settings(expectations=ExpectationsSettings(enabled=True))
    periods = (EarningsPeriod(2026, 3, "2026-09-30"),) + tuple(
        EarningsPeriod(year, 3, f"{year}-09-30") for year in range(2021, 2026)
    )
    event_ids = []
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
        event = CanonicalEvent(
            EventIdentity(
                EventType(EventFamily.EARNINGS, "EARNINGS.SCHEDULE"),
                EventScope(ScopeKind.COMPANY, (issuer,)),
                f"event-{i}",
            ),
            EventRevision(1, RevisionKind.ORIGINAL, 1),
            LifecycleState.SCHEDULED,
            ConfirmationState.CONFIRMED_PRIMARY,
            (raw.observation_id,),
            frozenset({SourceClass.OFFICIAL_PRIMARY}),
            "Synthetic earnings",
            facts=(StructuredFact("reference_period", period.key),),
        )
        store.append_event(event)
        event_ids.append(event.event_id)
    engine = ExpectationsEngine(settings, store, registry)
    for event_id, period in zip(event_ids, periods, strict=True):
        engine.bind_event(event_id, period, 1)
    service = EstimateIngestor(engine)

    def ingest(data: bytes, at: int, provider: str = "SYNTH_A") -> None:
        source = EstimateAdapter(provider, SourceClass.LICENSED_NEWS, frozenset(EstimateCapability))
        diagnostic = service.ingest_payload(source, data, at, at)
        assert diagnostic.error_category is None

    def prior_payload(index: int, value: float, at: int, actual: bool) -> bytes:
        period = periods[index]
        row: dict[str, object] = {
            "record_id": f"{'actual' if actual else 'consensus'}-{index}",
            "observation_type": "ACTUAL" if actual else "ESTIMATE",
            "event_id": event_ids[index],
            "measurement_id": EPS_DILUTED.code,
            "reference_period": {
                "type": "EARNINGS",
                "fiscal_year": period.fiscal_year,
                "fiscal_quarter": period.fiscal_quarter,
                "period_end": period.period_end,
            },
            "quantity": {"value": value, "unit": {"kind": "CURRENCY_PER_SHARE", "currency": "USD"}},
        }
        if actual:
            row["published_at_ns"] = at
        else:
            row.update(estimate_type="PROVIDER_CONSENSUS_MEAN", vintage_at_ns=at)
        return json.dumps(
            {
                "schema": "EIE_ESTIMATES_EXTRACT_V1",
                "provider_id": "SYNTH_A",
                "source_version": "synthetic-1",
                "items": [
                    {
                        "item_id": f"history-{index}-{at}",
                        "source_reference": f"https://synth-a.example/history/{index}/{at}",
                        "records": [row],
                    }
                ],
            },
            sort_keys=True,
        ).encode()

    for i, raw_surprise in enumerate((-2, -1, 1, 2, 3), 1):
        release = i * 10
        ingest(prior_payload(i, 10, release - 1, False), release - 1)
        ingest(prior_payload(i, 10 + raw_surprise, release, True), release)

    eid = event_ids[0]
    ingest((FIXTURES / "provider_a_t1.json").read_bytes(), 100)
    earlier = canonical_json_bytes(service.engine.consensus(eid, EPS_DILUTED, 150))
    ingest((FIXTURES / "provider_a_t2.json").read_bytes(), 200)
    ingest((FIXTURES / "provider_b.json").read_bytes(), 210, "SYNTH_B")
    assert len(service.engine.consensus(eid, EPS_DILUTED, 250).provider_claims) == 2
    ingest((FIXTURES / "actual_t3.json").read_bytes(), 300)
    before = canonical_json_bytes(
        service.engine.surprise(eid, EPS_DILUTED, 300, provider_id="SYNTH_A")
    )
    ingest((FIXTURES / "provider_a_t4.json").read_bytes(), 400)
    assert canonical_json_bytes(service.engine.consensus(eid, EPS_DILUTED, 150)) == earlier
    assert (
        canonical_json_bytes(service.engine.surprise(eid, EPS_DILUTED, 300, provider_id="SYNTH_A"))
        == before
    )
    return service, eid


if __name__ == "__main__":
    service, event_id = build_example()
    result = service.engine.surprise(event_id, EPS_DILUTED, 300, provider_id="SYNTH_A")
    assert result.normalization is not None and result.normalization.observation_count == 5
    print(f"consensus={result.consensus.value}; actual=2.50; raw={result.raw_surprise:.4f}")
    print(f"prior_count=5; normalized={result.normalized_surprise:.6f}; earlier bytes unchanged")
