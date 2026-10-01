"""Explicit SEC fetch/parse/normalize boundary with batch-safe in-memory commits."""

from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256

from eie.config.settings import Settings
from eie.core.errors import EIEError, PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.entities.protocols import ResolutionState
from eie.entities.sec import (
    SecEntityRegistry,
    SecFilerRevision,
    SecIssuerRevision,
    SecTickerMapRevision,
    sec_filer_id,
    sec_issuer_id,
)
from eie.filings.models import FilingQueryResult, FilingView, SymbolFilingQueryResult
from eie.filings.store import InMemoryFilingStore
from eie.persistence.memory import InMemoryEventStore
from eie.providers.common.errors import (
    ProviderParseError,
    ProviderRateLimitedError,
    ProviderSchemaMismatchError,
    ProviderTransportError,
)
from eie.providers.common.protocols import HttpTransport
from eie.providers.sec.models import (
    CIK,
    HistoryFileRef,
    SecResourceKind,
    SecSnapshot,
    SecTickerMap,
    validate_sec_url,
)
from eie.providers.sec.normalization import (
    ADAPTER_VERSION,
    filing_observation,
    issuer_observation,
    normalize_filing,
)
from eie.providers.sec.parser import TICKER_MAP_URL, parse_submissions, parse_ticker_map
from eie.sources.models import RawSourceObservation, SourceHealth, SourceHealthState, SourceIdentity
from eie.sources.trust import SourceClass


class SecCapability(StrEnum):
    ISSUER_METADATA = "ISSUER_METADATA"
    SUBMISSIONS_METADATA = "SUBMISSIONS_METADATA"
    RECENT_FILINGS = "RECENT_FILINGS"
    HISTORICAL_FILINGS = "HISTORICAL_FILINGS"
    TICKER_ASSOCIATIONS = "TICKER_ASSOCIATIONS"
    SOURCE_HEALTH = "SOURCE_HEALTH"


