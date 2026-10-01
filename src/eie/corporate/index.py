"""Append-only corporate history with explicit knowledge and world-time queries."""

from copy import deepcopy
from datetime import timedelta

from eie.config.settings import Settings
from eie.core.errors import PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import EPOCH, require_ns
from eie.corporate.models import (
    CorporateEvent,
    CorporateKind,
    CorporateStatus,
    Deal,
    Delisting,
    Dividend,
    EarningsSchedule,
    ManagementChange,
    SpinOff,
    Split,
)
from eie.corporate.queries import (
    CorporateCoverage,
    CorporateQueryResult,
    CorporateSymbolQueryResult,
    CorporateView,
)
from eie.entities.protocols import ResolutionState
from eie.entities.sec import SecEntityRegistry
from eie.events.models import CanonicalEvent
from eie.persistence.memory import InMemoryEventStore
from eie.providers.corporate.normalization import canonical_identity
from eie.sources.models import RawSourceObservation

DAY_NS = 86_400_000_000_000
EARNINGS_KINDS = frozenset(
    {CorporateKind.EARNINGS_SCHEDULE, CorporateKind.EARNINGS_RELEASE, CorporateKind.EARNINGS_CALL}
)


class CorporateEventIndex:
    """Corporate insertion entry point; direct store writes bypass this history."""

    def __init__(self, settings: Settings, registry: SecEntityRegistry | None = None) -> None:
        self.settings = settings
        self.registry = deepcopy(registry) if registry is not None else SecEntityRegistry()
        self.event_store = InMemoryEventStore()
        self._events: dict[str, list[CorporateEvent]] = {}
        self._coverage: list[CorporateCoverage] = []

    def append_observation(self, raw: RawSourceObservation) -> bool:
        return self.event_store.append_observation(raw)

    def append(self, corporate: CorporateEvent, canonical: CanonicalEvent) -> None:
        if (
            canonical.identity != canonical_identity(corporate.identity)
            or canonical.revision != corporate.revision
        ):
            raise PersistenceError("corporate/canonical identity or revision mismatch")
        history = self._events.get(corporate.corporate_id, ())
        if corporate.revision.number != len(history) + 1:
            raise PersistenceError("corporate revision gap")
        if (
            history
            and corporate.revision.available_from_ns <= history[-1].revision.available_from_ns
        ):
            raise TemporalConsistencyError("corporate revision knowledge must advance")
        if set(canonical.evidence_observation_ids) != {
            claim.observation_id for claim in corporate.claims
        }:
            raise PersistenceError("corporate/canonical evidence mismatch")
        self.event_store.append_event(canonical)
        self._events.setdefault(corporate.corporate_id, []).append(corporate)

    def append_coverage(self, coverage: CorporateCoverage) -> None:
        if not any(
            (row.provider_id, row.kinds, row.from_date, row.to_date)
            == (coverage.provider_id, coverage.kinds, coverage.from_date, coverage.to_date)
            for row in self._coverage
        ):
            self._coverage.append(coverage)

    def history(self, corporate_id: str) -> tuple[CorporateEvent, ...]:
        return tuple(self._events.get(corporate_id, ()))

    def get(self, corporate_id: str, as_of_ns: int) -> CorporateView | None:
        require_ns(as_of_ns, "as_of_ns")
        event = next(
            (
                row
                for row in reversed(self._events.get(corporate_id, ()))
                if row.revision.available_from_ns <= as_of_ns
            ),
            None,
        )
        if event is None:
            return None
        canonical = self.event_store.get_event(
            canonical_identity(event.identity).event_id, as_of_ns
        )
        if canonical is None:
            raise PersistenceError("corporate event lacks canonical revision")
        return CorporateView(event, canonical, as_of_ns)

    def _result(self, as_of_ns: int, views: tuple[CorporateView, ...]) -> CorporateQueryResult:
        if len(views) > self.settings.corporate.max_results:
            raise ValidationError("corporate query exceeds maximum results")
        coverage = tuple(
            sorted(
                (row for row in self._coverage if row.available_from_ns <= as_of_ns),
                key=lambda row: (row.provider_id, row.available_from_ns, row.from_date),
            )
        )
        return CorporateQueryResult(as_of_ns, views, coverage, self.settings.fingerprint)

    def known_as_of(
        self,
        as_of_ns: int,
        *,
        issuer_id: str | None = None,
        security_id: str | None = None,
        kind: CorporateKind | None = None,
    ) -> CorporateQueryResult:
        require_ns(as_of_ns, "as_of_ns")
        views = []
        for view in self._views_as_of(as_of_ns):
            identity = view.corporate.identity
            if issuer_id is not None and identity.issuer_id != issuer_id:
                continue
            if security_id is not None and identity.security_id != security_id:
                continue
            if kind is not None and identity.kind is not kind:
                continue
            views.append(view)
        return self._result(as_of_ns, tuple(views))

    def _views_as_of(self, as_of_ns: int) -> tuple[CorporateView, ...]:
        return tuple(
            view
            for corporate_id in sorted(self._events)
            if (view := self.get(corporate_id, as_of_ns)) is not None
        )

    def events_for_security(self, security_id: str, as_of_ns: int) -> CorporateQueryResult:
        require_ns(as_of_ns, "as_of_ns")
        security = self.registry.security_by_id(security_id)
        issuer_id = None if security is None else security.issuer_id
        views = tuple(
            view
            for view in self._views_as_of(as_of_ns)
            if view.corporate.identity.security_id == security_id
            or (
                view.corporate.identity.security_id is None
                and view.corporate.identity.issuer_id == issuer_id
            )
        )
        return self._result(as_of_ns, views)

    def events_for_symbol(
        self, ticker: str, exchange: str, as_of_ns: int, valid_at_ns: int
    ) -> CorporateSymbolQueryResult:
        resolution = self.registry.resolve_symbol(
            ticker, exchange, as_of_ns, valid_at_ns=valid_at_ns
        )
        if resolution.state is not ResolutionState.RESOLVED:
            return CorporateSymbolQueryResult(valid_at_ns, resolution, self._result(as_of_ns, ()))
        issuer = resolution.candidate_ids[0]
        security_ids = {
            listing.security_id
            for listing in self.registry.listings_at(issuer, as_of_ns, valid_at_ns)
            if listing.ticker == ticker and listing.exchange == exchange
        }
        views = tuple(
            view
            for view in self._views_as_of(as_of_ns)
            if view.corporate.identity.issuer_id == issuer
            and (
                view.corporate.identity.security_id is None
                or view.corporate.identity.security_id in security_ids
            )
        )
        return CorporateSymbolQueryResult(valid_at_ns, resolution, self._result(as_of_ns, views))

    def upcoming_earnings(
        self, as_of_ns: int, *, issuer_id: str | None = None, security_id: str | None = None
    ) -> CorporateQueryResult:
        require_ns(as_of_ns, "as_of_ns")
        if security_id is not None:
            security = self.registry.security_by_id(security_id)
            if security is None or (issuer_id is not None and security.issuer_id != issuer_id):
                return self._result(as_of_ns, ())
            issuer_id = security.issuer_id
        today = (EPOCH + timedelta(days=as_of_ns // DAY_NS)).date().isoformat()
        views = []
        for view in self._views_as_of(as_of_ns):
            identity = view.corporate.identity
            if issuer_id is not None and identity.issuer_id != issuer_id:
                continue
            payload = view.corporate.selected_claim.payload
            if identity.kind is not CorporateKind.EARNINGS_SCHEDULE or not isinstance(
                payload, EarningsSchedule
            ):
                continue
            if security_id is not None and identity.security_id not in (None, security_id):
                continue
            if view.corporate.selected_claim.status is CorporateStatus.CANCELLED:
                continue
            if payload.scheduled_date is None or payload.scheduled_date < today:
                continue
            if payload.scheduled_for_ns is not None and payload.scheduled_for_ns < as_of_ns:
                continue
            views.append(view)
        views.sort(
            key=lambda view: (
                view.corporate.selected_claim.payload.scheduled_date
                if isinstance(view.corporate.selected_claim.payload, EarningsSchedule)
                else "",
                view.corporate.corporate_id,
            )
        )
        return self._result(as_of_ns, tuple(views))

    def next_earnings(
        self, as_of_ns: int, *, issuer_id: str | None = None, security_id: str | None = None
    ) -> CorporateView | None:
        upcoming = self.upcoming_earnings(as_of_ns, issuer_id=issuer_id, security_id=security_id)
        return upcoming.events[0] if upcoming.events else None

    def announced_between(self, start_ns: int, end_ns: int, as_of_ns: int) -> CorporateQueryResult:
        return self._time_between(start_ns, end_ns, as_of_ns, "announced_at_ns")

    def announced_dates_between(
        self, start_date: str, end_date: str, as_of_ns: int
    ) -> CorporateQueryResult:
        from eie.providers.sec.models import iso_date

        if iso_date(start_date, "start_date") > iso_date(end_date, "end_date"):
            raise ValidationError("corporate date range reversed")
        views = tuple(
            view
            for view in self._views_as_of(as_of_ns)
            if view.corporate.identity.kind not in EARNINGS_KINDS
            and (date := view.corporate.selected_claim.announced_date) is not None
            and start_date <= date < end_date
        )
        return self._result(as_of_ns, views)

    def effective_between(self, start_ns: int, end_ns: int, as_of_ns: int) -> CorporateQueryResult:
        return self._time_between(start_ns, end_ns, as_of_ns, "effective_at_ns")

    def _time_between(
        self, start_ns: int, end_ns: int, as_of_ns: int, field: str
    ) -> CorporateQueryResult:
        require_ns(start_ns, "start_ns")
        require_ns(end_ns, "end_ns")
        if end_ns < start_ns:
            raise TemporalConsistencyError("corporate range reversed")
        views = tuple(
            view
            for view in self._views_as_of(as_of_ns)
            if view.corporate.identity.kind not in EARNINGS_KINDS
            and (value := getattr(view.corporate.selected_claim, field)) is not None
            and start_ns <= value < end_ns
        )
        return self._result(as_of_ns, views)

    def effective_dates_between(
        self, start_date: str, end_date: str, as_of_ns: int
    ) -> CorporateQueryResult:
        from eie.providers.sec.models import iso_date

        if iso_date(start_date, "start_date") > iso_date(end_date, "end_date"):
            raise ValidationError("corporate date range reversed")
        views = []
        for view in self._views_as_of(as_of_ns):
            payload = view.corporate.selected_claim.payload
            date = (
                payload.ex_date
                if isinstance(payload, Dividend)
                else payload.effective_date
                if isinstance(payload, Split)
                else payload.distribution_date
                if isinstance(payload, SpinOff)
                else payload.actual_close_date
                if isinstance(payload, Deal)
                else payload.effective_date
                if isinstance(payload, (Delisting, ManagementChange))
                else None
            )
            if date is not None and start_date <= date < end_date:
                views.append(view)
        return self._result(as_of_ns, tuple(views))

    def canonical_state_bytes(self) -> bytes:
        coverage = sorted(self._coverage, key=lambda row: (row.provider_id, row.available_from_ns))
        return canonical_json_bytes(
            {
                "registry": self.registry.canonical_state_bytes().decode(),
                "events": [row for key in sorted(self._events) for row in self._events[key]],
                "coverage": coverage,
                "store": self.event_store.canonical_state_bytes().decode(),
            }
        )
