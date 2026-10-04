"""Deep audits delegate to each established owner's verification algorithm."""

from eie.analogues.models import AnalogueResult
from eie.analogues.repository import AnalogueRepository
from eie.analogues.validation import validate as validate_analogue
from eie.core.errors import PersistenceError
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.graph.models import GraphSnapshot
from eie.graph.propagation import PropagationResult
from eie.graph.repository import GraphRepository
from eie.graph.validation import validate_propagation, validate_snapshot
from eie.impact.models import EventImpactResult
from eie.impact.validation import validate_impact
from eie.intelligence.models import EventIntelligenceResult
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.validation import validate as validate_intelligence
from eie.interactions.models import InteractionContext
from eie.interactions.repository import InteractionRepository
from eie.interactions.validation import validate as validate_interaction
from eie.nlp.models import ExtractionRun
from eie.nlp.repository import TextRepository
from eie.nlp.validation import validate_run
from eie.persistence.sqlite import SQLiteEventStore
from eie.reliability.models import InformationLatency, SourceCoverageResult, SourceReliabilityResult
from eie.reliability.repository import ReliabilityRepository
from eie.reliability.validation import validate as validate_reliability
from eie.service.owners import Detail
from eie.studies.models import EventStudyResult
from eie.studies.validation import validate_study


def audit(store: SQLiteEventStore, value: Detail) -> None:
    valid = True
    if isinstance(value, ExtractionRun):
        validate_run(TextRepository(store), value)
    elif isinstance(value, GraphSnapshot):
        valid = validate_snapshot(value, GraphRepository(store)).valid
    elif isinstance(value, PropagationResult):
        valid = validate_propagation(value, GraphRepository(store)).valid
    elif isinstance(value, EventStudyResult):
        reader = DatasetReader(DatasetRepository(store), value.definition.event_build_id)
        valid = validate_study(value, reader).status != "INVALID"
    elif isinstance(value, EventImpactResult):
        reader = DatasetReader(
            DatasetRepository(store), value.inputs.study.definition.event_build_id
        )
        valid = validate_impact(value, reader).status != "INVALID"
    elif isinstance(value, EventIntelligenceResult):
        valid = (
            validate_intelligence(IntelligenceRepository(store), value.intelligence_id).state
            != "INVALID"
        )
    elif isinstance(value, AnalogueResult):
        valid = (
            validate_analogue(AnalogueRepository(store), value.result_id, deep=True).state
            != "INVALID"
        )
    elif isinstance(value, InteractionContext):
        validate_interaction(InteractionRepository(store), value.artifact_id, deep=True)
    elif isinstance(value, (SourceReliabilityResult, InformationLatency, SourceCoverageResult)):
        validate_reliability(ReliabilityRepository(store), value, deep=True)
    if not valid:
        raise PersistenceError("native deep audit failed")
