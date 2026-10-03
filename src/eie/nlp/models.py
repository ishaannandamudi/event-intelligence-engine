"""Grounded proposals, verification and immutable processing manifests."""

import re
from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.corporate.types import EarningsPeriod
from eie.evidence.claims import TimeValue, identity, ordered
from eie.expectations.units import Quantity
from eie.nlp import TEXT_EXTRACTION_SCHEMA_VERSION
from eie.nlp.checks import checked
from eie.nlp.normalization import NormalizationSettings
from eie.nlp.schema import (
    EpistemicStatus,
    ExtractionFailure,
    ExtractionMethod,
    ExtractionQuality,
    FieldName,
    MetricQualifier,
    NumericBound,
    RunStatus,
    UnscheduledFamily,
    VerificationReason,
    VerificationState,
)
from eie.studies.checks import digest


@dataclass(frozen=True, slots=True)
class SpanProposal:
    start: int
    end: int
    fragment_hash: str

    def __post_init__(self) -> None:
        checked(self)
        digest(self.fragment_hash)
        if not 0 <= self.start < self.end <= 1_000_000:
            raise ValidationError("bounded proposed span required")


@dataclass(frozen=True, slots=True)
class NumericFieldProposal:
    name: FieldName
    quantity: Quantity
    span: SpanProposal
    upper: Quantity | None = None
    bound: NumericBound = NumericBound.POINT
    qualifiers: tuple[MetricQualifier, ...] = ()

    def __post_init__(self) -> None:
        checked(self)
        if self.qualifiers != tuple(sorted(set(self.qualifiers))):
            raise ValidationError("distinct canonical proposed metric qualifiers required")
        if self.name not in (
            FieldName.REVENUE,
            FieldName.EPS,
            FieldName.GUIDANCE_REVENUE,
            FieldName.GUIDANCE_EPS,
            FieldName.MARGIN,
            FieldName.AMOUNT,
        ):
            raise ValidationError("numeric field name required")
        if (self.bound is NumericBound.CLOSED_RANGE) != (self.upper is not None):
            raise ValidationError("proposed range and bound disagree")


@dataclass(frozen=True, slots=True)
class TextFieldProposal:
    name: FieldName
    value: str
    span: SpanProposal

    def __post_init__(self) -> None:
        checked(self)
        if self.name not in (
            FieldName.ROLE,
            FieldName.PERSON,
            FieldName.ACTION,
            FieldName.AGENCY,
            FieldName.TARGET,
            FieldName.ACQUIRER,
            FieldName.IMPACT,
        ):
            raise ValidationError("textual extraction field required")


@dataclass(frozen=True, slots=True)
class TemporalFieldProposal:
    value: TimeValue
    span: SpanProposal

    def __post_init__(self) -> None:
        checked(self)


@dataclass(frozen=True, slots=True)
class PeriodProposal:
    period: EarningsPeriod
    span: SpanProposal

    def __post_init__(self) -> None:
        checked(self)


@dataclass(frozen=True, slots=True)
class EventProposal:
    key: str
    family: UnscheduledFamily
    epistemic: EpistemicStatus
    span: SpanProposal
    entity_ids: tuple[str, ...]
    numerics: tuple[NumericFieldProposal, ...] = ()
    text_fields: tuple[TextFieldProposal, ...] = ()
    temporal: TemporalFieldProposal | None = None
    period: PeriodProposal | None = None

    def __post_init__(self) -> None:
        checked(self)
        if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,79}", self.key) is None:
            raise ValidationError("safe bounded source-local candidate key required")
        ordered(self.entity_ids, 16)
        for entity in self.entity_ids:
            identity(entity, ("iss",))
        if len(self.numerics) > 32 or len(self.text_fields) > 32:
            raise ValidationError("candidate field bound exceeded")
        names = (*tuple(f.name for f in self.numerics), *tuple(f.name for f in self.text_fields))
        if len(names) != len(set(names)):
            raise ValidationError("duplicate proposed field name")


