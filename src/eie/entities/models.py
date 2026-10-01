"""Stable entity IDs and historical ticker listings."""

import re
from dataclasses import dataclass

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.schema import ENTITY_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns

ISSUER_ID = re.compile(r"^iss_[0-9a-f]{64}$")
SECURITY_ID = re.compile(r"^sec_[0-9a-f]{64}$")


def issuer_id(authority: str, stable_identifier: str) -> str:
    if (
        not authority
        or authority != authority.strip()
        or not stable_identifier
        or stable_identifier != stable_identifier.strip()
        or authority.upper() in ("TICKER", "SYMBOL", "NAME")
    ):
        raise ValidationError("issuer identity requires stable non-ticker authority and identifier")
    return "iss_" + fingerprint("eie:issuer-id:v1", (authority, stable_identifier))


def security_id(authority: str, stable_identifier: str) -> str:
    if (
        not authority
        or authority != authority.strip()
        or not stable_identifier
        or stable_identifier != stable_identifier.strip()
        or authority.upper() in ("TICKER", "SYMBOL", "NAME")
    ):
        raise ValidationError(
            "security identity requires stable non-ticker authority and identifier"
        )
    return "sec_" + fingerprint("eie:security-id:v1", (authority, stable_identifier))


@dataclass(frozen=True, slots=True)
class Issuer:
    issuer_id: str
    legal_name: str
    cik: str | None = None
    aliases: tuple[str, ...] = ()
    schema_version: str = ENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, ENTITY_SCHEMA_VERSION)
        if not ISSUER_ID.fullmatch(self.issuer_id) or not self.legal_name:
            raise ValidationError("issuer requires stable ID and legal name")
        if not isinstance(self.aliases, tuple) or any(
            not isinstance(item, str) for item in self.aliases
        ):
            raise ValidationError("issuer aliases must be immutable strings")
        if self.cik is not None and (len(self.cik) != 10 or not self.cik.isdigit()):
            raise ValidationError("CIK must be ten digits")
        if tuple(sorted(set(self.aliases))) != self.aliases:
            raise ValidationError("issuer aliases must be sorted and unique")


@dataclass(frozen=True, slots=True)
class Security:
    security_id: str
    issuer_id: str
    description: str
    official_identifier: str | None = None
    schema_version: str = ENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, ENTITY_SCHEMA_VERSION)
        if (
            not SECURITY_ID.fullmatch(self.security_id)
            or not ISSUER_ID.fullmatch(self.issuer_id)
            or not self.description
        ):
            raise ValidationError("security requires stable IDs and description")


@dataclass(frozen=True, slots=True)
class Listing:
    security_id: str
    ticker: str
    exchange: str
    valid_from_ns: int
    valid_to_ns: int | None = None
    available_from_ns: int = 0
    schema_version: str = ENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, ENTITY_SCHEMA_VERSION)
        if not SECURITY_ID.fullmatch(self.security_id) or not self.ticker or not self.exchange:
            raise ValidationError("listing requires security, ticker and exchange")
        require_ns(self.valid_from_ns, "valid_from_ns")
        require_ns(self.available_from_ns, "available_from_ns")
        if self.valid_to_ns is not None:
            require_ns(self.valid_to_ns, "valid_to_ns")
            if self.valid_to_ns <= self.valid_from_ns:
                raise TemporalConsistencyError("listing interval must be positive")
