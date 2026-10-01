"""Validated SEC source identities and parsed submission metadata."""

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from urllib.parse import parse_qsl, urlsplit

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.schema import FILING_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns
from eie.sources.models import SECRET_KEYS

CIK_PATTERN = re.compile(r"^[0-9]{1,10}$")
ACCESSION_PATTERN = re.compile(r"^([0-9]{10})-?([0-9]{2})-?([0-9]{6})$")
DOCUMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FORM_BASES = frozenset({"8-K", "10-Q", "10-K", "6-K", "20-F", "S-1", "13D", "13G", "4"})
FORM_424B = re.compile(r"^424B[0-9A-Z]*$")


def validate_sec_url(url: str) -> None:
    if not isinstance(url, str):
        raise ValidationError("SEC source URL required")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"data.sec.gov", "www.sec.gov", "sec.gov"}
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or any(key.lower() in SECRET_KEYS for key, _ in parse_qsl(parts.query))
    ):
        raise ValidationError("SEC source requires approved credential-free HTTPS URL")


def iso_date(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} requires an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"invalid {name}") from exc
    if parsed.isoformat() != value:
        raise ValidationError(f"noncanonical {name}")
    return value


def filing_id_for(cik: "CIK", accession: "Accession") -> str:
    return "fil_" + fingerprint("eie:sec-filing-id:v1", (cik, accession))


@dataclass(frozen=True, slots=True)
class CIK:
    """Numeric filer identity rendered as exactly ten digits."""

    digits: str

    def __post_init__(self) -> None:
        if not isinstance(self.digits, str) or not re.fullmatch(r"[0-9]{10}", self.digits):
            raise ValidationError("canonical CIK requires ten digits")
        if int(self.digits) == 0:
            raise ValidationError("CIK zero is invalid")

    @classmethod
    def parse(cls, value: object) -> "CIK":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValidationError("CIK requires decimal digits")
        text = str(value)
        if not CIK_PATTERN.fullmatch(text) or int(text) == 0:
            raise ValidationError("invalid SEC CIK")
        return cls(text.zfill(10))

    @property
    def number(self) -> int:
        return int(self.digits)

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:sec-cik:v1", self)


@dataclass(frozen=True, slots=True)
class Accession:
    formatted: str

    def __post_init__(self) -> None:
        if not isinstance(self.formatted, str) or not re.fullmatch(
            r"[0-9]{10}-[0-9]{2}-[0-9]{6}", self.formatted
        ):
            raise ValidationError("canonical accession requires 10-2-6 digits")

    @classmethod
    def parse(cls, value: object) -> "Accession":
        if not isinstance(value, str):
            raise ValidationError("accession must be a string")
        match = ACCESSION_PATTERN.fullmatch(value)
        if match is None:
            raise ValidationError("invalid SEC accession")
        return cls("-".join(match.groups()))

    @property
    def compact(self) -> str:
        return self.formatted.replace("-", "")


@dataclass(frozen=True, slots=True)
class SecForm:
    raw: str
    base: str
    is_amendment: bool

    def __post_init__(self) -> None:
        if not isinstance(self.raw, str) or not self.raw:
            raise ValidationError("SEC form is required")
        base = self.raw.removesuffix("/A")
        normalized = base.removeprefix("SC ") if base.startswith("SC 13") else base
        if (
            (normalized not in FORM_BASES and FORM_424B.fullmatch(normalized) is None)
            or self.base != normalized
            or self.is_amendment is not self.raw.endswith("/A")
        ):
            raise ValidationError("SEC form family disagrees with raw form")

    @classmethod
    def parse(cls, value: object) -> "SecForm":
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValidationError("SEC form must be trimmed text")
        base = value.removesuffix("/A")
        normalized = base.removeprefix("SC ") if base.startswith("SC 13") else base
        if normalized not in FORM_BASES and FORM_424B.fullmatch(normalized) is None:
            raise ValidationError("unsupported SEC form")
        return cls(value, normalized, value.endswith("/A"))

    @property
    def event_code(self) -> str:
        if self.base.startswith("424B"):
            return "SEC.424B"
        return "SEC." + ("FORM_4" if self.base == "4" else self.base.replace("-", "_"))


class SecResourceKind(StrEnum):
    SUBMISSIONS = "SUBMISSIONS"
    HISTORY = "HISTORY"


