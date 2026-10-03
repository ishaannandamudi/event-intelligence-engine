"""Descriptive category sets and counts; no allocation or combined severity."""

from eie.interactions.models import EventCollision
from eie.interactions.schema import CollisionType as C
from eie.interactions.schema import ContaminationCategory as K
from eie.interactions.schema import InteractionQuality


def contamination(collisions: tuple[EventCollision, ...]) -> tuple[K, ...]:
    if not collisions:
        return (K.ISOLATED,)
    categories = {c for row in collisions for c in row.categories}
    flags = set()
    for c, k in (
        (C.SAME_SOURCE_BUNDLE, K.SAME_SOURCE_BUNDLE),
        (C.SAME_ISSUER, K.SAME_ISSUER_OVERLAP),
        (C.MACRO_ISSUER, K.MACRO_OVERLAP),
        (C.GRAPH_RELATED, K.GRAPH_RELATED_OVERLAP),
        (C.SAME_SECURITY, K.SAME_SECURITY_OVERLAP),
        (C.MARKET_ISSUER, K.MARKET_OVERLAP),
        (C.SECTOR_ISSUER, K.SECTOR_OVERLAP),
    ):
        if c in categories:
            flags.add(k)
    if len(collisions) > 1:
        flags.add(K.MULTIPLE_OVERLAPS)
    if any(
        r.quality & {InteractionQuality.TIED, InteractionQuality.PARTIAL_ORDER} for r in collisions
    ):
        flags.add(K.ORDERING_AMBIGUOUS)
    # Market/security/window-only overlaps still carry a descriptive overlap category.
    if not flags:
        flags.add(K.WINDOW_OVERLAP)
    return tuple(sorted(flags))
