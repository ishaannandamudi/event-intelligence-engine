from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from math import inf, nan

import pytest

from eie import __version__
from eie.config.settings import Settings
from eie.core.errors import SchemaCompatibilityError, TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.schema import CONFIG_SCHEMA_VERSION, EVENT_SCHEMA_VERSION, OBSERVATION_SCHEMA_VERSION
from eie.core.serialization import canonical_json_bytes, json_value
from eie.core.time import MAX_NS, datetime_to_ns, ns_to_datetime, require_ns
from eie.sources.trust import DEFAULT_PRECEDENCE, SourceClass, SourcePrecedencePolicy


def test_versions_and_time_exactness() -> None:
    assert __version__ == "0.4.0"
    assert (EVENT_SCHEMA_VERSION, OBSERVATION_SCHEMA_VERSION, CONFIG_SCHEMA_VERSION) == (
        "1.0",
        "1.0",
        "1.0",
    )
    instant = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-5)))
    ns = datetime_to_ns(instant)
    assert ns_to_datetime(ns) == instant.astimezone(UTC)
    assert datetime_to_ns(ns_to_datetime(ns)) == ns
    assert require_ns(MAX_NS) == MAX_NS
    for bad in (-1, MAX_NS + 1, True, 1.5):
        with pytest.raises(TemporalConsistencyError):
            require_ns(bad)  # type: ignore[arg-type]
    with pytest.raises(TemporalConsistencyError):
        datetime_to_ns(datetime(2026, 1, 1))
    with pytest.raises(TemporalConsistencyError):
        datetime_to_ns(datetime(1960, 1, 1, tzinfo=UTC))
    with pytest.raises(TemporalConsistencyError):
        ns_to_datetime(ns + 1)


@dataclass(frozen=True)
class Sample:
    z: frozenset[str]
    a: SourceClass


def test_canonical_serialization_and_hashes() -> None:
    value = Sample(frozenset({"é", "a"}), SourceClass.OFFICIAL_PRIMARY)
    encoded = canonical_json_bytes(value)
    assert encoded == b'{"a":"OFFICIAL_PRIMARY","z":["a","\xc3\xa9"]}'
    assert canonical_json_bytes({"b": None, "a": [1, True]}) == b'{"a":[1,true],"b":null}'
    assert canonical_json_bytes({"z": 1, "a": 2}) == canonical_json_bytes({"a": 2, "z": 1})
    assert fingerprint("domain", value) == fingerprint("domain", value)
    assert fingerprint("other", value) != fingerprint("domain", value)
    with pytest.raises(ValueError):
        fingerprint("", value)
    with pytest.raises(ValueError):
        fingerprint("bad\x00domain", value)
    for invalid in (nan, inf, -inf, object(), {1: "bad"}):
        with pytest.raises(ValidationError):
            json_value(invalid)


def test_immutable_fingerprinted_settings() -> None:
    a = Settings()
    assert a.fingerprint == Settings().fingerprint
    assert a.fingerprint != Settings(default_staleness_ns=1).fingerprint
    assert len(a.accepted_source_classes) == len(SourceClass)
    with pytest.raises(SchemaCompatibilityError):
        Settings(schema_version="2.0")
    with pytest.raises(SchemaCompatibilityError):
        Settings(event_schema_version="2.0")
    with pytest.raises(SchemaCompatibilityError):
        Settings(observation_schema_version="2.0")
    with pytest.raises(ValidationError):
        Settings(accepted_source_classes=frozenset())
    with pytest.raises(ValidationError):
        Settings(accepted_source_classes=frozenset({"OFFICIAL_PRIMARY"}))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(source_precedence={})  # type: ignore[arg-type]
    with pytest.raises(TemporalConsistencyError):
        Settings(default_staleness_ns=-1)
    with pytest.raises(ValidationError):
        Settings(strict_replay_availability=False)
    with pytest.raises(ValidationError):
        SourcePrecedencePolicy((SourceClass.OFFICIAL_PRIMARY,))
    with pytest.raises(ValidationError):
        SourcePrecedencePolicy(tuple(item.value for item in DEFAULT_PRECEDENCE))  # type: ignore[arg-type]
    fact_order = tuple(reversed(DEFAULT_PRECEDENCE))
    policy = SourcePrecedencePolicy(fact_overrides=(("earnings", fact_order),))
    assert policy.order_for("earnings") == fact_order
    assert policy.order_for("macro") == DEFAULT_PRECEDENCE
    with pytest.raises(ValidationError):
        SourcePrecedencePolicy(fact_overrides=(("z", fact_order), ("a", fact_order)))
    with pytest.raises(ValidationError):
        SourcePrecedencePolicy(fact_overrides=(("earnings", (SourceClass.UNVERIFIED,)),))