class SecFailureCategory(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    PARSE_ERROR = "PARSE_ERROR"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"
    UNRESOLVED_ISSUER = "UNRESOLVED_ISSUER"


@dataclass(frozen=True, slots=True)
class SecDiagnostics:
    as_of_ns: int
    items_seen: int
    raw_created: int
    issuers_created_or_updated: int
    filings_created_or_updated: int
    amendments_linked: int
    duplicates_suppressed: int
    unresolved_issuers: int
    health: SourceHealth
    error_category: SecFailureCategory | None = None

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if not isinstance(self.health, SourceHealth):
            raise ValidationError("SEC diagnostic health required")
        for name in (
            "items_seen",
            "raw_created",
            "issuers_created_or_updated",
            "filings_created_or_updated",
            "amendments_linked",
            "duplicates_suppressed",
            "unresolved_issuers",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError("SEC diagnostic counters must be nonnegative")


@dataclass(slots=True)
class _SecState:
    registry: SecEntityRegistry = field(default_factory=SecEntityRegistry)
    filings: InMemoryFilingStore = field(default_factory=InMemoryFilingStore)
    events: InMemoryEventStore = field(default_factory=InMemoryEventStore)
    history_files: dict[str, tuple[HistoryFileRef, int]] = field(default_factory=dict)


class SecAdapter:
    provider_id = "SEC_EDGAR"
    adapter_version = ADAPTER_VERSION

    def capabilities(self) -> frozenset[SecCapability]:
        return frozenset(SecCapability)

    def parse(
        self,
        payload: bytes,
        source_url: str,
        *,
        expected_cik: CIK | None = None,
        kind: SecResourceKind = SecResourceKind.SUBMISSIONS,
    ) -> SecSnapshot:
        return parse_submissions(payload, source_url, expected_cik=expected_cik, kind=kind)

    def parse_ticker_map(self, payload: bytes) -> SecTickerMap:
        return parse_ticker_map(payload)


class SecIngestor:
    """Stage all SEC writes on a copy; publish one state pointer after validation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._state = _SecState()
        self._diagnostics: list[SecDiagnostics] = []

    @property
    def registry(self) -> SecEntityRegistry:
        return self._state.registry

    @property
    def filing_store(self) -> InMemoryFilingStore:
        return self._state.filings

    @property
    def event_store(self) -> InMemoryEventStore:
        return self._state.events

    def diagnostics(self) -> tuple[SecDiagnostics, ...]:
        return tuple(self._diagnostics)

    def health_as_of(self, as_of_ns: int) -> SourceHealth:
        require_ns(as_of_ns, "as_of_ns")
        latest = next(
            (item for item in reversed(self._diagnostics) if item.as_of_ns <= as_of_ns), None
        )
        if latest is None:
            return SourceHealth(SourceHealthState.UNKNOWN)
        health = latest.health
        if (
            health.state is SourceHealthState.HEALTHY
            and health.last_success_ns is not None
            and (as_of_ns - health.last_success_ns > self.settings.sec.staleness_ns)
        ):
            return replace(health, state=SourceHealthState.STALE)
        return health

    def _validate_attempt(self, payload: bytes, observed_ns: int, ingested_ns: int) -> None:
        if not self.settings.sec.enabled:
            raise ValidationError("SEC provider is disabled")
        require_ns(observed_ns, "source_observed_at_ns")
        require_ns(ingested_ns, "ingested_at_ns")
        if observed_ns > ingested_ns:
            raise TemporalConsistencyError("SEC observation follows ingestion")
        if len(payload) > self.settings.sec.max_payload_bytes:
            raise ValidationError("SEC payload exceeds configured maximum")
        if self._diagnostics and ingested_ns <= self._diagnostics[-1].as_of_ns:
            raise TemporalConsistencyError("SEC attempts must advance in knowledge time")

    def _record(self, diagnostic: SecDiagnostics) -> SecDiagnostics:
        self._diagnostics.append(diagnostic)
        return diagnostic

    def _failure(self, as_of_ns: int, category: SecFailureCategory) -> SecDiagnostics:
        previous = self.health_as_of(as_of_ns)
        state = {
            SecFailureCategory.UNAVAILABLE: SourceHealthState.UNAVAILABLE,
            SecFailureCategory.RATE_LIMITED: SourceHealthState.RATE_LIMITED,
            SecFailureCategory.SCHEMA_MISMATCH: SourceHealthState.SCHEMA_MISMATCH,
            SecFailureCategory.PARSE_ERROR: SourceHealthState.DEGRADED,
            SecFailureCategory.NORMALIZATION_ERROR: SourceHealthState.DEGRADED,
            SecFailureCategory.UNRESOLVED_ISSUER: SourceHealthState.DEGRADED,
        }[category]
        health = SourceHealth(
            state,
            last_success_ns=previous.last_success_ns,
            last_attempt_ns=as_of_ns,
            parse_failures=previous.parse_failures
            + int(category in (SecFailureCategory.SCHEMA_MISMATCH, SecFailureCategory.PARSE_ERROR)),
            error_category=category.value,
        )
        return self._record(SecDiagnostics(as_of_ns, 0, 0, 0, 0, 0, 0, 0, health, category))

    def report_failure(self, as_of_ns: int, category: SecFailureCategory) -> SecDiagnostics:
        if not self.settings.sec.enabled:
            raise ValidationError("SEC provider is disabled")
        require_ns(as_of_ns, "as_of_ns")
        if self._diagnostics and as_of_ns <= self._diagnostics[-1].as_of_ns:
            raise TemporalConsistencyError("SEC attempts must advance in knowledge time")
        return self._failure(as_of_ns, category)

    @staticmethod
    def _append_raw(
        state: _SecState, candidate: RawSourceObservation
    ) -> tuple[RawSourceObservation, bool]:
        prior = state.events.get_observation(candidate.observation_id)
        if prior is not None:
            return prior, False
        state.events.append_observation(candidate)
        return candidate, True

    def ingest_payload(
        self,
        payload: bytes,
        source_url: str,
        observed_ns: int,
        ingested_ns: int,
        *,
        expected_cik: CIK | None = None,
        kind: SecResourceKind = SecResourceKind.SUBMISSIONS,
    ) -> SecDiagnostics:
        self._validate_attempt(payload, observed_ns, ingested_ns)
        try:
            snapshot = parse_submissions(payload, source_url, expected_cik=expected_cik, kind=kind)
        except ProviderParseError:
            return self._failure(ingested_ns, SecFailureCategory.PARSE_ERROR)
        except (ProviderSchemaMismatchError, ValidationError):
            return self._failure(ingested_ns, SecFailureCategory.SCHEMA_MISMATCH)
        if len(snapshot.filings) > self.settings.sec.max_filings_per_ingest:
            raise ValidationError("SEC filing batch exceeds configured maximum")
        staged = deepcopy(self._state)
        raw_count = issuer_count = filing_count = links = duplicates = unresolved = 0
        try:
            if snapshot.company_name is not None:
                issuer_raw, created = self._append_raw(
                    staged, issuer_observation(snapshot, observed_ns, ingested_ns)
                )
                raw_count += int(created)
                known_filer = staged.registry.filer_by_cik(snapshot.cik, ingested_ns)
                filer_aliases = set(snapshot.aliases)
                if known_filer is not None:
                    filer_aliases.update(known_filer.aliases)
                    if known_filer.name != snapshot.company_name:
                        filer_aliases.add(known_filer.name)
                staged.registry.add_filer(
                    SecFilerRevision(
                        sec_filer_id(snapshot.cik),
                        snapshot.cik,
                        snapshot.company_name,
                        tuple(sorted(filer_aliases)),
                        ingested_ns,
                        issuer_raw.observation_id,
                    )
                )
                known_issuer = staged.registry.issuer_by_cik(snapshot.cik, ingested_ns)
                if known_issuer is not None or (
                    self.settings.sec.allow_issuer_bootstrap and snapshot.entity_type == "operating"
                ):
                    aliases = set(snapshot.aliases)
                    if known_issuer is not None:
                        aliases.update(known_issuer.aliases)
                        if known_issuer.legal_name != snapshot.company_name:
                            aliases.add(known_issuer.legal_name)
                    issuer_count += int(
                        staged.registry.add_issuer(
                            SecIssuerRevision(
                                sec_issuer_id(snapshot.cik),
                                snapshot.cik,
                                snapshot.company_name,
                                tuple(sorted(aliases)),
                                ingested_ns,
                                issuer_raw.observation_id,
                            )
                        )
                    )
            for item in sorted(
                snapshot.filings, key=lambda row: (row.accepted_at_ns, row.accession.formatted)
            ):
                raw, created = self._append_raw(
                    staged, filing_observation(item, observed_ns, ingested_ns)
                )
                raw_count += int(created)
                filer = staged.registry.filer_by_cik(item.cik, ingested_ns)
                issuer_cik = item.issuer_cik or (item.cik if item.form.base != "4" else None)
                issuer = (
                    staged.registry.issuer_by_cik(issuer_cik, ingested_ns)
                    if issuer_cik is not None
                    else None
                )
                if filer is None or (issuer is None and item.form.base != "4"):
                    unresolved += 1
                    continue
                normalized = normalize_filing(
                    item, raw, filer, issuer, staged.filings, observed_ns, ingested_ns
                )
                if normalized is None:
                    duplicates += 1
                    continue
                metadata, event = normalized
                staged.filings.append(metadata)
                staged.events.append_event(event)
                filing_count += 1
                links += int(metadata.amends_filing_id is not None)
            for ref in snapshot.history_files:
                previous = staged.history_files.get(ref.name)
                if previous is not None and previous[0] != ref:
                    raise PersistenceError("SEC history file reference changed at stable name")
                if previous is None:
                    staged.history_files[ref.name] = (ref, ingested_ns)
        except EIEError:
            return self._failure(ingested_ns, SecFailureCategory.NORMALIZATION_ERROR)
        self._state = staged
        prior = self.health_as_of(ingested_ns)
        health = SourceHealth(
            SourceHealthState.DEGRADED if unresolved else SourceHealthState.HEALTHY,
            last_success_ns=ingested_ns,
            last_attempt_ns=ingested_ns,
            parse_failures=prior.parse_failures,
            error_category=SecFailureCategory.UNRESOLVED_ISSUER.value if unresolved else None,
        )
        return self._record(
            SecDiagnostics(
                ingested_ns,
                len(snapshot.filings),
                raw_count,
                issuer_count,
                filing_count,
                links,
                duplicates,
                unresolved,
                health,
                SecFailureCategory.UNRESOLVED_ISSUER if unresolved else None,
            )
        )

    def ingest_ticker_map(
        self, payload: bytes, observed_ns: int, ingested_ns: int
    ) -> SecDiagnostics:
        self._validate_attempt(payload, observed_ns, ingested_ns)
        try:
            mapping = parse_ticker_map(payload)
        except ProviderParseError:
            return self._failure(ingested_ns, SecFailureCategory.PARSE_ERROR)
        except (ProviderSchemaMismatchError, ValidationError):
            return self._failure(ingested_ns, SecFailureCategory.SCHEMA_MISMATCH)
        if len(mapping.rows) > self.settings.sec.max_filings_per_ingest:
            raise ValidationError("SEC ticker-map batch exceeds configured maximum")
        staged = deepcopy(self._state)
        raw_count = issuer_count = 0
        ids = []
        try:
            for cik, name, ticker, exchange in mapping.rows:
                row = (cik.digits, name, ticker, exchange)
                raw, created = self._append_raw(
                    staged,
                    RawSourceObservation(
                        SourceIdentity(
                            "SEC_EDGAR",
                            "sec-ticker-exchange-json-v1",
                            ADAPTER_VERSION,
                            SourceClass.OFFICIAL_PRIMARY,
                        ),
                        f"CIK{cik.digits}:{ticker}:{exchange}",
                        "1",
                        sha256(canonical_json_bytes(row)).hexdigest(),
                        "application/json",
                        observed_ns,
                        ingested_ns,
                        source_reference=TICKER_MAP_URL,
                        raw_entity_identifiers=(cik.digits,),
                        metadata=(("sec_schema", "sec-ticker-exchange-json-v1"),),
                    ),
                )
                ids.append(raw.observation_id)
                raw_count += int(created)
                known = staged.registry.issuer_by_cik(cik, ingested_ns)
                known_filer = staged.registry.filer_by_cik(cik, ingested_ns)
                filer_aliases = set(known_filer.aliases) if known_filer is not None else set()
                if known_filer is not None and known_filer.name != name:
                    filer_aliases.add(known_filer.name)
                staged.registry.add_filer(
                    SecFilerRevision(
                        sec_filer_id(cik),
                        cik,
                        name,
                        tuple(sorted(filer_aliases)),
                        ingested_ns,
                        raw.observation_id,
                    )
                )
                if known is not None or self.settings.sec.allow_issuer_bootstrap:
                    aliases = set(known.aliases) if known is not None else set()
                    if known is not None and known.legal_name != name:
                        aliases.add(known.legal_name)
                    issuer_count += int(
                        staged.registry.add_issuer(
                            SecIssuerRevision(
                                sec_issuer_id(cik),
                                cik,
                                name,
                                tuple(sorted(aliases)),
                                ingested_ns,
                                raw.observation_id,
                            )
                        )
                    )
            staged.registry.add_ticker_map(
                SecTickerMapRevision(mapping, observed_ns, ingested_ns, tuple(sorted(set(ids))))
            )
        except EIEError:
            return self._failure(ingested_ns, SecFailureCategory.NORMALIZATION_ERROR)
        self._state = staged
        prior = self.health_as_of(ingested_ns)
        health = SourceHealth(
            SourceHealthState.HEALTHY,
            last_success_ns=ingested_ns,
            last_attempt_ns=ingested_ns,
            parse_failures=prior.parse_failures,
        )
        return self._record(
            SecDiagnostics(
                ingested_ns,
                len(mapping.rows),
                raw_count,
                issuer_count,
                0,
                0,
                len(mapping.rows) - raw_count,
                0,
                health,
            )
        )

    def fetch_and_ingest(
        self,
        url: str,
        transport: HttpTransport,
        observed_ns: int,
        ingested_ns: int,
        *,
        expected_cik: CIK | None = None,
        kind: SecResourceKind = SecResourceKind.SUBMISSIONS,
    ) -> SecDiagnostics:
        if not self.settings.sec.enabled:
            raise ValidationError("SEC provider is disabled")
        require_ns(observed_ns, "source_observed_at_ns")
        require_ns(ingested_ns, "ingested_at_ns")
        if observed_ns > ingested_ns:
            raise TemporalConsistencyError("SEC observation follows ingestion")
        validate_sec_url(url)
        user_agent = self.settings.sec.user_agent
        if user_agent is None or "@" not in user_agent:
            raise ValidationError("SEC fetch requires configured identifying contact user agent")
        try:
            body = transport.get(
                url, timeout_seconds=self.settings.sec.timeout_seconds, user_agent=user_agent
            )
        except ProviderRateLimitedError:
            return self.report_failure(ingested_ns, SecFailureCategory.RATE_LIMITED)
        except ProviderTransportError:
            return self.report_failure(ingested_ns, SecFailureCategory.UNAVAILABLE)
        return self.ingest_payload(
            body, url, observed_ns, ingested_ns, expected_cik=expected_cik, kind=kind
        )

    def history_files_as_of(self, as_of_ns: int) -> tuple[HistoryFileRef, ...]:
        require_ns(as_of_ns, "as_of_ns")
        return tuple(
            sorted(
                (
                    ref
                    for ref, available in self._state.history_files.values()
                    if available <= as_of_ns
                ),
                key=lambda item: item.name,
            )
        )

    def _query(
        self,
        as_of_ns: int,
        *,
        issuer_id: str | None = None,
        cik: CIK | None = None,
        exact_form: str | None = None,
        base_form: str | None = None,
        amendment: bool | None = None,
        report_period: str | None = None,
    ) -> FilingQueryResult:
        items = self._state.filings.known_as_of(
            as_of_ns,
            issuer_id=issuer_id,
            cik=cik,
            exact_form=exact_form,
            base_form=base_form,
            amendment=amendment,
            report_period=report_period,
        )
        views = tuple(
            FilingView(
                item,
                ()
                if item.issuer_id is None
                else self._state.registry.symbols_for_issuer(
                    item.issuer_id, as_of_ns, item.accepted_at_ns
                ),
            )
            for item in items
        )
        return FilingQueryResult(as_of_ns, views, self.settings.fingerprint)

    def filings_for_issuer(
        self,
        issuer_id: str,
        as_of_ns: int,
        *,
        exact_form: str | None = None,
        base_form: str | None = None,
        amendment: bool | None = None,
        report_period: str | None = None,
    ) -> FilingQueryResult:
        return self._query(
            as_of_ns,
            issuer_id=issuer_id,
            exact_form=exact_form,
            base_form=base_form,
            amendment=amendment,
            report_period=report_period,
        )

    def filings_for_cik(
        self,
        cik: CIK,
        as_of_ns: int,
        *,
        exact_form: str | None = None,
        base_form: str | None = None,
        amendment: bool | None = None,
    ) -> FilingQueryResult:
        return self._query(
            as_of_ns, cik=cik, exact_form=exact_form, base_form=base_form, amendment=amendment
        )

    def filings_for_symbol(
        self, ticker: str, exchange: str, as_of_ns: int, valid_at_ns: int
    ) -> SymbolFilingQueryResult:
        resolution = self._state.registry.resolve_symbol(
            ticker, exchange, as_of_ns, valid_at_ns=valid_at_ns
        )
        query = (
            self._query(as_of_ns, issuer_id=resolution.candidate_ids[0])
            if (resolution.state is ResolutionState.RESOLVED)
            else FilingQueryResult(as_of_ns, (), self.settings.fingerprint)
        )
        return SymbolFilingQueryResult(valid_at_ns, resolution, query)

    def recent_filings(self, as_of_ns: int, limit: int = 10) -> FilingQueryResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("recent filing limit must be positive")
        result = self._query(as_of_ns)
        return FilingQueryResult(
            as_of_ns,
            tuple(reversed(result.filings[-limit:])),
            result.configuration_fingerprint,
        )

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "entities": self._state.registry.canonical_state_bytes().decode(),
                "filings": self._state.filings.canonical_state_bytes().decode(),
                "events": self._state.events.canonical_state_bytes().decode(),
                "history_files": [
                    self._state.history_files[key] for key in sorted(self._state.history_files)
                ],
            }
        )
