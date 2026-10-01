"""Corporate action and management payloads."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.corporate.types import SYMBOL, Money, _date, _issuer, _positive
from eie.entities.models import SECURITY_ID


@dataclass(frozen=True, slots=True)
class Dividend:
    amount: Money | None = None
    record_date: str | None = None
    ex_date: str | None = None
    payment_date: str | None = None
    frequency: str | None = None

    def __post_init__(self) -> None:
        if self.amount is not None and (
            not isinstance(self.amount, Money) or not self.amount.per_share
        ):
            raise ValidationError("dividend amount must be money per share")
        for name in ("record_date", "ex_date", "payment_date"):
            _date(getattr(self, name), name)
        if self.frequency is not None and (
            not self.frequency or self.frequency != self.frequency.strip()
        ):
            raise ValidationError("invalid dividend frequency")


@dataclass(frozen=True, slots=True)
class Split:
    numerator: int
    denominator: int
    record_date: str | None = None
    effective_date: str | None = None
    trading_adjustment_date: str | None = None

    def __post_init__(self) -> None:
        _positive(self.numerator, "split numerator")
        _positive(self.denominator, "split denominator")
        for name in ("record_date", "effective_date", "trading_adjustment_date"):
            _date(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Buyback:
    authorization: Money | None = None
    shares: int | None = None
    percent_basis_points: int | None = None
    expiration_date: str | None = None
    expanded: bool = False

    def __post_init__(self) -> None:
        if self.authorization is not None and (
            not isinstance(self.authorization, Money) or self.authorization.per_share
        ):
            raise ValidationError("buyback authorization must be total money")
        _positive(self.shares, "buyback shares", zero=True)
        _positive(self.percent_basis_points, "buyback basis points", zero=True)
        if self.percent_basis_points is not None and self.percent_basis_points > 10_000:
            raise ValidationError("buyback percentage exceeds 100 percent")
        _date(self.expiration_date, "expiration_date")
        if not isinstance(self.expanded, bool):
            raise ValidationError("expanded must be boolean")


@dataclass(frozen=True, slots=True)
class TenderOffer:
    price: Money | None = None
    expiration_at_ns: int | None = None
    tender_type: str | None = None

    def __post_init__(self) -> None:
        if self.price is not None and (
            not isinstance(self.price, Money) or not self.price.per_share
        ):
            raise ValidationError("tender price must be money per share")
        if self.expiration_at_ns is not None:
            require_ns(self.expiration_at_ns, "expiration_at_ns")
        if self.tender_type is not None and (
            not self.tender_type or self.tender_type != self.tender_type.strip()
        ):
            raise ValidationError("invalid tender type")


@dataclass(frozen=True, slots=True)
class Deal:
    target_issuer_id: str | None
    acquirer_issuer_id: str | None
    unresolved_party_name: str | None = None
    expected_close_date: str | None = None
    actual_close_date: str | None = None
    termination_date: str | None = None
    actual_close_at_ns: int | None = None
    terminated_at_ns: int | None = None
    cash_component: Money | None = None
    stock_component_shares: int | None = None
    transaction_structure: str | None = None
    offer_price: Money | None = None

    def __post_init__(self) -> None:
        _issuer(self.target_issuer_id, "target_issuer_id", optional=True)
        _issuer(self.acquirer_issuer_id, "acquirer_issuer_id", optional=True)
        if self.target_issuer_id is None and self.acquirer_issuer_id is None:
            raise ValidationError("deal needs at least one stable party")
        if self.unresolved_party_name is not None and (
            not self.unresolved_party_name
            or self.unresolved_party_name != self.unresolved_party_name.strip()
        ):
            raise ValidationError("invalid unresolved party name")
        _date(self.expected_close_date, "expected_close_date")
        _date(self.actual_close_date, "actual_close_date")
        _date(self.termination_date, "termination_date")
        for name in ("actual_close_at_ns", "terminated_at_ns"):
            value = getattr(self, name)
            if value is not None:
                require_ns(value, name)
        if self.cash_component is not None and (
            not isinstance(self.cash_component, Money) or self.cash_component.per_share
        ):
            raise ValidationError("deal cash component must be total money")
        _positive(self.stock_component_shares, "stock_component_shares", zero=True)
        if self.transaction_structure is not None and (
            not self.transaction_structure
            or self.transaction_structure != self.transaction_structure.strip()
        ):
            raise ValidationError("invalid transaction structure")
        if self.offer_price is not None and (
            not isinstance(self.offer_price, Money) or not self.offer_price.per_share
        ):
            raise ValidationError("deal offer price must be money per share")


@dataclass(frozen=True, slots=True)
class SpinOff:
    spun_issuer_id: str | None = None
    record_date: str | None = None
    distribution_date: str | None = None
    ratio_numerator: int | None = None
    ratio_denominator: int | None = None

    def __post_init__(self) -> None:
        _issuer(self.spun_issuer_id, "spun_issuer_id", optional=True)
        _date(self.record_date, "record_date")
        _date(self.distribution_date, "distribution_date")
        if (self.ratio_numerator is None) != (self.ratio_denominator is None):
            raise ValidationError("spin-off ratio requires both components")
        _positive(self.ratio_numerator, "ratio_numerator")
        _positive(self.ratio_denominator, "ratio_denominator")


@dataclass(frozen=True, slots=True)
class SymbolChange:
    security_id: str
    old_ticker: str
    old_exchange: str
    new_ticker: str
    new_exchange: str
    effective_at_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.security_id, str) or not SECURITY_ID.fullmatch(self.security_id):
            raise ValidationError("symbol change requires stable security")
        for name in ("old_ticker", "old_exchange", "new_ticker", "new_exchange"):
            value = getattr(self, name)
            if not isinstance(value, str) or not SYMBOL.fullmatch(value):
                raise ValidationError(f"invalid {name}")
        if (self.old_ticker, self.old_exchange) == (self.new_ticker, self.new_exchange):
            raise ValidationError("symbol change must change a symbol or exchange")
        require_ns(self.effective_at_ns, "symbol effective_at_ns")
        if self.effective_at_ns == 0:
            raise ValidationError("symbol effective time must be positive")


@dataclass(frozen=True, slots=True)
class Delisting:
    security_id: str
    exchange: str | None
    expected_date: str | None = None
    effective_date: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.security_id, str) or not SECURITY_ID.fullmatch(self.security_id):
            raise ValidationError("delisting requires stable security")
        if self.exchange is not None and (
            not isinstance(self.exchange, str) or not SYMBOL.fullmatch(self.exchange)
        ):
            raise ValidationError("invalid exchange")
        _date(self.expected_date, "expected_date")
        _date(self.effective_date, "effective_date")
        if self.reason is not None and (not self.reason or self.reason != self.reason.strip()):
            raise ValidationError("invalid delisting reason")


@dataclass(frozen=True, slots=True)
class ManagementChange:
    person_name: str
    role: str
    source_person_id: str | None = None
    effective_date: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.person_name, str)
            or not self.person_name
            or self.person_name != self.person_name.strip()
            or not isinstance(self.role, str)
            or not self.role
            or self.role != self.role.strip()
        ):
            raise ValidationError("management change needs person and role")
        if self.source_person_id is not None and (
            not self.source_person_id or self.source_person_id != self.source_person_id.strip()
        ):
            raise ValidationError("invalid source person ID")
        _date(self.effective_date, "effective_date")
