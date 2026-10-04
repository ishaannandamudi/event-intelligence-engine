"""Allowlisted typed JSON, never imports or executes type names from stored data."""

import json
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from enum import Enum
from types import ModuleType
from typing import cast

from eie.analogues import models as analogue_models
from eie.analogues import schema as analogue_schema
from eie.archive import models as archive_models
from eie.backfill import models as backfill_models
from eie.config import settings as config
from eie.core import quality
from eie.core.errors import EIEError, PersistenceError
from eie.core.serialization import JSONValue, canonical_json_bytes
from eie.corporate import actions, earnings
from eie.corporate import models as corporate
from eie.corporate import queries as corporate_queries
from eie.corporate import settings as corporate_settings
from eie.corporate import types as corporate_types
from eie.datasets import models as dataset_models
from eie.entities import models as entities
from eie.entities import protocols as entity_protocols
from eie.entities import sec as registry
from eie.events import identity, lifecycle, revisions, scope, taxonomy
from eie.events import models as events
from eie.evidence import claims as evidence_claims
from eie.evidence import dedup as evidence_dedup
from eie.evidence import graph as evidence_graph
from eie.evidence import matching as evidence_matching
from eie.evidence import models as evidence_models
from eie.evidence import references as evidence_references
from eie.evidence import schema as evidence_schema
from eie.expectations import consensus, guidance, normalization, observations, queries, units
from eie.expectations import models as measurements
from eie.expectations import settings as expectations_settings
from eie.expectations import surprise as information_surprise
from eie.filings import models as filings
from eie.graph import claims as graph_claims
from eie.graph import exposure as graph_exposure
from eie.graph import history as graph_history
from eie.graph import models as graph_models
from eie.graph import nodes as graph_nodes
from eie.graph import propagation as graph_propagation
from eie.graph import taxonomy as graph_taxonomy
from eie.graph import validation as graph_validation
from eie.impact import inputs as impact_inputs
from eie.impact import models as impact_models
from eie.impact import sensitivity as impact_sensitivity
from eie.impact import validation as impact_validation
from eie.ingestion import models as operations
from eie.intelligence import models as intelligence_models
from eie.intelligence import policy as intelligence_policy
from eie.intelligence import schema as intelligence_schema
from eie.interactions import models as interaction_models
from eie.interactions import schema as interaction_schema
from eie.nlp import audit as text_audit
from eie.nlp import documents as text_documents
from eie.nlp import entities as text_entities
from eie.nlp import mentions as text_mentions
from eie.nlp import models as text_models
from eie.nlp import normalization as text_normalization
from eie.nlp import prompts as text_prompts
from eie.nlp import schema as text_schema
from eie.nlp import segmentation as text_segmentation
from eie.nlp import spans as text_spans
from eie.nlp import validation_models as text_validation_models
from eie.providers.common import ingestion as official_ingestion
from eie.providers.common import models as official
from eie.providers.common import settings as official_settings
from eie.providers.corporate import adapter as corporate_adapter
from eie.providers.estimates import adapter as estimates_adapter
from eie.providers.evidence import models as evidence_sources
from eie.providers.relationships import models as relationship_sources
from eie.providers.sec import adapter as sec_adapter
from eie.providers.sec import models as sec
from eie.providers.sec import settings as sec_settings
from eie.providers.text import models as text_sources
from eie.reliability import models as reliability_models
from eie.reliability import schema as reliability_schema
from eie.scheduled import coverage, windows
from eie.scheduled import models as schedules
from eie.service import models as service_models
from eie.service import schema as service_schema
from eie.sources import models as sources
from eie.sources import trust
from eie.studies import aggregation, decay, market
from eie.studies import models as study_models
from eie.studies import queries as study_queries
from eie.studies import validation as study_validation

