"""Static typed JSON interchange, configured source trust, bounded offline parsing."""

from eie.core.errors import PersistenceError, ValidationError
from eie.graph.claims import ValidSemantics
from eie.graph.exposure import ExposureDomain
from eie.persistence import codec
from eie.providers.relationships.models import (
    RelationshipBatch,
    RelationshipProvider,
    SnapshotPolicy,
)


class RelationshipAdapter:
    def __init__(self, provider: RelationshipProvider) -> None:
        self.provider = provider

    def parse(self, payload: bytes) -> RelationshipBatch:
        if not isinstance(payload, bytes) or len(payload) > 20_000_000:
            raise ValidationError("bounded relationship payload bytes required")
        try:
            batch = codec.loads(payload)
        except PersistenceError as exc:
            raise ValidationError("invalid relationship interchange") from exc
        if not isinstance(batch, RelationshipBatch):
            raise ValidationError("relationship batch envelope required")
        p = self.provider
        if any(i.relationship_type not in p.capabilities for i in batch.items):
            raise ValidationError("relationship source exceeded declared capabilities")
        if batch.snapshot is not None:
            scope = batch.snapshot
            if (
                scope.relationship_type not in p.capabilities
                or scope.effective_ns > batch.ingested_at_ns
            ):
                raise ValidationError("snapshot capability/reference unavailable")
            if scope.remove_missing and (
                not p.allow_implied_removal
                or p.snapshot_policy is not SnapshotPolicy.CARRY_UNTIL_NEXT
            ):
                raise ValidationError("implied removal requires exhaustive declared carry policy")
            semantics = (
                ValidSemantics.SNAPSHOT
                if p.snapshot_policy is SnapshotPolicy.POINT_ONLY
                else ValidSemantics.INTERVAL
            )
            if any(
                i.relationship_type is not scope.relationship_type
                or i.valid_from_ns != scope.effective_ns
                or i.valid_semantics is not semantics
                or i.valid_to_ns is not None
                for i in batch.items
            ):
                raise ValidationError(
                    "composition rows must agree with snapshot scope/temporal policy"
                )
        if p.long_only_portfolio and any(
            i.exposure is not None and i.exposure.domain is ExposureDomain.SIGNED_PORTFOLIO
            for i in batch.items
        ):
            raise ValidationError("long-only provider cannot supply signed portfolio exposure")
        return batch
