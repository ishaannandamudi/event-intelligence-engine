"""Pure component assembly over one transactionally collected catalog view."""

from eie.service.compatibility import versions
from eie.service.errors import ServiceError
from eie.service.identity import resolve
from eie.service.models import (
    EIESnapshot,
    ProvenancePin,
    SnapshotCompleteness,
    SnapshotComponent,
    SnapshotRequest,
)
from eie.service.repository import ServiceRepository
from eie.service.schema import CompletenessState as Q
from eie.service.schema import ComponentKind, ServiceCode
from eie.service.schema import ComponentState as S


def assemble(repository: ServiceRepository, request: SnapshotRequest) -> EIESnapshot:
    identity, scopes, identities = resolve(repository, request)
    components = []
    provenance: set[ProvenancePin] = set()
    for p in identities:
        provenance.add(
            ProvenancePin(
                "IDENTITY_PUBLICATION", p.artifact_id, p.fingerprint, p.reference.available_ns
            )
        )
    for kind in request.components:
        values = repository.collect(request, kind, scopes)
        declaration = repository.coverage(request, kind, scopes)
        completeness = Q.UNKNOWN if declaration is None else declaration.completeness
        reasons: set[str] = set()
        if kind is ComponentKind.IDENTITY and identity.issuer_id is None:
            completeness, state, values = Q.NOT_APPLICABLE, S.NOT_APPLICABLE, ()
        elif completeness is Q.NOT_APPLICABLE:
            if values:
                raise ServiceError(ServiceCode.INTERNAL_VALIDATION_FAILURE)
            state = S.NOT_APPLICABLE
        elif not values:
            state = S.AVAILABLE if completeness is Q.COMPLETE else S.UNAVAILABLE
            if state is S.UNAVAILABLE:
                reasons.add("NO_PUBLISHED_COMPONENT")
        else:
            groups = [p.group_key for p in values]
            if len(groups) != len(set(groups)) or any(p.state is S.CONFLICTED for p in values):
                state = S.CONFLICTED
                reasons.add("CONFLICTED_COMPONENT")
            elif any(p.state is S.INCOMPLETE for p in values) or completeness is Q.KNOWN_GAPS:
                state = S.INCOMPLETE
            elif any(p.state is S.STALE for p in values):
                state = S.STALE
            else:
                state = S.AVAILABLE
            if any(p.quality for p in values):
                reasons.add("OWNER_QUALITY_QUALIFICATIONS")
        if completeness is Q.UNKNOWN:
            reasons.add("COVERAGE_UNKNOWN")
        if kind in request.required and state not in (S.AVAILABLE, S.CONFLICTED, S.STALE):
            raise ServiceError(ServiceCode.INCOMPLETE_DATA)
        if declaration is not None:
            provenance.add(declaration.evidence)
        for p in values:
            provenance.update(p.reference.provenance)
            provenance.add(
                ProvenancePin("PUBLICATION", p.artifact_id, p.fingerprint, p.reference.available_ns)
            )
        components.append(
            SnapshotComponent(
                kind,
                state,
                completeness,
                tuple(p.reference for p in values),
                tuple(p.artifact_id for p in values),
                None if declaration is None else declaration.artifact_id,
                tuple(sorted(reasons)),
            )
        )
    dimensions = SnapshotCompleteness(tuple((c.kind, c.completeness) for c in components))
    return EIESnapshot(
        request,
        identity,
        tuple(components),
        dimensions,
        tuple(
            sorted(provenance, key=lambda p: (p.kind, p.identity, p.fingerprint, p.available_ns))
        ),
        versions(),
    )
