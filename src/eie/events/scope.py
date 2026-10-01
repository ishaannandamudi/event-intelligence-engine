"""Direct scope applicability; no inferred relationship propagation."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError


class ScopeKind(StrEnum):
    SYMBOL = "SYMBOL"
    SECURITY = "SECURITY"
    COMPANY = "COMPANY"
    SEC_FILER = "SEC_FILER"
    INDUSTRY = "INDUSTRY"
    SECTOR = "SECTOR"
    INDEX = "INDEX"
    MARKET = "MARKET"
    MACRO_COUNTRY = "MACRO_COUNTRY"
    MACRO_GLOBAL = "MACRO_GLOBAL"


@dataclass(frozen=True, slots=True)
class EventScope:
    kind: ScopeKind
    identifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScopeKind) or not isinstance(self.identifiers, tuple):
            raise ValidationError("typed immutable scope required")
        if any(not isinstance(item, str) for item in self.identifiers):
            raise ValidationError("scope identifiers must be strings")
        broad = self.kind in (ScopeKind.MARKET, ScopeKind.MACRO_GLOBAL)
        if broad and self.identifiers or not broad and not self.identifiers:
            raise ValidationError("scope identifiers inconsistent with kind")
        if any(not item or item != item.strip() for item in self.identifiers):
            raise ValidationError("empty or untrimmed scope identifier")
        if tuple(sorted(set(self.identifiers))) != self.identifiers:
            raise ValidationError("scope identifiers must be sorted and unique")


@dataclass(frozen=True, slots=True)
class ApplicabilityQuery:
    symbol: str | None = None
    security_id: str | None = None
    issuer_id: str | None = None
    filer_id: str | None = None
    industry: str | None = None
    sector: str | None = None
    index: str | None = None
    market: str | None = None
    country: str | None = None


def applies_directly(scope: EventScope, query: ApplicabilityQuery) -> bool:
    if scope.kind is ScopeKind.MACRO_GLOBAL:
        return True
    if scope.kind is ScopeKind.MARKET:
        return query.market is not None
    field = {
        ScopeKind.SYMBOL: query.symbol,
        ScopeKind.SECURITY: query.security_id,
        ScopeKind.COMPANY: query.issuer_id,
        ScopeKind.SEC_FILER: query.filer_id,
        ScopeKind.INDUSTRY: query.industry,
        ScopeKind.SECTOR: query.sector,
        ScopeKind.INDEX: query.index,
        ScopeKind.MACRO_COUNTRY: query.country,
    }[scope.kind]
    return field is not None and field in scope.identifiers
