# Descriptive event influence and decay

EIE-8 defines influence narrowly as the **magnitude and persistence of observed raw
response**. The baseline magnitude is absolute sampled cumulative return. It carries
no claim of causal impact, prediction, direction or estimated counterfactual.

`decay(result)` produces a single-event `EventDecayResult`. It records the post/zero
response curve, peak magnitude, earliest peak offset, threshold fraction, first sampled
time to that fraction after the peak, input fingerprint, method and quality reasons.
Default fraction is 0.5; `half_response_time_ns` is elapsed offset from peak, not from the
anchor. A configurable fraction has the same explicit sampled-crossing interpretation.

At least three available points are required. Missing offsets remain missing; zero curves,
insufficient observations, incomplete curves and an unobserved threshold receive structured
reasons. No crossing time is interpolated, no sustained-normalization claim is made and
an exponential fit is not forced. A crossing between observations is unresolved.
Durations inherit the study's explicit elapsed or trading-session clock.

`class_decay(results, DistributionQuery(...))` produces an **event-class** curve using
median absolute magnitude at each compatible offset, requiring the configured minimum
sample count at each point. It uses exactly the same strict-prior event/completed-study
selection, conditioning, current-event exclusion and cohort compatibility as historical
response distributions. It cannot use a future event or an incomplete later response.
Per-offset sample counts and exact input fingerprints are retained. Single and class
results have separate scope labels and deterministic fingerprints.

Class decay is a descriptive curve of cross-event medians, not an average of individual
half-response times. It may be nonmonotonic or have no observed crossing. Sampling,
contamination, event-history gaps, reconstruction assumptions, chosen reference/return type
and market-data quality limit interpretation. The original study quality remains available
through each input fingerprint. No new causal, regime or collision model is included.