MODULES: tuple[ModuleType, ...] = (
    service_models,
    service_schema,
    reliability_models,
    reliability_schema,
    interaction_models,
    interaction_schema,
    analogue_models,
    analogue_schema,
    intelligence_models,
    intelligence_policy,
    intelligence_schema,
    information_surprise,
    text_documents,
    text_entities,
    text_mentions,
    text_models,
    text_normalization,
    text_prompts,
    text_schema,
    text_segmentation,
    text_spans,
    text_validation_models,
    text_audit,
    text_sources,
    evidence_claims,
    evidence_dedup,
    evidence_graph,
    evidence_matching,
    evidence_models,
    evidence_references,
    evidence_schema,
    evidence_sources,
    graph_claims,
    graph_exposure,
    graph_history,
    graph_models,
    graph_nodes,
    graph_propagation,
    graph_taxonomy,
    graph_validation,
    relationship_sources,
    impact_inputs,
    impact_models,
    impact_sensitivity,
    impact_validation,
    market,
    study_models,
    aggregation,
    decay,
    study_queries,
    study_validation,
    archive_models,
    backfill_models,
    dataset_models,
    config,
    quality,
    actions,
    earnings,
    corporate,
    corporate_queries,
    corporate_settings,
    corporate_types,
    entities,
    entity_protocols,
    registry,
    identity,
    lifecycle,
    events,
    revisions,
    scope,
    taxonomy,
    consensus,
    guidance,
    measurements,
    normalization,
    observations,
    queries,
    expectations_settings,
    units,
    filings,
    official_ingestion,
    official,
    official_settings,
    corporate_adapter,
    estimates_adapter,
    sec_adapter,
    sec,
    sec_settings,
    coverage,
    schedules,
    windows,
    sources,
    trust,
    operations,
)
TYPES: dict[str, type[object]] = {
    f"{module.__name__}.{name}": value
    for module in MODULES
    for name, value in vars(module).items()
    if isinstance(value, type)
    and value.__module__ == module.__name__
    and (is_dataclass(value) or issubclass(value, Enum))
}


def pack(value: object, child: Callable[[object], JSONValue] | None = None) -> JSONValue:
    encode = child or pack
    if isinstance(value, Enum):
        key = f"{type(value).__module__}.{type(value).__name__}"
        if key not in TYPES:
            raise PersistenceError("unregistered persisted enum")
        return {"enum": key, "value": encode(value.value)}
    if is_dataclass(value) and not isinstance(value, type):
        key = f"{type(value).__module__}.{type(value).__name__}"
        if key not in TYPES:
            raise PersistenceError("unregistered persisted contract")
        return {
            "type": key,
            "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value) if f.init},
        }
    if isinstance(value, (tuple, list, frozenset, set)):
        kind = type(value).__name__
        items = [encode(v) for v in value]
        if isinstance(value, (frozenset, set)):
            items.sort(key=canonical_json_bytes)
        return {"collection": kind, "items": items}
    if isinstance(value, dict):
        pairs = [[encode(k), encode(v)] for k, v in value.items()]
        pairs.sort(key=lambda pair: canonical_json_bytes(pair[0]))
        return {"collection": "dict", "items": cast(list[JSONValue], pairs)}
    if value is None or isinstance(value, (str, bool, int, float)):
        canonical_json_bytes(value)
        return value
    raise PersistenceError("unsupported persisted value")


def unpack(value: object, child: Callable[[object], object] | None = None) -> object:
    decode = child or unpack
    if value is None or isinstance(value, (str, bool, int, float)):
        canonical_json_bytes(value)
        return value
    if not isinstance(value, dict):
        raise PersistenceError("invalid typed JSON envelope")
    data = cast(dict[str, object], value)
    if set(data) == {"collection", "items"}:
        items = data["items"]
        if not isinstance(items, list):
            raise PersistenceError("invalid persisted collection")
        kind = data["collection"]
        if kind == "dict":
            result: dict[object, object] = {}
            for pair in items:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise PersistenceError("invalid persisted mapping")
                key = decode(pair[0])
                if key in result:
                    raise PersistenceError("duplicate persisted key")
                result[key] = decode(pair[1])
            return result
        decoded = [decode(v) for v in items]
        constructors: dict[str, Callable[[list[object]], object]] = {
            "tuple": tuple,
            "list": list,
            "frozenset": frozenset,
            "set": set,
        }
        if not isinstance(kind, str) or kind not in constructors:
            raise PersistenceError("unsupported persisted collection")
        return constructors[kind](decoded)
    key = data.get("type", data.get("enum"))
    if not isinstance(key, str) or key not in TYPES:
        raise PersistenceError("unknown persisted type")
    cls = TYPES[key]
    constructor = cast(Callable[..., object], cls)
    if issubclass(cls, Enum) and set(data) == {"enum", "value"}:
        return constructor(data["value"])
    if is_dataclass(cls) and set(data) == {"type", "fields"}:
        values = data["fields"]
        if not isinstance(values, dict) or set(values) != {f.name for f in fields(cls) if f.init}:
            raise PersistenceError("persisted contract field mismatch")
        return constructor(**{k: decode(v) for k, v in values.items()})
    raise PersistenceError("persisted type/envelope mismatch")


def dumps(value: object) -> bytes:
    return canonical_json_bytes(pack(value))


def strict_json(payload: bytes) -> object:
    def mapping(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PersistenceError("duplicate persisted JSON field")
            result[key] = value
        return result

    return json.loads(payload, object_pairs_hook=mapping)


def loads(payload: bytes) -> object:
    try:
        return unpack(strict_json(payload))
    except (ValueError, TypeError, KeyError, AttributeError, EIEError, RecursionError) as exc:
        raise PersistenceError("invalid persisted typed JSON") from exc
