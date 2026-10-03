"""Transient trusted inputs; only their references are persisted in intelligence."""

from dataclasses import dataclass

from eie.events.models import CanonicalEvent
from eie.evidence.claims import Claim, ProviderClaimObservation
from eie.evidence.models import CanonicalClaimResolution
from eie.expectations.guidance import GuidanceObservation
from eie.graph.models import GraphSnapshot
from eie.impact.models import EventImpactResult
from eie.intelligence.models import IntelligenceInputManifest
from eie.nlp.models import ExtractedEventCandidate, ExtractionRun


@dataclass(frozen=True, slots=True)
class FactInput:
    claim: Claim
    assertion: ProviderClaimObservation
    event: CanonicalEvent | None
    resolution: CanonicalClaimResolution


@dataclass(frozen=True, slots=True)
class DocumentInput:
    run: ExtractionRun
    original_hash: str
    normalized_hash: str
    normalization_fingerprint: str
    segment_hashes: tuple[str, ...]
    structured_hashes: tuple[str, ...]
    candidates: tuple[ExtractedEventCandidate, ...]
    unresolved_entity: bool


@dataclass(frozen=True, slots=True)
class IntelligenceInputs:
    manifest: IntelligenceInputManifest
    event: CanonicalEvent
    current: tuple[FactInput, ...]
    prior: tuple[FactInput, ...]
    documents: tuple[DocumentInput, ...]
    prior_documents: tuple[DocumentInput, ...]
    prior_graph: GraphSnapshot | None
    current_graph: GraphSnapshot | None
    guidance: tuple[GuidanceObservation, ...]
    scale: FactInput | None
    identity_reasons: tuple[str, ...]
    impacts: tuple[EventImpactResult, ...]