@dataclass(frozen=True, slots=True)
class ExtractedEventCandidate:
    document_id: str
    proposal: EventProposal
    method: ExtractionMethod
    quality: ExtractionQuality

    def __post_init__(self) -> None:
        checked(self)
        identity(self.document_id, ("tdoc",))

    @property
    def candidate_id(self) -> str:
        return "tcand_" + fingerprint("eie:extracted-event-candidate:v1", self)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    candidate_id: str
    state: VerificationState
    reasons: tuple[VerificationReason, ...]
    span_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        checked(self)
        identity(self.candidate_id, ("tcand",))
        for span in self.span_ids:
            identity(span, ("tspan",))
        ordered(self.span_ids, 128)
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValidationError("distinct canonical verification reasons required")
        if self.state is VerificationState.VERIFIED and (self.reasons or not self.span_ids):
            raise ValidationError("verified candidate needs source evidence and no failures")

    @property
    def verification_id(self) -> str:
        return "tverify_" + fingerprint("eie:text-verification-result:v1", self)


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    model_version: str
    temperature: float = 0.0

    def __post_init__(self) -> None:
        checked(self)
        for value in (self.model_id, self.model_version):
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value) is None or value.startswith(
                ("sk-", "ghp_", "xoxb-", "Bearer")
            ):
                raise ValidationError("credential-free model identifiers required")
        if not 0 <= self.temperature <= 2:
            raise ValidationError("bounded recorded model stochastic setting required")


@dataclass(frozen=True, slots=True)
class ExtractionSettings:
    normalization: NormalizationSettings = NormalizationSettings()
    parser_version: str = "financial-text-1"
    schema_version: str = TEXT_EXTRACTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        checked(self)
        if self.schema_version != TEXT_EXTRACTION_SCHEMA_VERSION:
            raise ValidationError("supported extraction schema required")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:text-extraction-settings:v1", self)


@dataclass(frozen=True, slots=True)
class AcceptedCandidateLink:
    candidate_id: str
    event_id: str
    claim_ids: tuple[str, ...]
    assertion_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        checked(self)
        identity(self.candidate_id, ("tcand",))
        identity(self.event_id, ("evt",))
        ordered(self.claim_ids, 128, empty=False)
        ordered(self.assertion_ids, 128, empty=False)
        for claim in self.claim_ids:
            identity(claim, ("cclaim",))
        for assertion in self.assertion_ids:
            identity(assertion, ("pclaim",))


@dataclass(frozen=True, slots=True)
class ExtractionRun:
    document_id: str
    map_id: str
    settings: ExtractionSettings
    catalog_fingerprint: str
    available_ns: int
    model: ModelDescriptor | None
    template_fingerprint: str | None
    request_fingerprint: str | None
    output_hash: str | None
    segment_ids: tuple[str, ...]
    entity_mention_ids: tuple[str, ...]
    numeric_mention_ids: tuple[str, ...]
    temporal_mention_ids: tuple[str, ...]
    period_mention_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    verification_ids: tuple[str, ...]
    span_ids: tuple[str, ...]
    accepted: tuple[AcceptedCandidateLink, ...]
    receipt_id: str | None
    status: RunStatus
    failures: tuple[ExtractionFailure, ...]
    previous_run_id: str | None = None
    retracted_assertion_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        checked(self)
        identity(self.document_id, ("tdoc",))
        identity(self.map_id, ("tmap",))
        if self.previous_run_id is not None:
            identity(self.previous_run_id, ("trun",))
        for value in (
            self.catalog_fingerprint,
            self.template_fingerprint,
            self.request_fingerprint,
            self.output_hash,
        ):
            if value is not None:
                digest(value)
        if (self.model is None) != (self.template_fingerprint is None) or (self.model is None) != (
            self.request_fingerprint is None
        ):
            raise ValidationError("model/template/request processing provenance required together")
        for values in (
            self.segment_ids,
            self.entity_mention_ids,
            self.numeric_mention_ids,
            self.temporal_mention_ids,
            self.period_mention_ids,
            self.candidate_ids,
            self.verification_ids,
            self.span_ids,
        ):
            ordered(values, 10000)
        if tuple(a.candidate_id for a in self.accepted) != tuple(
            sorted({a.candidate_id for a in self.accepted})
        ) or any(a.candidate_id not in self.candidate_ids for a in self.accepted):
            raise ValidationError("accepted candidates must reference exact run inputs")
        if self.failures != tuple(sorted(set(self.failures))):
            raise ValidationError("canonical extraction failure categories required")
        ordered(self.retracted_assertion_ids, 10000)
        if bool(self.accepted or self.retracted_assertion_ids) != (self.receipt_id is not None):
            raise ValidationError("accepted candidates require an atomic EIE-11 receipt")

    @property
    def processing_fingerprint(self) -> str:
        return fingerprint(
            "eie:text-processing-identity:v1",
            (
                self.document_id,
                self.map_id,
                self.settings,
                self.catalog_fingerprint,
                self.available_ns,
                self.model,
                self.template_fingerprint,
                self.request_fingerprint,
                self.output_hash,
                self.previous_run_id,
                self.candidate_ids,
                self.verification_ids,
                self.span_ids,
                self.status,
                self.failures,
            ),
        )

    @property
    def run_id(self) -> str:
        return "trun_" + self.processing_fingerprint

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:text-extraction-run:v1", self)
