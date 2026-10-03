"""Real immutable EIE-7/11/13 events and EIE-8 responses for offline comparisons."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from eie.analogues.models import AnaloguePolicy, ContextFeature, FeatureRow, ResponseSpec
from eie.analogues.repository import AnalogueRepository
from eie.analogues.responses import response_spec
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.evidence.claims import Claim, ClaimSubject, StatusValue
from eie.evidence.engine import EvidenceEngine
from eie.evidence.references import EvidenceDataset, EvidenceLocator, LocatorKind
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimType, Predicate, SubjectKind
from eie.ingestion.models import RetentionPermission
from eie.intelligence.engine import IntelligenceEngine
from eie.intelligence.models import IntelligenceRequest
from eie.intelligence.policy import IntelligencePolicy
from eie.intelligence.repository import IntelligenceRepository
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import ClaimInput, EvidenceBatch, EvidenceProvider
from eie.providers.evidence.normalization import field_reference, historical_lineage
from eie.studies.engine import StudyEngine
from eie.studies.market import IndexedMarketProvider, MarketSlice, Timeframe
from eie.studies.models import (
    AnchorSemantics,
    EventStudyResult,
    EventWindow,
    SessionPolicy,
    StudyPolicy,
)
from eie.studies.repository import StudyRepository
from examples.event_studies import DAY, MINUTE, history, market


@dataclass(frozen=True)
class Fixture:
    store: SQLiteEventStore
    repository: AnalogueRepository
    rows: tuple[FeatureRow, ...]
    studies: tuple[EventStudyResult, ...]
    spec: ResponseSpec


DEFAULT_POLICY = AnaloguePolicy()


def fixture(
    root: Path,
    policy: AnaloguePolicy = DEFAULT_POLICY,
    *,
    days: tuple[int, ...] = tuple(range(2, 10)),
    responses: bool = True,
    historical: tuple[SQLiteEventStore, DatasetReader, tuple[str, ...]] | None = None,
    market_factory: Callable[[int], MarketSlice] | None = None,
    stage_times: dict[str, float] | None = None,
) -> Fixture:
    root.mkdir(parents=True, exist_ok=True)
    store, reader, ids = history(root, days) if historical is None else historical
    timings = {} if stage_times is None else stage_times

    def measured(name: str, start: float) -> None:
        timings[name] = timings.get(name, 0.0) + perf_counter() - start

    archived, evidence = DatasetRepository(store), EvidenceRepository(store)
    dataset = EvidenceDataset(
        "analogue-history", reader.manifest.content_fingerprint, reader.build_id
    )
    intelligence = IntelligenceRepository(store)
    intelligence_policy = IntelligencePolicy()
    intelligence.append_policy(intelligence_policy)
    repo = AnalogueRepository(store)
    repo.append_policy(policy)
    supplied = IndexedMarketProvider((market(),))
    window = EventWindow(
        AnchorSemantics.PUBLICATION,
        4 * MINUTE,
        5 * MINUTE,
        Timeframe.M1,
        SessionPolicy.ELAPSED,
        tuple(j * MINUTE for j in range(-4, 6)),
    )
    study_policy = StudyPolicy(contamination_pre_ns=MINUTE, contamination_post_ns=5 * MINUTE)
    rows, studies = [], []
    for event_id in ids:
        start = perf_counter()
        fact = next(
            f
            for f in archived.facts(reader.build_id)
            if getattr(f.record, "event_id", None) == event_id
        )
        event = fact.record
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
            ClaimType.EVENT_STATUS, ClaimSubject(SubjectKind.EVENT, event_id), Predicate.STATUS
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
            (
                ClaimInput(
                    "lifecycle-" + event_id, claim, value, (ref.evidence_id,), event_id=event_id
                ),
            ),
            (event,),
        )
        EvidenceEngine(evidence).ingest(provider, current)
        measured("evidence_ingestion", start)
        assert fact.source_available_ns is not None
        at = fact.source_available_ns
        day = at // DAY
        start = perf_counter()
        intel = IntelligenceEngine(intelligence).compute(
            IntelligenceRequest(
                event_id,
                event.revision.number,
                dataset.dataset_id,
                at,
                reader.manifest.completed_at_ns,
                intelligence_policy.policy_id,
            )
        )
        measured("intelligence_construction", start)
        context = (
            ContextFeature(
                "macro_proximity", float(day * 10), "SYNTHETIC", "schedule-1", at, at, "MINUTES"
            ),
            ContextFeature("sector", "TECH", "SYNTHETIC", "sector-1", at, at, valid_from_ns=at),
            ContextFeature(
                "volatility", float(day) / 100, "SYNTHETIC", "vol-1", at, at, "FRACTION"
            ),
        )
        start = perf_counter()
        row = repo.index_features(intel.intelligence_id, policy.policy_id, context)
        measured("feature_index_construction", start)
        rows.append(row)
        start = perf_counter()
        provider = (
            supplied if market_factory is None else IndexedMarketProvider((market_factory(at),))
        )
        engine = StudyEngine(reader, provider, at + 5 * MINUTE)
        study = engine.study_event(
            event_id,
            "security_fixture",
            engine.anchor(event_id, AnchorSemantics.PUBLICATION),
            window,
            data_as_of_ns=at + 5 * MINUTE,
            policy=study_policy,
        )
        StudyRepository(store).append(study)
        studies.append(study)
        measured("study_construction_and_persistence", start)
    spec = response_spec(studies[0])
    if responses:
        for row, study in zip(rows[:-1], studies[:-1], strict=True):
            start = perf_counter()
            repo.index_response(row.row_id, study.study_id, spec)
            measured("response_index_construction", start)
    return Fixture(store, repo, tuple(rows), tuple(studies), spec)
