"""Point-in-time SEC issuer, security, listing, and ticker association history."""

import re
from dataclasses import dataclass

from eie.core.errors import PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.schema import ENTITY_SCHEMA_VERSION, require_schema
from eie.core.time import require_ns
from eie.entities.models import ISSUER_ID, SECURITY_ID, Security, issuer_id
from eie.entities.protocols import ResolutionResult, ResolutionState
from eie.providers.sec.models import CIK, SecTickerMap

TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
EXCHANGE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,19}$")


def sec_issuer_id(cik: CIK) -> str:
    return issuer_id("SEC_CIK", cik.digits)


def sec_filer_id(cik: CIK) -> str:
    return "flr_" + fingerprint("eie:sec-filer-id:v1", cik)


@dataclass(frozen=True, slots=True)
class SecFilerRevision:
    filer_id: str
    cik: CIK
    name: str
    aliases: tuple[str, ...]
    available_from_ns: int
    source_observation_id: str
    schema_version: str = ENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, ENTITY_SCHEMA_VERSION)
        if not isinstance(self.cik, CIK) or self.filer_id != sec_filer_id(self.cik):
            raise ValidationError("filer ID must derive from CIK")
        if not isinstance(self.name, str) or not self.name or self.name != self.name.strip():
            raise ValidationError("SEC filer name required")
        if self.aliases != tuple(sorted(set(self.aliases))):
            raise ValidationError("SEC filer aliases must be sorted unique")
        require_ns(self.available_from_ns, "available_from_ns")
        if not self.source_observation_id.startswith("obs_"):
            raise ValidationError("SEC filer requires raw evidence")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:sec-filer:v1", self)


def sec_listing_id(security_id: str, ticker: str, exchange: str) -> str:
    if (
        not SECURITY_ID.fullmatch(security_id)
        or not TICKER.fullmatch(ticker)
        or not EXCHANGE.fullmatch(exchange)
    ):
        raise ValidationError("invalid stable SEC listing identity")
    return "lst_" + fingerprint("eie:sec-listing-id:v1", (security_id, ticker, exchange))


@dataclass(frozen=True, slots=True)
class SecIssuerRevision:
    issuer_id: str
    cik: CIK
    legal_name: str
    aliases: tuple[str, ...]
    available_from_ns: int
    source_observation_id: str
    schema_version: str = ENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, ENTITY_SCHEMA_VERSION)
        if not isinstance(self.cik, CIK) or self.issuer_id != sec_issuer_id(self.cik):
            raise ValidationError("issuer ID must derive from CIK")
        if (
            not isinstance(self.legal_name, str)
            or not self.legal_name
            or self.legal_name != self.legal_name.strip()
        ):
            raise ValidationError("issuer legal name required")
        if self.aliases != tuple(sorted(set(self.aliases))) or any(
            not isinstance(name, str) or not name or name != name.strip() for name in self.aliases
        ):
            raise ValidationError("issuer aliases must be sorted unique names")
        require_ns(self.available_from_ns, "available_from_ns")
        if not self.source_observation_id.startswith("obs_"):
            raise ValidationError("SEC issuer requires raw evidence")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:sec-issuer:v1", self)


@dataclass(frozen=True, slots=True)
class SecListingRevision:
    listing_id: str
    issuer_id: str
    security_id: str
    ticker: str
    exchange: str
    valid_from_ns: int
    valid_to_ns: int | None
    available_from_ns: int
    source_observation_id: str
    schema_version: str = ENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_schema(self.schema_version, ENTITY_SCHEMA_VERSION)
        if (
            not ISSUER_ID.fullmatch(self.issuer_id)
            or not SECURITY_ID.fullmatch(self.security_id)
            or self.listing_id != sec_listing_id(self.security_id, self.ticker, self.exchange)
        ):
            raise ValidationError("invalid SEC listing identity")
        require_ns(self.valid_from_ns, "valid_from_ns")
        require_ns(self.available_from_ns, "available_from_ns")
        if self.valid_to_ns is not None:
            require_ns(self.valid_to_ns, "valid_to_ns")
            if self.valid_to_ns <= self.valid_from_ns:
                raise TemporalConsistencyError("listing interval must be positive")
        if not self.source_observation_id.startswith("obs_"):
            raise ValidationError("SEC listing requires raw evidence")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:sec-listing:v1", self)


