"""Corporate identity, source claims, and revision contracts.

Payload and value types are re-exported here as the stable public import surface.
"""

import re
from dataclasses import dataclass

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.quality import ConfirmationState, QualityFlag
from eie.core.schema import CORPORATE_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns
from eie.corporate.actions import (
    Buyback,
    Deal,
    Delisting,
    Dividend,
    ManagementChange,
    SpinOff,
    Split,
    SymbolChange,
    TenderOffer,
)
from eie.corporate.earnings import EarningsCall, EarningsRelease, EarningsSchedule
from eie.corporate.types import (
    PROVIDER_ID,
    CorporateKind,
    CorporateStatus,
    _date,
    _issuer,
)
from eie.corporate.types import (
    EarningsPeriod as EarningsPeriod,
)
from eie.corporate.types import (
    Money as Money,
)
from eie.corporate.types import (
    ReleaseTiming as ReleaseTiming,
)
from eie.corporate.types import (
    SchedulePrecision as SchedulePrecision,
)
from eie.entities.models import SECURITY_ID
from eie.events.revisions import EventRevision
from eie.sources.trust import SourceClass

__all__ = (
    "Buyback",
    "CorporateClaim",
    "CorporateEvent",
    "CorporateIdentity",
    "CorporateKind",
    "CorporatePayload",
    "CorporateStatus",
    "Deal",
    "Delisting",
    "Dividend",
    "EarningsCall",
    "EarningsPeriod",
    "EarningsRelease",
    "EarningsSchedule",
    "ManagementChange",
    "Money",
    "PAYLOAD_TYPE",
    "PROVIDER_ID",
    "ReleaseTiming",
    "SchedulePrecision",
    "SpinOff",
    "Split",
    "SymbolChange",
    "TenderOffer",
)

CorporatePayload = (
    EarningsSchedule
    | EarningsRelease
    | EarningsCall
    | Dividend
    | Split
    | Buyback
    | TenderOffer
    | Deal
    | SpinOff
    | SymbolChange
    | Delisting
    | ManagementChange
)

PAYLOAD_TYPE: dict[CorporateKind, type[object]] = {
    CorporateKind.EARNINGS_SCHEDULE: EarningsSchedule,
    CorporateKind.EARNINGS_RELEASE: EarningsRelease,
    CorporateKind.EARNINGS_CALL: EarningsCall,
    CorporateKind.DIVIDEND: Dividend,
    CorporateKind.SPECIAL_DIVIDEND: Dividend,
    CorporateKind.STOCK_SPLIT: Split,
    CorporateKind.REVERSE_SPLIT: Split,
    CorporateKind.BUYBACK: Buyback,
    CorporateKind.TENDER_OFFER: TenderOffer,
    CorporateKind.MERGER: Deal,
    CorporateKind.ACQUISITION: Deal,
    CorporateKind.SPIN_OFF: SpinOff,
    CorporateKind.SYMBOL_CHANGE: SymbolChange,
    CorporateKind.DELISTING: Delisting,
    CorporateKind.CEO_DEPARTURE: ManagementChange,
    CorporateKind.CEO_APPOINTMENT: ManagementChange,
    CorporateKind.CFO_DEPARTURE: ManagementChange,
    CorporateKind.CFO_APPOINTMENT: ManagementChange,
    CorporateKind.BOARD_CHANGE: ManagementChange,
    CorporateKind.AUDITOR_CHANGE: ManagementChange,
}


@dataclass(frozen=True, slots=True)
class CorporateIdentity:
    kind: CorporateKind
    issuer_id: str
    semantic_key: str
    security_id: str | None = None
    listing_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CorporateKind):
            raise ValidationError("typed corporate kind required")
        _issuer(self.issuer_id, "issuer_id")
        if (
            not isinstance(self.semantic_key, str)
            or not self.semantic_key
            or self.semantic_key != self.semantic_key.strip()
        ):
            raise ValidationError("stable semantic key required")
        if self.security_id is not None and (
            not isinstance(self.security_id, str) or not SECURITY_ID.fullmatch(self.security_id)
        ):
            raise ValidationError("invalid security ID")
        if self.listing_id is not None and (
            not isinstance(self.listing_id, str)
            or re.fullmatch(r"lst_[0-9a-f]{64}", self.listing_id) is None
        ):
            raise ValidationError("invalid listing ID")

    @property
    def corporate_id(self) -> str:
        return "corp_" + fingerprint("eie:corporate-identity:v1", self)


