"""Explicit compatible version matrix, not permissive future decoding."""

from eie.archive.models import ARCHIVE_SCHEMA_VERSION
from eie.backfill.models import BACKFILL_SCHEMA_VERSION
from eie.core.errors import SchemaCompatibilityError
from eie.core.schema import (
    CONFIG_SCHEMA_VERSION,
    CORPORATE_SCHEMA_VERSION,
    ENTITY_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    EXPECTATIONS_SCHEMA_VERSION,
    FILING_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    OFFICIAL_SOURCE_SCHEMA_VERSION,
    SCHEDULE_SCHEMA_VERSION,
)
from eie.datasets.models import DATASET_SCHEMA_VERSION
from eie.evidence import CLAIM_EVIDENCE_SCHEMA_VERSION
from eie.graph import GRAPH_SCHEMA_VERSION
from eie.impact import IMPACT_SCHEMA_VERSION
from eie.ingestion.models import INGESTION_SCHEMA_VERSION
from eie.intelligence import EVENT_INTELLIGENCE_SCHEMA_VERSION
from eie.interactions import EVENT_INTERACTION_SCHEMA_VERSION
from eie.nlp import TEXT_EXTRACTION_SCHEMA_VERSION
from eie.persistence.migrations import STORE_SCHEMA_VERSION
from eie.persistence.sessions import LAYOUT_VERSION
from eie.reliability import SOURCE_RELIABILITY_SCHEMA_VERSION
from eie.runtime.config import RUNTIME_SCHEMA_VERSION
from eie.service import API_VERSION, SNAPSHOT_SCHEMA_VERSION
from eie.studies.models import STUDY_SCHEMA_VERSION
from eie.version import __version__


def versions() -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            {
                "engine": __version__,
                "snapshot": SNAPSHOT_SCHEMA_VERSION,
                "api": API_VERSION,
                "store": STORE_SCHEMA_VERSION,
                "event": EVENT_SCHEMA_VERSION,
                "observation": OBSERVATION_SCHEMA_VERSION,
                "entity": ENTITY_SCHEMA_VERSION,
                "config": CONFIG_SCHEMA_VERSION,
                "schedule": SCHEDULE_SCHEMA_VERSION,
                "official_source": OFFICIAL_SOURCE_SCHEMA_VERSION,
                "filing": FILING_SCHEMA_VERSION,
                "corporate": CORPORATE_SCHEMA_VERSION,
                "expectations": EXPECTATIONS_SCHEMA_VERSION,
                "archive": ARCHIVE_SCHEMA_VERSION,
                "backfill": BACKFILL_SCHEMA_VERSION,
                "dataset": DATASET_SCHEMA_VERSION,
                "evidence": CLAIM_EVIDENCE_SCHEMA_VERSION,
                "graph": GRAPH_SCHEMA_VERSION,
                "impact": IMPACT_SCHEMA_VERSION,
                "ingestion": INGESTION_SCHEMA_VERSION,
                "intelligence": EVENT_INTELLIGENCE_SCHEMA_VERSION,
                "interaction": EVENT_INTERACTION_SCHEMA_VERSION,
                "extraction": TEXT_EXTRACTION_SCHEMA_VERSION,
                "reliability": SOURCE_RELIABILITY_SCHEMA_VERSION,
                "runtime": RUNTIME_SCHEMA_VERSION,
                "session_layout": LAYOUT_VERSION,
                "study": STUDY_SCHEMA_VERSION,
                "python_requirement": ">=3.11",
            }.items()
        )
    )


def check_versions(actual: tuple[tuple[str, str], ...]) -> None:
    if actual != versions():
        raise SchemaCompatibilityError("incompatible final service version matrix")
