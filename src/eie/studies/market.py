"""Provider-neutral supplied data, explicit adjustment semantics and indexed retrieval."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.studies.checks import contract, digest


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    DAILY = "daily"

    @property
    def duration_ns(self) -> int:
        return {self.M1: 60, self.M5: 300, self.M15: 900, self.H1: 3600, self.DAILY: 86400}[
            self
        ] * 10**9


class Adjustment(StrEnum):
    UNADJUSTED = "UNADJUSTED"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    SPLIT_DIVIDEND_ADJUSTED = "SPLIT_DIVIDEND_ADJUSTED"


class ReturnType(StrEnum):
    PRICE_RETURN = "PRICE_RETURN"
    TOTAL_RETURN = "TOTAL_RETURN"


@dataclass(frozen=True, slots=True)
class MarketSeriesIdentity:
    instrument_id: str
    category: str
    source: str
    dataset_id: str
    dataset_fingerprint: str
    version: str
    timeframe: Timeframe
    adjustment: Adjustment
    return_type: ReturnType

    def __post_init__(self) -> None:
        contract(self)
        digest(self.dataset_fingerprint)
        if (
            self.return_type is ReturnType.TOTAL_RETURN
            and self.adjustment is not Adjustment.SPLIT_DIVIDEND_ADJUSTED
        ):
            raise ValidationError("total return requires supplied split/dividend adjusted input")


@dataclass(frozen=True, slots=True)
class MarketBar:
    instrument_id: str
    start_ns: int
    end_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    available_ns: int
    source: str
    timeframe: Timeframe

    def __post_init__(self) -> None:
        contract(self)
        for t in (self.start_ns, self.end_ns, self.available_ns):
            require_ns(t)
        if not self.start_ns < self.end_ns <= self.available_ns:
            raise ValidationError("bar must be completed before availability")
        if self.end_ns - self.start_ns > self.timeframe.duration_ns:
            raise ValidationError("bar exceeds explicit timeframe")
        if (
            not 0
            <= self.low
            <= min(self.open, self.close)
            <= max(self.open, self.close)
            <= self.high
            or self.volume < 0
        ):
            raise ValidationError("invalid OHLC/volume")


@dataclass(frozen=True, slots=True)
class MarketQuote:
    instrument_id: str
    timestamp_ns: int
    available_ns: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    source: str

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.timestamp_ns)
        require_ns(self.available_ns)
        if (
            self.available_ns < self.timestamp_ns
            or not 0 <= self.bid <= self.ask
            or min(self.bid_size, self.ask_size) < 0
        ):
            raise ValidationError("invalid quote clocks/prices/sizes")


@dataclass(frozen=True, slots=True)
class OptionsMarketObservation:
    underlying_id: str
    timestamp_ns: int
    available_ns: int
    expiry: str
    tenor: str
    bucket: str
    iv: float
    skew: float
    volume: float | None
    open_interest: float | None
    source: str

    def __post_init__(self) -> None:
        from datetime import date

        contract(self)
        require_ns(self.timestamp_ns)
        require_ns(self.available_ns)
        try:
            if date.fromisoformat(self.expiry).isoformat() != self.expiry:
                raise ValueError
        except ValueError as exc:
            raise ValidationError("options expiry requires ISO date") from exc
        if (
            self.available_ns < self.timestamp_ns
            or self.iv < 0
            or any(v is not None and v < 0 for v in (self.volume, self.open_interest))
        ):
            raise ValidationError("invalid options clocks/values")


@dataclass(frozen=True, slots=True)
class MarketSession:
    label: str
    open_ns: int
    close_ns: int
    available_ns: int
    source: str
    version: str

    def __post_init__(self) -> None:
        contract(self)
        for t in (self.open_ns, self.close_ns, self.available_ns):
            require_ns(t)
        if self.open_ns >= self.close_ns:
            raise ValidationError("invalid supplied session")


@dataclass(frozen=True, slots=True)
class MarketGap:
    start_ns: int
    end_ns: int
    reason: str

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.start_ns)
        require_ns(self.end_ns)
        if self.start_ns >= self.end_ns:
            raise ValidationError("empty market gap")


@dataclass(frozen=True, slots=True)
class SplitBoundary:
    effective_ns: int
    available_ns: int
    source: str
    action_id: str

    def __post_init__(self) -> None:
        contract(self)
        require_ns(self.effective_ns)
        require_ns(self.available_ns)


@dataclass(frozen=True, slots=True)
class MarketSlice:
    identity: MarketSeriesIdentity
    bars: tuple[MarketBar, ...]
    quotes: tuple[MarketQuote, ...] = ()
    options: tuple[OptionsMarketObservation, ...] = ()
    sessions: tuple[MarketSession, ...] = ()
    gaps: tuple[MarketGap, ...] = ()
    splits: tuple[SplitBoundary, ...] = ()

    def __post_init__(self) -> None:
        contract(self)
        for values in (self.bars, self.quotes, self.options):
            for v in values:
                instrument = (
                    v.underlying_id if isinstance(v, OptionsMarketObservation) else v.instrument_id
                )
                if instrument != self.identity.instrument_id or v.source != self.identity.source:
                    raise ValidationError("market observation identity/source mismatch")
        if any(b.timeframe is not self.identity.timeframe for b in self.bars):
            raise ValidationError("mixed explicit timeframes")
        if any(a.end_ns > b.start_ns for a, b in zip(self.bars, self.bars[1:], strict=False)):
            raise ValidationError("unsorted/overlapping bars")
        for values in (self.quotes, self.options):
            keys = tuple(
                (v.timestamp_ns, v.expiry, v.tenor, v.bucket)
                if isinstance(v, OptionsMarketObservation)
                else (v.timestamp_ns,)
                for v in values
            )
            if keys != tuple(sorted(set(keys))):
                raise ValidationError("snapshots must be sorted and unique")
        if any(
            a.close_ns > b.open_ns for a, b in zip(self.sessions, self.sessions[1:], strict=False)
        ) or len({s.label for s in self.sessions}) != len(self.sessions):
            raise ValidationError("invalid session ordering/labels")
        if self.gaps != tuple(
            sorted(set(self.gaps), key=lambda g: (g.start_ns, g.end_ns, g.reason))
        ) or self.splits != tuple(
            sorted(set(self.splits), key=lambda s: (s.effective_ns, s.action_id))
        ):
            raise ValidationError("market gaps/actions must be sorted unique")


class MarketSeriesProvider(Protocol):
    def identity(self, instrument_id: str) -> MarketSeriesIdentity | None: ...
    def read(self, instrument_id: str, start_ns: int, end_ns: int) -> MarketSlice: ...
    def sessions(self, instrument_id: str) -> tuple[MarketSession, ...]: ...


T = TypeVar("T", MarketBar, MarketQuote, OptionsMarketObservation)


class IndexedMarketProvider:
    """Offline in-memory implementation; external readers implement the same protocol."""

    def __init__(self, series: tuple[MarketSlice, ...]) -> None:
        if (
            not isinstance(series, tuple)
            or any(not isinstance(s, MarketSlice) for s in series)
            or len({s.identity.instrument_id for s in series}) != len(series)
        ):
            raise ValidationError("unique typed market series required")
        self._series = {s.identity.instrument_id: s for s in series}
        self._times = {
            s.identity.instrument_id: (
                tuple(b.start_ns for b in s.bars),
                tuple(q.timestamp_ns for q in s.quotes),
                tuple(o.timestamp_ns for o in s.options),
            )
            for s in series
        }

    def identity(self, instrument_id: str) -> MarketSeriesIdentity | None:
        s = self._series.get(instrument_id)
        return None if s is None else s.identity

    def sessions(self, instrument_id: str) -> tuple[MarketSession, ...]:
        return self._series[instrument_id].sessions

    def read(self, instrument_id: str, start_ns: int, end_ns: int) -> MarketSlice:
        require_ns(start_ns)
        require_ns(end_ns)
        if end_ns < start_ns or instrument_id not in self._series:
            raise ValidationError("invalid market range/instrument")
        s, times = self._series[instrument_id], self._times[instrument_id]
        return MarketSlice(
            s.identity,
            self._range(s.bars, times[0], start_ns, end_ns),
            self._range(s.quotes, times[1], start_ns, end_ns),
            self._range(s.options, times[2], start_ns, end_ns),
            tuple(
                session
                for session in s.sessions
                if session.close_ns >= start_ns and session.open_ns <= end_ns
            ),
            tuple(g for g in s.gaps if g.start_ns < end_ns and g.end_ns > start_ns),
            tuple(a for a in s.splits if start_ns <= a.effective_ns <= end_ns),
        )

    @staticmethod
    def _range(
        values: tuple[T, ...], times: tuple[int, ...], start: int, end: int
    ) -> tuple[T, ...]:
        return values[bisect_left(times, start) : bisect_right(times, end)]
