# EIE-5 completion report

Completed from the existing working tree. Changes remain uncommitted; nothing was
pushed. EIE-6 implementation has not started.

## Implementation (A–AC)

| Item | Result |
| --- | --- |
| A. Architecture | Causal expectations engine over copies of known canonical events and a verified SEC registry; raw-first, staged ingestion and reconstructable queries. |
| B. Package structure | `eie.expectations` contains units, models, observations, guidance, histories, consensus, surprise, normalization, queries, engine and settings; `eie.providers.estimates` contains typed records, parser and adapter. |
| C. Expectations schema | New schema `1.0`; observation constructors reject unsupported versions. Existing artifact schema constants remain unchanged. |
| D. Measurement/unit model | Separate extensible earnings/macro/guidance IDs; exact period, stage and seasonal adjustment; reported/canonical quantities; explicit currencies, rates, rate changes, counts, indices and ratios. Non-finite values and unsafe conversion fail. |
| E. Estimate contract | Immutable context/source/value/kind/revision/contributor/count/quality/schema with observation, stream and result identities. |
| F. Estimate vintages | Append-only revisions, latest eligible availability at query time, raw provenance verification, and no backward projection of late-delivered historical vintages. Missing vintage is explicitly unsafe for surprise. |
| G. Provider consensus | Reported mean/median/high/low/count; independent competing provider claims and configured precedence; no automatic provider averaging. |
| H. EIE-computed consensus | Explicit within-provider aggregation of latest individual observations identified by stable contributors. |
| I. Contributors/dispersion | Provider plus contributor identity; anonymous observations retained but excluded; count/high/low/spread, sample standard deviation and IQR where supported; no invented provider dispersion. |
| J. Freshness/conflicts | Age uses oldest contributing knowledge time; stale value stays present; source outages, missing coverage and provider disagreement remain explicit. |
| K. Actual contract | Immutable raw-backed actual values with publication/observation/availability clocks and append-only correction chains. |
| L. Previous/revised previous | Distinct roles and streams with explicit previous comparison period; corrections affect only later views. |
| M. Raw surprise | Actual minus compatible, point-in-time consensus; no previous-value substitution; missingness and overflow have reasons. |
| N. Relative surprise | Opt-in diluted EPS/revenue in matching monetary dimensions, absolute consensus denominator and minimum magnitude guard. |
| O. Earnings surprise | EPS and revenue remain independent components; no combined scalar or trading direction. |
| P. Macro surprise | Separate component queries; counts/rates normalized, previous/revised previous preserved, stage and seasonal adjustment explicit. |
| Q. Historical series | Ordered immutable historical rows reconstructed as of a supplied time, preserving selected provider/method and surprise evidence fingerprint. |
| R. Prior-only normalization | Strictly earlier release and availability, excluding self/same-time/future events; compatible series/provider default; MAD or configured sample standard deviation; expanding or rolling window. |
| S. Normalization artifact | Immutable `SurpriseScale` retains cutoff, method, scope, center, scale, count, complete historical rows, reasons, configuration and result fingerprint. Insufficient/zero/non-finite scale is missing. |
| T. Guidance | Typed stable-issuer target-period range/point observations, preserved bounds, derived midpoint and causal revisions. |
| U. Guidance comparisons | Earlier company guidance changes and publication-cutoff market consensus comparisons are separate, with finite/missingness guards. |
| V. Adapters | Synthetic `EIE_ESTIMATES_EXTRACT_V1`; explicit consensus/analyst/actual/guidance/vintage capabilities; no live networking. |
| W. Queries | Estimates, actuals, guidance, consensus, surprise and historical surprise queries plus two guidance comparisons. |
| X. Persistence/atomicity | In-memory append-only ledger; full batch staging; idempotent raw-item duplicates; record identity conflicts fail; failed batch preserves all logical state. |
| Y. Replay/causality | Frozen original release cutoff, strict-before default and inclusive opt-in; future estimates/corrections and same-time events cannot rewrite earlier results. |
| Z. Serialization/fingerprints | Canonical JSON, explicit nulls and domain-separated SHA-256 for observations, sources, consensus, guidance, surprises, historical rows, scales, histories and configuration; runtime latency/failure counters projected out. |
| AA. Configuration | Disabled default; typed history, scale/window, freshness, cutoff, pooling, relative and bounded payload/item settings. The added namespace changes configuration fingerprints across engine versions. |
| AB. Health/schema drift | Causal healthy/degraded/stale/unavailable/rate-limited/schema states, separate parse/normalization failures, explicit coverage and atomic drift rejection. |
| AC. Security | Standard-library data parsing, credential-free HTTPS references, no execution of provider text, no parser file/network access, bounded inputs and raw evidence validation. |

