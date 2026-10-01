# Scheduled events (EIE-1)

The scheduled-event engine is an offline, append-only view over EIE-0 canonical events and raw observations. `ScheduledEventIndex` owns insertion, keeps a sorted event-time index and preserves every revision; `ScheduledEventEngine` provides typed causal queries. Inserting directly into the underlying `EventStore` bypasses the schedule index. This in-memory index is not durable.

## Two clocks and one identity

`CanonicalEvent.scheduled_for_ns` is **event time**. `EventRevision.available_from_ns` is **knowledge time**. All values are UTC nanoseconds. An event may be scheduled for a future date while already known today. Every query takes an explicit `as_of_ns` or `known_as_of_ns`; no wall clock is consulted. `ReplayClock` can drive `upcoming_at_clock` and `active_windows_at_clock`.

`EventIdentity` supplies a stable semantic event ID independent of schedule time. A schedule change creates a new `ScheduleRevision` with the same event ID and a new canonical revision ID. The append-only history has contiguous revision numbers, strictly increasing knowledge times, and explicit predecessor references. `resolve_schedule(event_id, T)` returns the latest revision available at `T`, or `None` before the first known revision. Later inserts do not change serialized historical query results.

`RevisionKind` distinguishes `ORIGINAL`, `RESCHEDULE`, `CORRECTION`, `UPDATE`, and `CANCELLATION`. A correction and an actual organizer reschedule may both move `scheduled_for_ns`; the revision kind and reason codes preserve the distinction supplied by evidence. `ScheduleStatus` (`ACTIVE` or `CANCELLED`) is separate from canonical lifecycle and from event-window state. Cancellation retains the last scheduled timestamp for history, selects a cancellation claim, and disappears from ordinary upcoming/range results. `events_known_as_of` and `events_between(..., include_cancelled=True)` can expose it directly. EIE-0 makes `CANCELLED` terminal, so reinstatement is deferred until a compatible lifecycle contract is designed.

## Queries

- `events_known_as_of(T)` returns every known semantic event's resolved revision, including cancellations.
- `events_between(start, end, known_as_of, query)` filters the resolved schedule by event time in `[start, end)`. It excludes cancellations unless requested.
- `upcoming(T, query, horizon)` uses `[T, T+horizon)`; `recent(T, lookback, query)` uses `[T-lookback, T)`.
- `next_event(T, query)` returns the earliest known active schedule at or after `T`, or `None`.
- `schedule_history` and `resolve_schedule` expose append-only history and direct as-of state.
- `window_state(event_id, T)` returns one event's state, including `NORMAL`; `active_windows(T, query)` returns **all** active windows and an aggregate state.

Results sort by `(scheduled_for_ns, event_type.code, event_id)`. `ScheduledEventView.time_until_ns` is the exact signed integer `scheduled_for_ns - as_of_ns`; it is `None` for cancellations. `next_event` uses the same deterministic tie order. `ScheduledSettings` bounds range horizons and result counts; the default horizon is five 366-day years and the default result limit is 10,000. An explicit query beyond the limit fails closed. `next_event` searches the indexed future rather than applying the range horizon. The maximum representable nanosecond is an exclusive upper bound in range queries.

## Windows and direct applicability

`EventWindowPolicy` configures nonnegative `pre_event_ns`, `event_window_ns`, and `post_event_ns`. Selection precedence is exact event type, then event family, then default. The shipped development default is zero for all three; configure research policy explicitly. These values are **policy**, not learned truth or measured event impact. They carry no trading instruction.

For event time `E`, pre duration `P`, event duration `D`, and post duration `Q`, the exact half-open intervals are:

| State | Interval |
| --- | --- |
| `PRE_EVENT` | `[E-P, E)` |
| `EVENT_WINDOW` | `[E, E+D)` |
| `POST_EVENT` | `[E+D, E+D+Q)` |
| `NORMAL` | elsewhere |

Zero durations produce empty corresponding intervals. If windows overlap across events, each active event remains visible. Aggregate precedence is `EVENT_WINDOW > PRE_EVENT > POST_EVENT > NORMAL`. Cancellation yields `NORMAL`. The aggregate is descriptive context only.

Filters use EIE-0 `ApplicabilityQuery` and direct `EventScope` matching: symbol, security, company, industry, sector, index, market, macro country, and macro global. A U.S. macro event remains one scoped event, not a copy per equity. Callers supply the relevant country/market identifiers; historical ticker resolution can be performed first with the EIE-0 entity registry. No relationship propagation or inferred issuer graph exists.

## Evidence, coverage, and limitations

Each schedule revision includes one or more `ScheduleClaim`s pointing to stored raw observation IDs. Claim timestamps cannot precede raw ingestion. The configured `source_precedence.order_for("scheduled_for")` chooses the highest-ranked class. All losing claims remain attached to the revision. Disagreeing claims require the canonical `CONFLICTING_SOURCES` quality flag. Equal-precedence disagreement requires explicit adjudication reason codes; equivalent top-ranked claims use stable observation-ID tie breaking. Confirmation state and quality flags remain categorical. There is no opaque confidence score.

`CoverageLedger` records source health for an explicitly asserted event-time interval and optional direct scope, with its own availability time. Query results carry `CoverageSummary` and `QueryOutcome`. An empty result is `NO_MATCH` only when the queried interval has an applicable `AVAILABLE` coverage assertion. Otherwise it is `INDETERMINATE`, with `NOT_INGESTED`, `UNKNOWN`, `STALE`, `UNAVAILABLE`, or `DEGRADED` as appropriate. Later partial coverage does not certify the entire earlier interval. A source assertion is source-specific; it does not prove universal completeness across all possible providers or unrepresented scope. These statuses report what has been asserted in the ledger, not live adapter health.

EIE-1 contains no providers, HTTP, scraping, service endpoint, exchange calendar, event study, materiality calculation, relationship graph, or trading function. The EIE-2 additions are described below.

EIE-2 now adds the [official-source adapters](official-sources.md). Exact-time official schedules enter this index. Date-only official schedules remain canonical known facts in the underlying store until an exact time is published; no midnight or habitual release hour is fabricated, and exact-time `upcoming` excludes them. Official releases and corrections for an exact schedule append to the same semantic event history.
