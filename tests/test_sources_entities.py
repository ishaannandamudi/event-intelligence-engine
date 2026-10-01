from dataclasses import replace

import pytest

from eie.core.errors import (
    PersistenceError,
    SchemaCompatibilityError,
    TemporalConsistencyError,
    ValidationError,
)
from eie.core.quality import QualityFlag
from eie.entities.models import Issuer, Listing, Security, issuer_id, security_id
from eie.entities.protocols import ResolutionResult, ResolutionState
from eie.entities.registry import InMemoryEntityRegistry
from eie.sources.models import (
    RawSourceObservation,
    SourceHealth,
    SourceHealthState,
    SourceIdentity,
    SourcePollResult,
)
from eie.sources.protocols import (
    EventNormalizer,
    EventSource,
    FetchingSource,
    PollingSource,
    ScheduledSource,
    SourceCapability,
)
from eie.sources.trust import SourceClass


def test_raw_provenance_and_health(raw: RawSourceObservation) -> None:
    assert raw.observation_id == replace(raw).observation_id
    assert raw.result_fingerprint == replace(raw).result_fingerprint
    assert raw.result_fingerprint != replace(raw, ingested_at_ns=102).result_fingerprint
    healthy_empty = SourcePollResult((), SourceHealth(SourceHealthState.HEALTHY))
    failed_empty = SourcePollResult((), SourceHealth(SourceHealthState.UNAVAILABLE))
    assert healthy_empty != failed_empty
    assert SourceHealth(SourceHealthState.DEGRADED, 1, 2, 1, 1, "PARSE").parse_failures == 1
    with pytest.raises(ValidationError):
        SourcePollResult((raw,), SourceHealth(SourceHealthState.UNAVAILABLE))
    with pytest.raises(ValidationError):
        SourceHealth(SourceHealthState.HEALTHY, parse_failures=-1)
    with pytest.raises(TemporalConsistencyError):
        SourceHealth(SourceHealthState.HEALTHY, last_success_ns=3, last_attempt_ns=2)
    with pytest.raises(ValidationError):
        SourceIdentity("", "1", "1", SourceClass.UNVERIFIED)
    with pytest.raises(ValidationError):
        SourceIdentity("name\nsecret", "1", "1", SourceClass.UNVERIFIED)
    with pytest.raises(ValidationError):
        SourceIdentity("name", "1", "1", "UNVERIFIED")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        replace(raw, raw_entity_identifiers=["A"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        replace(raw, metadata=(("bad", 1),))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        replace(raw, quality_flags=frozenset({"DUPLICATE"}))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SourceHealth("HEALTHY")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SourceHealth(SourceHealthState.HEALTHY, parse_failures=True)
    with pytest.raises(ValidationError):
        SourcePollResult([raw], SourceHealth(SourceHealthState.HEALTHY))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SourcePollResult(("bad",), SourceHealth(SourceHealthState.HEALTHY))  # type: ignore[arg-type]
    assert all(
        item is not None
        for item in (EventSource, EventNormalizer, FetchingSource, PollingSource, ScheduledSource)
    )
    assert SourceCapability.POLL.value == "POLL"
    assert replace(raw, published_at_ns=None).published_at_ns is None
    assert replace(raw, source_reference="https://example.test/item?id=1").source_reference


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"schema_version": "2.0"}, SchemaCompatibilityError),
        ({"payload_hash": "bad"}, ValidationError),
        ({"source_item_id": ""}, ValidationError),
        ({"source_observed_at_ns": 99}, TemporalConsistencyError),
        ({"ingested_at_ns": 99}, TemporalConsistencyError),
        ({"raw_entity_identifiers": ("Z", "A")}, ValidationError),
        ({"raw_entity_identifiers": ("",)}, ValidationError),
        ({"metadata": (("z", "1"), ("a", "2"))}, ValidationError),
        ({"metadata": (("", "1"),)}, ValidationError),
        ({"metadata": (("api_key", "secret"),)}, ValidationError),
        ({"source_reference": "https://user:pass@example.test/item"}, ValidationError),
        ({"source_reference": "https://example.test/item?token=secret"}, ValidationError),
    ],
)
def test_raw_rejects_invalid(
    raw: RawSourceObservation, changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(raw, **changes)


def test_entity_identity_and_exact_historical_resolution() -> None:
    iid = issuer_id("CIK", "0000123456")
    sid = security_id("FIGI", "BBG000TEST")
    assert issuer_id("CIK", "0000123456") == iid
    assert iid != issuer_id("CIK", "0000654321")
    assert sid != security_id("FIGI", "BBG000OTHER")
    with pytest.raises(ValidationError):
        issuer_id("TICKER", "ABC")
    with pytest.raises(ValidationError):
        security_id("NAME", "Example Inc")
    registry = InMemoryEntityRegistry()
    issuer = Issuer(iid, "Example Inc", "0000123456", ("Old Name",))
    security = Security(sid, iid, "Common Stock", "BBG000TEST")
    registry.add_issuer(issuer)
    registry.add_security(security)
    registry.add_listing(Listing(sid, "OLD", "XNYS", 10, 20, available_from_ns=11))
    registry.add_listing(Listing(sid, "OLD", "XNYS", 10, 20, available_from_ns=11))
    registry.add_listing(Listing(sid, "NEW", "XNAS", 20, available_from_ns=21))
    assert registry.resolve("OLD", 10).state is ResolutionState.UNRESOLVED
    assert registry.resolve("OLD", 15).candidate_ids == (sid,)
    assert registry.resolve("OLD", 20).state is ResolutionState.UNRESOLVED
    assert registry.resolve("OLD", 30, valid_at_ns=15).candidate_ids == (sid,)
    assert registry.resolve("NEW", 21).candidate_ids == (sid,)
    assert registry.resolve("UNKNOWN", 21).state is ResolutionState.UNRESOLVED
    sid2 = security_id("FIGI", "BBG000TEST2")
    registry.add_security(Security(sid2, iid, "Class B"))
    registry.add_listing(Listing(sid2, "NEW", "XNAS", 20))
    assert registry.resolve("NEW", 21).state is ResolutionState.AMBIGUOUS
    registry.add_issuer(issuer)
    registry.add_security(security)
    with pytest.raises(PersistenceError):
        registry.add_issuer(replace(issuer, legal_name="Different"))
    with pytest.raises(PersistenceError):
        registry.add_security(Security(security_id("X", "x"), issuer_id("CIK", "unknown"), "Stock"))
    with pytest.raises(PersistenceError):
        registry.add_security(replace(security, description="Conflicting"))
    with pytest.raises(PersistenceError):
        registry.add_listing(Listing(security_id("FIGI", "unknown"), "X", "XNAS", 1))


def test_entity_and_resolution_validation() -> None:
    iid = issuer_id("CIK", "0000123456")
    sid = security_id("FIGI", "X")
    with pytest.raises(ValidationError):
        Issuer(iid, "X", "1")
    with pytest.raises(ValidationError):
        Issuer("bad", "X")
    with pytest.raises(ValidationError):
        Issuer(iid, "X", aliases=("z", "a"))
    with pytest.raises(ValidationError):
        Issuer(iid, "X", aliases=["old"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Security("bad", iid, "Stock")
    with pytest.raises(TemporalConsistencyError):
        Listing(sid, "X", "XNYS", 10, 10)
    with pytest.raises(ValidationError):
        Listing(sid, "", "XNYS", 10)
    with pytest.raises(ValidationError):
        ResolutionResult(ResolutionState.RESOLVED, (), "exact")
    with pytest.raises(ValidationError):
        ResolutionResult(ResolutionState.UNRESOLVED, (sid,), "exact")
    with pytest.raises(ValidationError):
        ResolutionResult(ResolutionState.AMBIGUOUS, (sid,), "exact")
    with pytest.raises(ValidationError):
        ResolutionResult(ResolutionState.RESOLVED, (sid,), "")
    with pytest.raises(ValidationError):
        ResolutionResult("RESOLVED", (sid,), "exact")  # type: ignore[arg-type]
    assert (
        ResolutionResult(
            ResolutionState.CONFLICTING,
            ("a", "b"),
            "exact",
            quality_flags=frozenset({QualityFlag.CONFLICTING_SOURCES}),
        ).state
        is ResolutionState.CONFLICTING
    )