@dataclass(frozen=True, slots=True)
class HistoryFileRef:
    name: str
    filing_from: str
    filing_to: str
    source_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(
            r"CIK[0-9]{10}-submissions-[0-9]+\.json", self.name
        ):
            raise ValidationError("invalid SEC history file name")
        if iso_date(self.filing_from, "filing_from") > iso_date(self.filing_to, "filing_to"):
            raise ValidationError("SEC history date range reversed")
        validate_sec_url(self.source_reference)
        if urlsplit(self.source_reference).hostname != "data.sec.gov":
            raise ValidationError("SEC history reference must use data.sec.gov")


@dataclass(frozen=True, slots=True)
class SecFilingItem:
    cik: CIK
    accession: Accession
    form: SecForm
    filing_date: str
    accepted_at_ns: int
    report_period: str | None
    primary_document: str
    file_number: str | None
    source_reference: str
    fragment_hash: str
    issuer_cik: CIK | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cik, CIK) or not isinstance(self.accession, Accession):
            raise ValidationError("typed CIK and accession required")
        if not isinstance(self.form, SecForm):
            raise ValidationError("typed SEC form required")
        if self.issuer_cik is not None and not isinstance(self.issuer_cik, CIK):
            raise ValidationError("typed filing issuer CIK required")
        iso_date(self.filing_date, "filing_date")
        require_ns(self.accepted_at_ns, "accepted_at_ns")
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
        if not isinstance(self.fragment_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.fragment_hash
        ):
            raise ValidationError("filing fragment requires SHA-256")
        validate_sec_url(self.source_reference)
        if urlsplit(self.source_reference).hostname not in {"data.sec.gov", "www.sec.gov"}:
            raise ValidationError("filing source reference must be SEC")

    @property
    def filing_id(self) -> str:
        return filing_id_for(self.cik, self.accession)


@dataclass(frozen=True, slots=True)
class SecSnapshot:
    kind: SecResourceKind
    cik: CIK
    company_name: str | None
    aliases: tuple[str, ...]
    ticker_exchange: tuple[tuple[str, str], ...]
    filings: tuple[SecFilingItem, ...]
    history_files: tuple[HistoryFileRef, ...]
    source_reference: str
    source_hash: str
    entity_type: str | None = None
    schema_version: str = FILING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, FILING_SCHEMA_VERSION)
        if not isinstance(self.kind, SecResourceKind) or not isinstance(self.cik, CIK):
            raise ValidationError("typed SEC snapshot required")
        if self.kind is SecResourceKind.SUBMISSIONS and not self.company_name:
            raise ValidationError("submissions require company name")
        if self.aliases != tuple(sorted(set(self.aliases))):
            raise ValidationError("aliases must be sorted and unique")
        if any(not isinstance(name, str) or not name.strip() for name in self.aliases):
            raise ValidationError("invalid SEC alias")
        if self.ticker_exchange != tuple(sorted(set(self.ticker_exchange))):
            raise ValidationError("ticker/exchange associations must be sorted and unique")
        if any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or any(not isinstance(part, str) or not part for part in pair)
            for pair in self.ticker_exchange
        ):
            raise ValidationError("invalid SEC ticker association")
        if self.entity_type is not None and (
            not isinstance(self.entity_type, str) or not self.entity_type.strip()
        ):
            raise ValidationError("invalid SEC entity type")
        if (
            not isinstance(self.source_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.source_hash) is None
        ):
            raise ValidationError("SEC snapshot requires source hash")
        if tuple(item.accession.formatted for item in self.filings) != tuple(
            sorted({item.accession.formatted for item in self.filings})
        ):
            raise ValidationError("SEC filings must be unique and sorted by accession")


@dataclass(frozen=True, slots=True)
class SecTickerMap:
    rows: tuple[tuple[CIK, str, str, str], ...]  # cik, name, ticker, exchange
    source_hash: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.source_hash) is None
        ):
            raise ValidationError("SEC ticker map requires source hash")
        if any(
            not isinstance(row, tuple)
            or len(row) != 4
            or not isinstance(row[0], CIK)
            or any(not isinstance(part, str) or not part for part in row[1:])
            for row in self.rows
        ):
            raise ValidationError("invalid SEC ticker-map row")
        if self.rows != tuple(
            sorted(set(self.rows), key=lambda row: (row[0].digits, row[2], row[3], row[1]))
        ):
            raise ValidationError("SEC ticker map must be sorted and unique")
