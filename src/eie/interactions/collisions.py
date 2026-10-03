"""Symmetric overlap records with separate issuer/bundle/scope/path categories."""

from eie.events.scope import ScopeKind
from eie.events.taxonomy import EventFamily
from eie.interactions.models import (
    EventCollision,
    InteractionEvent,
    InteractionWindow,
    PathReference,
)
from eie.interactions.schema import CollisionType as C
from eie.interactions.schema import EventInteractionRelation as R
from eie.interactions.schema import InteractionQuality as Q
from eie.interactions.schema import OrderingBasis
from eie.interactions.windows import time_value, window_interval

ISSUER = frozenset({ScopeKind.COMPANY, ScopeKind.SECURITY, ScopeKind.SYMBOL})


def collision(
    a: InteractionEvent,
    b: InteractionEvent,
    known: int,
    window: InteractionWindow,
    paths: tuple[PathReference, ...],
) -> EventCollision | None:
    left, right = sorted((a, b), key=lambda r: r.artifact_id)
    x, y = window_interval(left, window), window_interval(right, window)
    overlap = x is not None and y is not None and max(x[0], y[0]) <= min(x[1], y[1])
    shared_bundle = bool(set(left.bundle_ids) & set(right.bundle_ids))
    # A disclosure bundle is meaningful even with absent occurrence timing.
    if not overlap and not shared_bundle:
        return None
    categories: set[C] = set()
    relations = {R.CO_OCCURS_WITH}
    quality = set(left.quality | right.quality)
    if overlap:
        categories.add(C.OVERLAPPING_WINDOW)
    if shared_bundle:
        categories.add(C.SAME_SOURCE_BUNDLE)
        relations.add(R.SAME_BUNDLE_AS)
    if left.scope.kind is right.scope.kind and set(left.scope.identifiers) & set(
        right.scope.identifiers
    ):
        if left.scope.kind is ScopeKind.COMPANY:
            categories.add(C.SAME_ISSUER)
        if left.scope.kind is ScopeKind.SECURITY:
            categories.add(C.SAME_SECURITY)
    for other, issuer in ((left, right), (right, left)):
        if issuer.scope.kind in ISSUER:
            if other.event_type.family in (EventFamily.MACRO, EventFamily.CENTRAL_BANK):
                categories.add(C.MACRO_ISSUER)
            elif other.scope.kind is ScopeKind.MARKET:
                categories.add(C.MARKET_ISSUER)
            if other.scope.kind in (ScopeKind.SECTOR, ScopeKind.INDUSTRY) and paths:
                categories.add(C.SECTOR_ISSUER)
    if paths:
        categories.add(C.GRAPH_RELATED)
        relations.add(R.GRAPH_RELATED_TO)
        if any(p.qualified for p in paths):
            quality.add(Q.GRAPH_QUALIFIED)
    t, u = (
        time_value(left, window.basis_for(left.event_type)).timestamp_ns,
        time_value(right, window.basis_for(right.event_type)).timestamp_ns,
    )
    basis = window.basis_for(left.event_type)
    if basis is not window.basis_for(right.event_type):
        basis = OrderingBasis.UNKNOWN
    if t is not None and u is not None and t != u and basis is not OrderingBasis.UNKNOWN:
        relations.add(R.PRECEDES if t < u else R.FOLLOWS)
    if t is not None and u is not None:
        if t == u:
            categories.add(C.SAME_TIMESTAMP)
            quality.add(Q.TIED)
        elif categories & {C.SAME_ISSUER, C.SAME_SECURITY, C.GRAPH_RELATED}:
            categories.add(C.SEQUENTIAL_RELATED)
    else:
        quality.add(Q.PARTIAL_ORDER)
    # Temporal overlap alone is an explicit permitted category; it is not attribution.
    return EventCollision(
        left.artifact_id,
        right.artifact_id,
        known,
        window.artifact_id,
        tuple(sorted(categories)),
        tuple(sorted(relations)),
        x,
        y,
        basis,
        tuple(sorted(p.artifact_id for p in paths)),
        frozenset(quality),
    )