@dataclass(frozen=True, slots=True)
class CorporateClaim:
    provider_id: str
    provider_event_id: str
    observation_id: str
    source_class: SourceClass
    available_from_ns: int
    payload: CorporatePayload
    status: CorporateStatus
    announced_at_ns: int | None = None
    effective_at_ns: int | None = None
    quality_flags: frozenset[QualityFlag] = frozenset()
    announced_date: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not PROVIDER_ID.fullmatch(self.provider_id):
            raise ValidationError("stable provider ID required")
        if (
            not isinstance(self.provider_event_id, str)
            or not self.provider_event_id
            or self.provider_event_id != self.provider_event_id.strip()
        ):
            raise ValidationError("provider event ID required")
        if not isinstance(self.observation_id, str) or not self.observation_id.startswith("obs_"):
            raise ValidationError("raw observation ID required")
        if (
            not isinstance(self.source_class, SourceClass)
            or not isinstance(self.status, CorporateStatus)
            or not isinstance(self.payload, tuple(PAYLOAD_TYPE.values()))
        ):
            raise ValidationError("typed corporate claim required")
        if not isinstance(self.quality_flags, frozenset) or any(
            not isinstance(flag, QualityFlag) for flag in self.quality_flags
        ):
            raise ValidationError("typed claim quality flags required")
        require_ns(self.available_from_ns, "available_from_ns")
        for name in ("announced_at_ns", "effective_at_ns"):
            value = getattr(self, name)
            if value is not None:
                require_ns(value, name)
        if self.announced_at_ns is not None and self.announced_at_ns > self.available_from_ns:
            raise TemporalConsistencyError("announcement follows knowledge")
        _date(self.announced_date, "announced_date")

    @property
    def key(self) -> tuple[str, str]:
        return self.provider_id, self.provider_event_id

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:corporate-claim:v1", self)


@dataclass(frozen=True, slots=True)
class CorporateEvent:
    identity: CorporateIdentity
    revision: EventRevision
    claims: tuple[CorporateClaim, ...]
    selected_claim_key: tuple[str, str]
    confirmation: ConfirmationState
    quality_flags: frozenset[QualityFlag] = frozenset()
    conflict_reason_codes: tuple[str, ...] = ()
    schema_version: str = CORPORATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, CORPORATE_SCHEMA_VERSION)
        if not isinstance(self.identity, CorporateIdentity) or not isinstance(
            self.revision, EventRevision
        ):
            raise ValidationError("typed corporate event identity and revision required")
        if (
            not isinstance(self.claims, tuple)
            or not self.claims
            or any(not isinstance(claim, CorporateClaim) for claim in self.claims)
        ):
            raise ValidationError("corporate event needs typed claims")
        keys = tuple(claim.key for claim in self.claims)
        if keys != tuple(sorted(set(keys))) or self.selected_claim_key not in keys:
            raise ValidationError("corporate claims must be sorted unique with selection")
        if any(
            not isinstance(claim.payload, PAYLOAD_TYPE[self.identity.kind]) for claim in self.claims
        ):
            raise ValidationError("corporate payload disagrees with event kind")
        if self.identity.kind in (
            CorporateKind.EARNINGS_SCHEDULE,
            CorporateKind.EARNINGS_RELEASE,
            CorporateKind.EARNINGS_CALL,
        ):
            if any(
                not isinstance(claim.payload, (EarningsSchedule, EarningsRelease, EarningsCall))
                or claim.payload.period.key != self.identity.semantic_key
                for claim in self.claims
            ):
                raise ValidationError("earnings identity must equal reporting period")
        if self.identity.kind in (
            CorporateKind.EARNINGS_SCHEDULE,
            CorporateKind.EARNINGS_CALL,
        ) and any(
            claim.status not in (CorporateStatus.SCHEDULED, CorporateStatus.CANCELLED)
            for claim in self.claims
        ):
            raise ValidationError("scheduled earnings need scheduled/cancelled status")
        if self.identity.kind in (CorporateKind.STOCK_SPLIT, CorporateKind.REVERSE_SPLIT):
            if any(
                not isinstance(claim.payload, Split)
                or (
                    self.identity.kind is CorporateKind.STOCK_SPLIT
                    and claim.payload.numerator <= claim.payload.denominator
                )
                or (
                    self.identity.kind is CorporateKind.REVERSE_SPLIT
                    and claim.payload.numerator >= claim.payload.denominator
                )
                for claim in self.claims
            ):
                raise ValidationError("split ratio disagrees with forward/reverse kind")
        if self.identity.kind is CorporateKind.SYMBOL_CHANGE and any(
            not isinstance(claim.payload, SymbolChange)
            or claim.payload.security_id != self.identity.security_id
            for claim in self.claims
        ):
            raise ValidationError("symbol change needs matching security identity")
        if (
            not isinstance(self.confirmation, ConfirmationState)
            or not isinstance(self.quality_flags, frozenset)
            or any(not isinstance(flag, QualityFlag) for flag in self.quality_flags)
        ):
            raise ValidationError("typed corporate quality required")
        if self.conflict_reason_codes != tuple(sorted(set(self.conflict_reason_codes))):
            raise ValidationError("conflict reasons must be sorted unique")
        if any(claim.available_from_ns > self.revision.available_from_ns for claim in self.claims):
            raise TemporalConsistencyError("claim follows event availability")
        if (
            len(
                {
                    (
                        claim.payload,
                        claim.status,
                        claim.announced_at_ns,
                        claim.effective_at_ns,
                        claim.announced_date,
                    )
                    for claim in self.claims
                }
            )
            > 1
            and QualityFlag.CONFLICTING_SOURCES not in self.quality_flags
        ):
            raise ValidationError("disagreeing claims require conflict flag")
        if self.conflict_reason_codes and QualityFlag.CONFLICTING_SOURCES not in self.quality_flags:
            raise ValidationError("conflict reasons require conflict flag")

    @property
    def corporate_id(self) -> str:
        return self.identity.corporate_id

    @property
    def selected_claim(self) -> CorporateClaim:
        return next(claim for claim in self.claims if claim.key == self.selected_claim_key)

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:corporate-event:v1", self)
