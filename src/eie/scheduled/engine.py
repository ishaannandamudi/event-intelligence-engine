"""High-level causal scheduled-event queries."""

from eie.config.settings import Settings
from eie.core.errors import ValidationError
from eie.core.time import MAX_NS, require_ns
from eie.events.lifecycle import EventWindowState
from eie.events.scope import ApplicabilityQuery, applies_directly
from eie.replay.clock import ReplayClock
from eie.scheduled.index import ScheduledEventIndex
from eie.scheduled.models import (
    ActiveEventWindow,
    ActiveWindowsResult,
    NextEventResult,
    ScheduledEventQueryResult,
    ScheduledEventView,
    ScheduleQueryKind,
    ScheduleRevision,
    ScheduleStatus,
)
from eie.scheduled.windows import window_state

WINDOW_PRIORITY = {
    EventWindowState.NORMAL: 0,
    EventWindowState.POST_EVENT: 1,
    EventWindowState.PRE_EVENT: 2,
    EventWindowState.EVENT_WINDOW: 3,
}


class ScheduledEventEngine:
    def __init__(self, index: ScheduledEventIndex) -> None:
        self.index = index
        self.settings: Settings = index.settings

    def schedule_history(self, event_id: str) -> tuple[ScheduleRevision, ...]:
        return self.index.history(event_id)

    def resolve_schedule(self, event_id: str, as_of_ns: int) -> ScheduleRevision | None:
        return self.index.resolve(event_id, as_of_ns)

    def _visible(
        self,
        event_id: str,
        as_of_ns: int,
        query: ApplicabilityQuery | None,
        include_cancelled: bool,
    ) -> ScheduledEventView | None:
        revision = self.index.resolve(event_id, as_of_ns)
        if revision is None:
            return None
        if query is not None and not applies_directly(revision.event.identity.scope, query):
            return None
        if revision.status is ScheduleStatus.CANCELLED and not include_cancelled:
            return None
        return ScheduledEventView(revision, as_of_ns)

    @staticmethod
    def _sort_key(view: ScheduledEventView) -> tuple[int, str, str]:
        return (view.scheduled_for_ns, view.revision.event.identity.event_type.code, view.event_id)

    def _result(
        self,
        kind: ScheduleQueryKind,
        as_of_ns: int,
        start_ns: int,
        end_ns: int,
        views: list[ScheduledEventView],
        query: ApplicabilityQuery | None,
    ) -> ScheduledEventQueryResult:
        views.sort(key=self._sort_key)
        if len(views) > self.settings.scheduled.max_results:
            raise ValidationError("scheduled query exceeds configured maximum result count")
        coverage = self.index.coverage_summary(as_of_ns, start_ns, end_ns, query)
        return ScheduledEventQueryResult(
            kind, as_of_ns, start_ns, end_ns, tuple(views), coverage, self.settings.fingerprint
        )

    def events_known_as_of(
        self, as_of_ns: int, query: ApplicabilityQuery | None = None
    ) -> ScheduledEventQueryResult:
        require_ns(as_of_ns, "as_of_ns")
        views = [
            view
            for event_id in self.index.known_ids(as_of_ns)
            if (view := self._visible(event_id, as_of_ns, query, True)) is not None
        ]
        return self._result(ScheduleQueryKind.KNOWN, as_of_ns, 0, MAX_NS, views, query)

    def events_between(
        self,
        start_ns: int,
        end_ns: int,
        known_as_of_ns: int,
        query: ApplicabilityQuery | None = None,
        include_cancelled: bool = False,
    ) -> ScheduledEventQueryResult:
        require_ns(start_ns, "start_ns")
        require_ns(end_ns, "end_ns")
        require_ns(known_as_of_ns, "known_as_of_ns")
        if end_ns < start_ns or end_ns - start_ns > self.settings.scheduled.max_query_horizon_ns:
            raise ValidationError("scheduled event-time range exceeds configured horizon")
        views = []
        for event_id in self.index.candidate_ids(start_ns, end_ns):
            view = self._visible(event_id, known_as_of_ns, query, include_cancelled)
            if view is not None and start_ns <= view.scheduled_for_ns < end_ns:
                views.append(view)
        return self._result(
            ScheduleQueryKind.BETWEEN, known_as_of_ns, start_ns, end_ns, views, query
        )

    def upcoming(
        self, as_of_ns: int, query: ApplicabilityQuery | None = None, horizon_ns: int | None = None
    ) -> ScheduledEventQueryResult:
        require_ns(as_of_ns, "as_of_ns")
        horizon = (
            self.settings.scheduled.max_query_horizon_ns
            if horizon_ns is None
            else require_ns(horizon_ns, "horizon_ns")
        )
        end_ns = min(MAX_NS, as_of_ns + horizon)
        result = self.events_between(as_of_ns, end_ns, as_of_ns, query)
        return ScheduledEventQueryResult(
            ScheduleQueryKind.UPCOMING,
            result.as_of_ns,
            result.start_ns,
            result.end_ns,
            result.events,
            result.coverage,
            result.config_fingerprint,
        )

    def next_event(self, as_of_ns: int, query: ApplicabilityQuery | None = None) -> NextEventResult:
        require_ns(as_of_ns, "as_of_ns")
        views = []
        for event_id in self.index.candidate_ids(as_of_ns, MAX_NS):
            view = self._visible(event_id, as_of_ns, query, False)
            if view is not None and view.scheduled_for_ns >= as_of_ns:
                views.append(view)
        first = min(views, key=self._sort_key) if views else None
        coverage = self.index.coverage_summary(as_of_ns, as_of_ns, MAX_NS, query)
        return NextEventResult(as_of_ns, first, coverage, self.settings.fingerprint)

    def recent(
        self, as_of_ns: int, lookback_ns: int, query: ApplicabilityQuery | None = None
    ) -> ScheduledEventQueryResult:
        require_ns(as_of_ns, "as_of_ns")
        require_ns(lookback_ns, "lookback_ns")
        start_ns = max(0, as_of_ns - lookback_ns)
        result = self.events_between(start_ns, as_of_ns, as_of_ns, query)
        return ScheduledEventQueryResult(
            ScheduleQueryKind.RECENT,
            result.as_of_ns,
            result.start_ns,
            result.end_ns,
            result.events,
            result.coverage,
            result.config_fingerprint,
        )

    def window_state(self, event_id: str, as_of_ns: int) -> ActiveEventWindow | None:
        require_ns(as_of_ns, "as_of_ns")
        view = self._visible(event_id, as_of_ns, None, True)
        if view is None:
            return None
        state = EventWindowState.NORMAL
        if view.revision.status is ScheduleStatus.ACTIVE:
            policy = self.settings.scheduled.windows.policy_for(
                view.revision.event.identity.event_type
            )
            state = window_state(as_of_ns, view.scheduled_for_ns, policy)
        return ActiveEventWindow(view, state)

    def active_windows(
        self, as_of_ns: int, query: ApplicabilityQuery | None = None
    ) -> ActiveWindowsResult:
        require_ns(as_of_ns, "as_of_ns")
        windows = self.settings.scheduled.windows
        lower = max(0, as_of_ns - windows.max_after_ns)
        upper = min(MAX_NS, as_of_ns + windows.max_pre_ns + 1)
        active = []
        for event_id in self.index.candidate_ids(lower, upper):
            view = self._visible(event_id, as_of_ns, query, False)
            if view is None:
                continue
            state = window_state(
                as_of_ns,
                view.scheduled_for_ns,
                windows.policy_for(view.revision.event.identity.event_type),
            )
            if state is not EventWindowState.NORMAL:
                active.append(ActiveEventWindow(view, state))
        active.sort(key=lambda item: self._sort_key(item.event))
        if len(active) > self.settings.scheduled.max_results:
            raise ValidationError("active windows exceed configured maximum result count")
        aggregate = max(
            (item.state for item in active),
            key=WINDOW_PRIORITY.__getitem__,
            default=EventWindowState.NORMAL,
        )
        coverage = self.index.coverage_summary(as_of_ns, lower, upper, query)
        return ActiveWindowsResult(
            as_of_ns, tuple(active), aggregate, coverage, self.settings.fingerprint
        )

    def upcoming_at_clock(
        self, clock: ReplayClock, query: ApplicabilityQuery | None = None
    ) -> ScheduledEventQueryResult:
        return self.upcoming(clock.as_of_ns, query)

    def active_windows_at_clock(
        self, clock: ReplayClock, query: ApplicabilityQuery | None = None
    ) -> ActiveWindowsResult:
        return self.active_windows(clock.as_of_ns, query)