## Documentation and acceptance (AD–AN)

| Item | Result |
| --- | --- |
| AD. Documentation | Added expectations/consensus and surprise docs, runnable replay example; updated README, architecture, contracts, source/replay, corporate/earnings, official-source integration and the revised master roadmap. All six README Python examples execute; all local Markdown links resolve. |
| AE. Tests added | 216 additional collected cases covering numeric/schema/temporal validation, vintage causality, consensus origins/contributors/conflicts, actual corrections and previous roles, guidance, normalization compatibility/self/same-time exclusion, atomicity, determinism and existing-store integration. |
| AF. Full pytest | **502 passed**. All 286 pre-existing tests pass with the engine version assertion updated. |
| AG. Coverage | Branch collection enabled: **96.29197% overall**; **99.06025%** across new expectations/estimate provider packages. Overall covered branches: 1,871 / 2,008. The configured 95% combined statement/branch coverage gate passes. |
| AH. Ruff | Lint passes; all 125 checked files pass format checks. |
| AI. mypy | Strict configuration passes all 91 source files. |
| AJ. Diff review | Tracked diff and new implementation/docs/tests reviewed; `git diff --check` and new-file whitespace checks pass. Working tree intentionally contains EIE-5 changes. |
| AK. Secret/TODO scan | Credential/private-key pattern scan found no credentials. No actionable TODO/FIXME/XXX markers; the sole master-spec match names the mandatory review gate. Credential rejection fixtures contain synthetic negative examples. |
| AL. Benchmark | Not run; this phase makes no latency/throughput performance claim. |
| AM. Versions | Engine/package `0.6.0`; expectations `1.0`; existing schemas unchanged at `1.0`. |
| AN. Dependencies | No new runtime or development dependencies. Existing tooling was used without modifying another repository. |

The **entire 502-test suite also passes with socket creation, connection helpers,
and DNS lookup blocked**. The standalone example yields release consensus 2.45,
actual 2.50, raw surprise approximately 0.05, five strictly prior releases, MAD
scale 2.9652 and normalized surprise approximately 0.016862, preserving earlier bytes.

## AO. Remaining limitations

Synthetic interchange data only; no live vendor transport or native commercial
schema claims. Persistence is in-memory; upstream store/registry copies are not
automatically synchronized. Contributor identities are provider-local. No FX,
decimal accounting guarantee, durable restart recovery, text extraction, event
studies, market reaction analysis, trading logic or MCE/AME integration. Existing
EIE-4 symbol-change effective-time correction limitation remains in place.

## AP. Exact proposed EIE-6 scope

**Live Provider Transport & Ingestion Infrastructure:** real HTTP transport;
polling; streaming abstraction where useful; rate limits; bounded retries/backoff;
ingestion queues; per-provider concurrency; health; checkpoints/cursors; restart
recovery; persistent source state; ingestion latency; atomic writes; continuous
official/provider data acquisition. Preserve existing availability, raw evidence,
atomicity, idempotency and replay contracts. No EIE-6 code is included in this change.