@dataclass(frozen=True, slots=True)
class SecTickerMapRevision:
    mapping: SecTickerMap
    observed_at_ns: int
    available_from_ns: int
    source_observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_ns(self.observed_at_ns, "observed_at_ns")
        require_ns(self.available_from_ns, "available_from_ns")
        if self.observed_at_ns > self.available_from_ns:
            raise TemporalConsistencyError("ticker map observation follows knowledge")
        if any(not item.startswith("obs_") for item in self.source_observation_ids):
            raise ValidationError("ticker map requires raw evidence")


class SecEntityRegistry:
    """No fuzzy matching; public ticker maps do not imply security identity."""

    def __init__(self) -> None:
        self._filers: dict[str, list[SecFilerRevision]] = {}
        self._issuers: dict[str, list[SecIssuerRevision]] = {}
        self._cik_to_issuer: dict[str, str] = {}
        self._securities: dict[str, Security] = {}
        self._listings: dict[str, list[SecListingRevision]] = {}
        self._ticker_maps: list[SecTickerMapRevision] = []

    def add_filer(self, revision: SecFilerRevision) -> bool:
        history = self._filers.setdefault(revision.filer_id, [])
        if history:
            last = history[-1]
            if (last.name, last.aliases) == (revision.name, revision.aliases):
                return False
            if revision.available_from_ns <= last.available_from_ns:
                raise TemporalConsistencyError("filer history must advance in knowledge time")
        history.append(revision)
        return True

    def filer_by_cik(self, cik: CIK, as_of_ns: int) -> SecFilerRevision | None:
        require_ns(as_of_ns, "as_of_ns")
        history = self._filers.get(sec_filer_id(cik), ())
        return next(
            (item for item in reversed(history) if item.available_from_ns <= as_of_ns), None
        )

    def add_issuer(self, revision: SecIssuerRevision) -> bool:
        previous_id = self._cik_to_issuer.get(revision.cik.digits)
        if previous_id is not None and previous_id != revision.issuer_id:
            raise PersistenceError("CIK mapped to conflicting issuer")
        history = self._issuers.setdefault(revision.issuer_id, [])
        if history:
            last = history[-1]
            if (last.legal_name, last.aliases) == (revision.legal_name, revision.aliases):
                return False
            if revision.available_from_ns <= last.available_from_ns:
                raise TemporalConsistencyError("issuer history must advance in knowledge time")
        history.append(revision)
        self._cik_to_issuer[revision.cik.digits] = revision.issuer_id
        return True

    def issuer_by_cik(self, cik: CIK, as_of_ns: int) -> SecIssuerRevision | None:
        require_ns(as_of_ns, "as_of_ns")
        history = self._issuers.get(sec_issuer_id(cik), ())
        return next(
            (item for item in reversed(history) if item.available_from_ns <= as_of_ns), None
        )

    def issuer_by_id(self, identity: str, as_of_ns: int) -> SecIssuerRevision | None:
        require_ns(as_of_ns, "as_of_ns")
        history = self._issuers.get(identity, ())
        return next(
            (item for item in reversed(history) if item.available_from_ns <= as_of_ns), None
        )

    def add_security(self, security: Security) -> bool:
        if not ISSUER_ID.fullmatch(security.issuer_id) or security.issuer_id not in self._issuers:
            raise PersistenceError("security requires known issuer")
        previous = self._securities.get(security.security_id)
        if previous is not None:
            if previous != security:
                raise PersistenceError("conflicting security identity")
            return False
        self._securities[security.security_id] = security
        return True

    def securities_by_issuer(self, identity: str) -> tuple[Security, ...]:
        return tuple(
            sorted(
                (item for item in self._securities.values() if item.issuer_id == identity),
                key=lambda item: item.security_id,
            )
        )

    def add_listing(self, revision: SecListingRevision) -> bool:
        security = self._securities.get(revision.security_id)
        if security is None or security.issuer_id != revision.issuer_id:
            raise PersistenceError("listing security/issuer relationship is unknown")
        history = self._listings.setdefault(revision.listing_id, [])
        if history:
            last = history[-1]
            if last == revision:
                return False
            if revision.available_from_ns <= last.available_from_ns:
                raise TemporalConsistencyError("listing revision knowledge must increase")
            if revision.valid_from_ns != last.valid_from_ns or revision.valid_to_ns is None:
                raise PersistenceError("listing update may only close/correct its interval")
        history.append(revision)
        return True

    def listings_at(
        self, issuer: str, as_of_ns: int, valid_at_ns: int
    ) -> tuple[SecListingRevision, ...]:
        require_ns(as_of_ns, "as_of_ns")
        require_ns(valid_at_ns, "valid_at_ns")
        selected = []
        for history in self._listings.values():
            item = next(
                (row for row in reversed(history) if row.available_from_ns <= as_of_ns), None
            )
            if (
                item is not None
                and item.issuer_id == issuer
                and item.valid_from_ns <= valid_at_ns
                and (item.valid_to_ns is None or valid_at_ns < item.valid_to_ns)
            ):
                selected.append(item)
        return tuple(sorted(selected, key=lambda item: item.listing_id))

    def add_ticker_map(self, revision: SecTickerMapRevision) -> bool:
        if self._ticker_maps:
            last = self._ticker_maps[-1]
            if last.mapping == revision.mapping:
                return False
            if revision.available_from_ns <= last.available_from_ns:
                raise TemporalConsistencyError("SEC ticker map knowledge must increase")
        self._ticker_maps.append(revision)
        return True

    def symbols_for_issuer(
        self, issuer: str, as_of_ns: int, valid_at_ns: int
    ) -> tuple[tuple[str, str], ...]:
        require_ns(as_of_ns, "as_of_ns")
        require_ns(valid_at_ns, "valid_at_ns")
        symbols = {
            (item.ticker, item.exchange) for item in self.listings_at(issuer, as_of_ns, valid_at_ns)
        }
        snapshot = next(
            (
                item
                for item in reversed(self._ticker_maps)
                if item.available_from_ns <= as_of_ns and item.observed_at_ns <= valid_at_ns
            ),
            None,
        )
        if snapshot is not None:
            symbols.update(
                (row[2], row[3]) for row in snapshot.mapping.rows if sec_issuer_id(row[0]) == issuer
            )
        return tuple(sorted(symbols))

    def resolve_symbol(
        self, ticker: str, exchange: str, as_of_ns: int, *, valid_at_ns: int | None = None
    ) -> ResolutionResult:
        require_ns(as_of_ns, "as_of_ns")
        valid_at = as_of_ns if valid_at_ns is None else require_ns(valid_at_ns, "valid_at_ns")
        if not TICKER.fullmatch(ticker) or not EXCHANGE.fullmatch(exchange):
            raise ValidationError("symbol query requires exact ticker and exchange")
        candidates = {
            item.issuer_id
            for issuer in self._issuers
            for item in self.listings_at(issuer, as_of_ns, valid_at)
            if item.ticker == ticker and item.exchange == exchange
        }
        snapshot = next(
            (
                item
                for item in reversed(self._ticker_maps)
                if item.available_from_ns <= as_of_ns and item.observed_at_ns <= valid_at
            ),
            None,
        )
        if snapshot is not None:
            candidates.update(
                sec_issuer_id(row[0])
                for row in snapshot.mapping.rows
                if row[2] == ticker
                and row[3] == exchange
                and self.issuer_by_cik(row[0], as_of_ns) is not None
            )
        identities = tuple(sorted(candidates))
        if not identities:
            return ResolutionResult(
                ResolutionState.UNRESOLVED, (), "sec_exact_symbol", ("NO_VALID_MAPPING",)
            )
        if len(identities) > 1:
            return ResolutionResult(
                ResolutionState.AMBIGUOUS, identities, "sec_exact_symbol", ("MULTIPLE_ISSUERS",)
            )
        return ResolutionResult(ResolutionState.RESOLVED, identities, "sec_exact_symbol")

    def canonical_state_bytes(self) -> bytes:
        from eie.core.serialization import canonical_json_bytes

        return canonical_json_bytes(
            {
                "filers": [item for key in sorted(self._filers) for item in self._filers[key]],
                "issuers": [item for key in sorted(self._issuers) for item in self._issuers[key]],
                "securities": [self._securities[key] for key in sorted(self._securities)],
                "listings": [
                    item for key in sorted(self._listings) for item in self._listings[key]
                ],
                "ticker_maps": self._ticker_maps,
            }
        )
