"""Immutable filing metadata, amendment relation, and query contracts."""

import re
from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import QualityFlag
from eie.core.schema import FILING_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns
from eie.entities.models import ISSUER_ID
from eie.entities.protocols import ResolutionResult
from eie.providers.sec.models import (
    CIK,
    DOCUMENT_PATTERN,
    Accession,
    SecForm,
    filing_id_for,
    iso_date,
    validate_sec_url,
)


class AmendmentStatus(StrEnum):
    NOT_AMENDMENT = "NOT_AMENDMENT"
    LINKED = "LINKED"
    UNRESOLVED = "UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class FilingMetadata:
    filing_id: str
    accession: Accession
    cik: CIK
    issuer_id: str | None
    filer_id: str
    form: SecForm
    filing_date: str
    accepted_at_ns: int
    report_period: str | None
    primary_document: str
    file_number: str | None
    source_reference: str
    source_observation_id: str
    available_from_ns: int
    revision_number: int
    amendment_status: AmendmentStatus
    amends_filing_id: str | None = None
    amendment_candidates: tuple[str, ...] = ()
    quality_flags: frozenset[QualityFlag] = frozenset()
    schema_version: str = FILING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, FILING_SCHEMA_VERSION)
        if self.filing_id != filing_id_for(self.cik, self.accession):
            raise ValidationError("stable filing ID required")
        if self.issuer_id is not None and not ISSUER_ID.fullmatch(self.issuer_id):
            raise ValidationError("invalid filing issuer ID")
        if not isinstance(self.filer_id, str) or not re.fullmatch(
            r"flr_[0-9a-f]{64}", self.filer_id
        ):
            raise ValidationError("stable filing filer ID required")
        if (
            not isinstance(self.accession, Accession)
            or not isinstance(self.cik, CIK)
            or not isinstance(self.form, SecForm)
        ):
            raise ValidationError("typed accession, CIK, and form required")
        iso_date(self.filing_date, "filing_date")
        if self.report_period is not None:
            iso_date(self.report_period, "report_period")
        if (
            not isinstance(self.primary_document, str)
            or not DOCUMENT_PATTERN.fullmatch(self.primary_document)
            or self.primary_document in (".", "..")
        ):
            raise ValidationError("invalid SEC primary document")
        if self.file_number is not None and (
            not isinstance(self.file_number, str) or not self.file_number.strip()
        ):
            raise ValidationError("invalid SEC file number")
        validate_sec_url(self.source_reference)
        require_ns(self.accepted_at_ns, "accepted_at_ns")
        require_ns(self.available_from_ns, "available_from_ns")
        if self.accepted_at_ns > self.available_from_ns:
            raise ValidationError("filing knowledge predates acceptance")
        if (
            isinstance(self.revision_number, bool)
            or not isinstance(self.revision_number, int)
            or self.revision_number < 1
        ):
            raise ValidationError("filing revision number must be positive")
        if not self.source_observation_id.startswith("obs_"):
            raise ValidationError("filing metadata requires raw observation")
        if not isinstance(self.amendment_status, AmendmentStatus):
            raise ValidationError("typed amendment status required")
        if self.form.is_amendment:
            if self.amendment_status is AmendmentStatus.NOT_AMENDMENT:
                raise ValidationError("amendment requires resolution status")
        elif self.amendment_status is not AmendmentStatus.NOT_AMENDMENT:
            raise ValidationError("ordinary filing cannot have amendment status")
        if self.amendment_status is AmendmentStatus.LINKED:
            if self.amends_filing_id is None or self.amendment_candidates:
                raise ValidationError("linked amendment needs exactly one target")
        elif self.amends_filing_id is not None:
            raise ValidationError("unlinked amendment cannot name target")
        if self.amendment_status is AmendmentStatus.AMBIGUOUS:
            if len(self.amendment_candidates) < 2:
                raise ValidationError("ambiguous amendment needs candidates")
        elif self.amendment_candidates:
            raise ValidationError("candidates require ambiguity")
        if self.amendment_candidates != tuple(sorted(set(self.amendment_candidates))):
            raise ValidationError("amendment candidates must be sorted unique")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:sec-filing-metadata:v1", self)

    @property
    def document_url(self) -> str:
        return (
            f"https://www.sec.gov/Archives/edgar/data/{self.cik.number}/"
            f"{self.accession.compact}/{self.primary_document}"
        )


@dataclass(frozen=True, slots=True)
class FilingView:
    metadata: FilingMetadata
    historical_symbols: tuple[tuple[str, str], ...]

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:sec-filing-view:v1", self)


@dataclass(frozen=True, slots=True)
class FilingQueryResult:
    as_of_ns: int
    filings: tuple[FilingView, ...]
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if any(item.metadata.available_from_ns > self.as_of_ns for item in self.filings):
            raise ValidationError("filing query includes future knowledge")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:sec-filing-query:v1", self)


@dataclass(frozen=True, slots=True)
class SymbolFilingQueryResult:
    valid_at_ns: int
    resolution: ResolutionResult
    query: FilingQueryResult

    def __post_init__(self) -> None:
        require_ns(self.valid_at_ns, "valid_at_ns")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:sec-symbol-filing-query:v1", self)
