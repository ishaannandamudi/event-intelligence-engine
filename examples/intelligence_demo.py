"""Offline EIE-13: prior text intelligence and a separate real EIE-9 audit attachment."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.core.fingerprints import fingerprint
from eie.corporate.types import EarningsPeriod
from eie.datasets.repository import DatasetRepository
from eie.evidence.claims import Claim, ClaimSubject, StatusValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.references import EvidenceDataset, EvidenceLocator, LocatorKind
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimType, Predicate, SubjectKind
from eie.expectations.consensus import consensus_snapshot
from eie.expectations.models import ObservationSource
from eie.expectations.observations import ActualObservation, ActualRole
from eie.expectations.settings import ExpectationsSettings
from eie.expectations.surprise import calculate_surprise
from eie.expectations.units import Quantity
from eie.impact.engine import ImpactEngine
from eie.impact.models import ModelType
from eie.impact.repository import ImpactRepository
from eie.ingestion.models import RetentionPermission
from eie.intelligence.engine import IntelligenceEngine
from eie.intelligence.models import IntelligenceRequest
from eie.intelligence.policy import IntelligencePolicy
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.validation import validate
from eie.nlp.engine import TextEngine
from eie.nlp.repository import TextRepository
from eie.nlp.schema import UnscheduledFamily
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import ClaimInput, EvidenceBatch, EvidenceProvider
from eie.providers.evidence.normalization import field_reference, historical_lineage
from eie.sources.trust import SourcePrecedencePolicy
from examples.counterfactual_impact import CARS, ESTIMATION, fixture
from examples.event_studies import history
from examples.intelligence_fixtures import USD, batch
from examples.intelligence_fixtures import source as financial_source
from examples.text_demo import BODY, CATALOG, DATASET, item, source


def historical_audit(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    store, reader, ids = history(root)
    try:
        archived = DatasetRepository(store)
        fact = next(
            f
            for f in archived.facts(reader.build_id)
            if getattr(f.record, "event_id", None) == ids[-1]
        )
        event = fact.record
        dataset = EvidenceDataset(
            "intelligence-history", reader.manifest.content_fingerprint, reader.build_id
        )
        provider = EvidenceProvider(
            fact.raw.source,
            "HISTORICAL_ORIGIN",
            dataset,
            frozenset(ClaimType),
            fact.parser_version,
            RetentionPermission.ALLOWED,
        )
        lineage = historical_lineage(
            fact,
            reader.build_id,
            reader.manifest.job.definition.settings.fingerprint,
            archived.acquisitions(reader.build_id)[0],
        )
        value = StatusValue(event.lifecycle)
        claim = Claim(
            ClaimType.EVENT_STATUS,
            ClaimSubject(SubjectKind.EVENT, event.event_id),
            Predicate.STATUS,
        )
        ref = field_reference(
            fact.raw.observation_id,
            fact.raw.source_item_id,
            fact.raw.payload_hash,
            fact.parser_version,
            fact.adapter_version,
            EvidenceLocator(LocatorKind.JSON_POINTER, "/record/lifecycle"),
            value,
        )
        current = EvidenceBatch(
            fact.resource_id,
            fact.raw,
            lineage,
            (ref,),
            (ClaimInput("lifecycle", claim, value, (ref.evidence_id,), event_id=event.event_id),),
            (event,),
        )
        evidence = EvidenceRepository(store)
        EvidenceEngine(evidence).ingest(provider, current)
        repo = IntelligenceRepository(store)
        policy = IntelligencePolicy()
        repo.append_policy(policy)
        request = IntelligenceRequest(
            event.event_id,
            event.revision.number,
            dataset.dataset_id,
            fact.source_available_ns,
            reader.manifest.completed_at_ns,
            policy.policy_id,
        )
        intelligence = IntelligenceEngine(repo).compute(request)
        before = dumps(intelligence)
        base, _, _ = fixture(reader, ids[-1])
        impact = ImpactEngine().impact_event(base, ModelType.MARKET_MODEL, ESTIMATION, CARS)
        ImpactRepository(store).append(impact)
        audit = repo.attach_impact(
            intelligence.intelligence_id,
            (impact.impact_id,),
            intelligence.manifest.request.available_ns + 1,
        )
        assert audit.mode.value == "EX_POST_AUDIT"
        assert dumps(IntelligenceEngine(repo).compute(intelligence.manifest.request)) == before
        assert repo.audit(audit.audit_id) == audit
        assert validate(repo, intelligence.intelligence_id).state.value != "INVALID"
        identity = intelligence.intelligence_id
        audit_id = audit.audit_id
    finally:
        store.close()
    with SQLiteEventStore(root / "studies.sqlite") as restarted:
        repo = IntelligenceRepository(restarted)
        assert dumps(repo.get(identity)) == before and repo.audit(audit_id) == audit
    return {
        "impact_reference": impact.impact_id,
        "audit_mode": audit.mode.value,
        "impact_does_not_change_ex_ante_materiality": True,
        "historical_build": reader.build_id,
        "baseline_cutoff": intelligence.baseline_cutoff_ns,
        "actual_availability": intelligence.manifest.request.available_ns,
    }


def demonstrate(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "intelligence.sqlite"
    with SQLiteEventStore(path) as store:
        evidence = EvidenceRepository(store)
        e = EvidenceEngine(evidence)
        prior_source = replace(financial_source(), dataset=DATASET)
        for i, (quarter, value) in enumerate(((4, 4.5), (1, 4.7), (2, 4.8))):
            year = 2025 if quarter == 4 else 2026
            prior = batch(
                prior_source,
                100 + i * 10,
                Quantity(value * 1_000_000_000, USD),
                period=EarningsPeriod(year, quarter, f"{year}-{quarter * 3:02d}-28"),
            )
            e.ingest(prior_source, prior)
        p = source()
        d, body = item(p, BODY, 400)
        tr = TextRepository(store)
        run = TextEngine(tr).ingest(p, d, body, CATALOG, 500)
        accepted = next(
            a
            for a in run.accepted
            if tr.artifact(a.candidate_id).proposal.family is UnscheduledFamily.EARNINGS_RESULT
        )
        event = store.get_event(accepted.event_id, 500)
        assert event is not None
        repo = IntelligenceRepository(store)
        policy = IntelligencePolicy()
        repo.append_policy(policy)
        request = IntelligenceRequest(
            event.event_id, event.revision.number, DATASET.dataset_id, 500, 500, policy.policy_id
        )
        result = IntelligenceEngine(repo).compute(request)
        assertion = next(
            evidence.assertion(i)
            for i in accepted.assertion_ids
            if isinstance(evidence.assertion(i).value, Quantity)
            and evidence.claim(evidence.assertion(i).claim_id).measurement.measurement_id.code
            == "EARNINGS.REVENUE"
        )
        claim = evidence.claim(assertion.claim_id)
        assert claim.measurement is not None and isinstance(assertion.value, Quantity)
        source_metadata = ObservationSource(
            p.evidence.source.name,
            d.raw.source_item_id,
            d.raw.observation_id,
            p.evidence.source.source_class,
            500,
            400,
            400,
        )
        actual = ActualObservation(
            claim.measurement,
            source_metadata,
            assertion.value,
            ActualRole.ACTUAL,
            400,
            event.revision,
        )
        settings = ExpectationsSettings()
        fp = fingerprint("eie:demo-information-policy:v1", settings)
        consensus = consensus_snapshot(
            claim.measurement, (), 499, settings, SourcePrecedencePolicy(), fp
        )
        surprise = calculate_surprise(claim.measurement, 500, actual, consensus, settings, fp)
        surprise_id = repo.append_surprise(surprise)
        with_information = IntelligenceEngine(repo).compute(
            replace(request, surprise_ids=(surprise_id,))
        )
        assert (
            with_information.novelty == result.novelty
            and with_information.materiality == result.materiality
        )
        saved = dumps(with_information)
        identity = with_information.intelligence_id
        display = {
            "novelty": [
                (c.dimension.value, c.state.value, {m.name: m.value for m in c.metrics})
                for c in result.novelty
            ],
            "static_importance": result.static_importance.category.value,
            "materiality": result.materiality.category.value,
            "uncertainty": {
                c.kind.value: {"state": c.state.value, "reasons": c.reasons}
                for c in result.uncertainty
            },
            "surprise_reference": surprise_id,
            "surprise_reasons": surprise.reason_codes,
        }
    with SQLiteEventStore(path) as restarted:
        assert dumps(IntelligenceRepository(restarted).get(identity)) == saved
    return {
        **display,
        **historical_audit(root / "impact-audit"),
        "restart_byte_identical": True,
        "intelligence_id": identity,
    }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        for name, value in demonstrate(Path(directory)).items():
            print(f"{name}: {value}")
