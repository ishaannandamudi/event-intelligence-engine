"""Exact, time-bounded listing resolution for synthetic/offline use."""

from eie.core.errors import PersistenceError
from eie.core.time import require_ns
from eie.entities.models import Issuer, Listing, Security
from eie.entities.protocols import ResolutionResult, ResolutionState


class InMemoryEntityRegistry:
    def __init__(self) -> None:
        self._issuers: dict[str, Issuer] = {}
        self._securities: dict[str, Security] = {}
        self._listings: list[Listing] = []

    def add_issuer(self, issuer: Issuer) -> None:
        previous = self._issuers.get(issuer.issuer_id)
        if previous is not None and previous != issuer:
            raise PersistenceError("conflicting issuer identity")
        self._issuers[issuer.issuer_id] = issuer

    def add_security(self, security: Security) -> None:
        if security.issuer_id not in self._issuers:
            raise PersistenceError("unknown issuer")
        previous = self._securities.get(security.security_id)
        if previous is not None and previous != security:
            raise PersistenceError("conflicting security identity")
        self._securities[security.security_id] = security

    def add_listing(self, listing: Listing) -> None:
        if listing.security_id not in self._securities:
            raise PersistenceError("unknown security")
        if listing not in self._listings:
            self._listings.append(listing)

    def resolve(
        self, identifier: str, as_of_ns: int, valid_at_ns: int | None = None
    ) -> ResolutionResult:
        require_ns(as_of_ns, "as_of_ns")
        valid_at = as_of_ns if valid_at_ns is None else require_ns(valid_at_ns, "valid_at_ns")
        candidates = tuple(
            sorted(
                {
                    item.security_id
                    for item in self._listings
                    if item.ticker == identifier
                    and item.available_from_ns <= as_of_ns
                    and item.valid_from_ns <= valid_at
                    and (item.valid_to_ns is None or valid_at < item.valid_to_ns)
                }
            )
        )
        if not candidates:
            return ResolutionResult(
                ResolutionState.UNRESOLVED, (), "exact_listing", ("NO_VALID_LISTING",)
            )
        if len(candidates) > 1:
            return ResolutionResult(
                ResolutionState.AMBIGUOUS, candidates, "exact_listing", ("MULTIPLE_VALID_LISTINGS",)
            )
        return ResolutionResult(ResolutionState.RESOLVED, candidates, "exact_listing")
