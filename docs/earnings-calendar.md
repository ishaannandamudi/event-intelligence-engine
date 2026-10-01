# Earnings calendar (EIE-4)

An earnings schedule identifies the *reference reporting period* using fiscal year, optional fiscal quarter, and period end. That period is the stable identity across providers and schedule revisions. It is separate from the future release date. Earnings release and earnings call are distinct event kinds, even when one company announcement is evidence for both.

`ReleaseTiming` is `BEFORE_MARKET`, `DURING_MARKET`, `AFTER_MARKET`, or `UNKNOWN`. `SchedulePrecision` is `EXACT_TIMESTAMP`, `DATE_PLUS_SESSION`, `DATE_ONLY`, or `UNKNOWN`. Exact precision requires both a civil date and UTC nanosecond instant. Date plus session requires a date and known release timing but carries no exact instant. Date-only requires unknown session timing. Unknown precision carries neither date nor timing. BMO/AMC are session labels, not synthetic clock times. A call has its own exact scheduled instant and optional credential-free HTTPS webcast URL.

`upcoming_earnings(as_of_ns, issuer_id=..., security_id=...)` returns known active schedule revisions with a release date on or after the as-of civil date; an exact time already past is excluded. `next_earnings` selects the first of these. A cancellation removes the event from upcoming results after the cancellation becomes known but leaves prior as-of results intact. Exact time changes create reschedule revisions. Call moves are tracked independently. A release publication is an earnings release event, not a rescheduled calendar item.

If providers disagree, all claims and raw evidence remain in the event. The selected schedule follows configurable source precedence; equal-rank disagreement is marked, and a company-primary update can supersede a news calendar selection. Historical `known_as_of` and upcoming queries preserve what was visible before each update. A successful empty provider batch may declare dated kind coverage; failed or stale providers do not imply that no earnings exist.

EIE-4 has no earnings consensus, actual-versus-estimate surprise, transcript analysis, inferred date from a call, or live calendar fetcher. See [corporate events](corporate-events.md) for ingestion and replay contracts.
