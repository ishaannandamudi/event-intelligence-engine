"""Versioned internal state layouts for existing engines; no executable payloads."""

from collections.abc import Callable, Iterator
from dataclasses import fields, is_dataclass
from typing import cast

from eie.config.settings import Settings
from eie.core.errors import EIEError, PersistenceError
from eie.core.serialization import JSONValue, canonical_json_bytes
from eie.corporate.index import CorporateEventIndex
from eie.entities.registry import InMemoryEntityRegistry
from eie.entities.sec import SecEntityRegistry
from eie.expectations.engine import ExpectationsEngine
from eie.expectations.history import ObservationLedger
from eie.filings.store import InMemoryFilingStore
from eie.persistence import codec
from eie.persistence.memory import InMemoryEventStore
from eie.providers.common.ingestion import OfficialSourceIngestor
from eie.providers.corporate.adapter import CorporateIngestor
from eie.providers.estimates.adapter import EstimateIngestor
from eie.providers.sec.adapter import SecIngestor, _SecState
from eie.scheduled.coverage import CoverageLedger
from eie.scheduled.index import ScheduledEventIndex

Session = SecIngestor | CorporateIngestor | EstimateIngestor | OfficialSourceIngestor
LAYOUT_VERSION = "1.0"
LAYOUTS: dict[type[object], tuple[str, ...]] = {
    InMemoryEventStore: ("_observations", "_events"),
    _SecState: ("registry", "filings", "events", "history_files"),
    InMemoryEntityRegistry: ("_issuers", "_securities", "_listings"),
    SecEntityRegistry: (
        "_filers",
        "_issuers",
        "_cik_to_issuer",
        "_securities",
        "_listings",
        "_ticker_maps",
    ),
    InMemoryFilingStore: ("_by_id", "_by_accession"),
    CoverageLedger: ("_records",),
    ScheduledEventIndex: ("settings", "store", "coverage", "_revisions", "_times", "_first_known"),
    CorporateEventIndex: ("settings", "event_store", "registry", "_coverage", "_events"),
    ObservationLedger: ("_streams",),
    ExpectationsEngine: (
        "settings",
        "event_store",
        "registry",
        "ledger",
        "_events",
        "_coverage",
        "_health",
    ),
    SecIngestor: ("settings", "_state", "_diagnostics"),
    CorporateIngestor: ("settings", "_index", "_diagnostics", "_claim_ids", "_last_attempt_ns"),
    EstimateIngestor: ("_engine", "_diagnostics", "_record_ids", "_attempts"),
    OfficialSourceIngestor: ("settings", "index", "_diagnostics"),
}
NAMES = {f"{cls.__module__}.{cls.__name__}": cls for cls in LAYOUTS}


def encode(value: object) -> JSONValue:
    cls = type(value)
    if cls in LAYOUTS:
        return {
            "state": f"{cls.__module__}.{cls.__name__}",
            "fields": {name: encode(getattr(value, name)) for name in LAYOUTS[cls]},
        }
    return codec.pack(value, encode)


def decode(value: object) -> object:
    if not isinstance(value, dict) or "state" not in value:
        return codec.unpack(value, decode)
    if (
        set(value) != {"state", "fields"}
        or not isinstance(value["state"], str)
        or value["state"] not in NAMES
    ):
        raise PersistenceError("unsupported engine state layout")
    cls = NAMES[value["state"]]
    data = value["fields"]
    if not isinstance(data, dict) or set(data) != set(LAYOUTS[cls]):
        raise PersistenceError("engine state fields mismatch")
    state = {name: decode(item) for name, item in data.items()}
    constructor = cast(Callable[..., object], cls)
    instance: object
    if cls is OfficialSourceIngestor:
        index = state["index"]
        if not isinstance(index, ScheduledEventIndex):
            raise PersistenceError("invalid stored scheduled index")
        instance = OfficialSourceIngestor(index)
    elif "settings" in LAYOUTS[cls]:
        settings = state["settings"]
        if not isinstance(settings, Settings):
            raise PersistenceError("invalid stored domain configuration")
        instance = constructor(settings)
    elif cls is EstimateIngestor:
        engine = state["_engine"]
        if not isinstance(engine, ExpectationsEngine):
            raise PersistenceError("invalid stored expectations engine")
        instance = EstimateIngestor(engine)
    else:
        instance = constructor()
    for name, item in state.items():
        original = getattr(instance, name)
        if original is None:
            valid = item is None or (
                isinstance(item, int) and not isinstance(item, bool) and item >= 0
            )
        else:
            valid = type(item) is type(original)
        if not valid:
            raise PersistenceError("stored engine field type mismatch")
        setattr(instance, name, item)
    return instance


def dumps(session: Session) -> bytes:
    return canonical_json_bytes(encode(session))


def loads(payload: bytes) -> Session:
    try:
        value = decode(codec.strict_json(payload))
        if not isinstance(
            value, (SecIngestor, CorporateIngestor, EstimateIngestor, OfficialSourceIngestor)
        ):
            raise PersistenceError("stored root is not a domain session")
        return value
    except (ValueError, TypeError, KeyError, AttributeError, EIEError, RecursionError) as exc:
        raise PersistenceError("invalid domain session") from exc


def contracts(value: object) -> Iterator[object]:
    """Traverse only declared layouts and immutable contracts, preserving aliases."""
    visited: set[int] = set()

    def walk(item: object) -> Iterator[object]:
        if id(item) in visited:
            return
        visited.add(id(item))
        if type(item) in LAYOUTS:
            for name in LAYOUTS[type(item)]:
                yield from walk(getattr(item, name))
        elif is_dataclass(item) and not isinstance(item, type):
            yield item
            for f in fields(item):
                yield from walk(getattr(item, f.name))
        elif isinstance(item, dict):
            for key, child in item.items():
                yield from walk(key)
                yield from walk(child)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                yield from walk(child)

    yield from walk(value)
