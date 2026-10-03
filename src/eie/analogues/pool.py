"""Indexed blocking, strict clocks and explicit alias/revision exclusions."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from eie.analogues.models import AnaloguePolicy, AnalogueQuery, FeatureRow, ResponseObservation
from eie.analogues.schema import ContaminationPolicy, Exclusion, SectorCondition, TypeCondition
from eie.core.errors import ValidationError
from eie.evidence.repository import EvidenceRepository

if TYPE_CHECKING:
    from eie.analogues.repository import AnalogueRepository


@dataclass(frozen=True, slots=True)
class PoolInputs:
    target: FeatureRow
    rows: tuple[FeatureRow, ...]
    responses: tuple[ResponseObservation, ...]
    excluded_event_ids: tuple[str, ...]
    total_prior_events: int
    incompatible_row_ids: tuple[str, ...] = ()
    alias_groups: tuple[tuple[str, ...], ...] = ()


def target_exclusions(
    repo: "AnalogueRepository", query: AnalogueQuery, target: FeatureRow
) -> tuple[str, ...]:
    excluded = set(target.alias_event_ids) | {target.event_id}
    if query.event_resolution_id is not None:
        run = EvidenceRepository(repo.store).stored_event_run(query.event_resolution_id)
        if run.dataset_id != query.dataset_id or run.known_as_of_ns > query.known_as_of_ns:
            raise ValidationError("event aliases must pin a visible EIE-11 run")
        for group in run.groups:
            if target.event_id in group.member_event_ids:
                excluded.update(group.member_event_ids)
    return tuple(sorted(excluded))


def collect(repo: "AnalogueRepository", query: AnalogueQuery, policy: AnaloguePolicy) -> PoolInputs:
    target = repo.feature(query.target_row_id)
    if target is None or (
        (
            target.event_id,
            target.revision,
            target.dataset_id,
            target.policy_id,
            target.feature_cutoff_ns,
        )
        != (
            query.target_event_id,
            query.target_revision,
            query.dataset_id,
            query.policy_id,
            query.known_as_of_ns,
        )
        or target.available_ns > query.available_ns
    ):
        raise ValidationError("query must pin an available ex-ante target row")
    if (
        query.top_k > min(repo.limits.selected, repo.limits.responses)
        or len(policy.features) > repo.limits.features
    ):
        raise ValidationError("query exceeds declared selection/feature bounds")
    where = ["f.dataset=?", "f.policy=?", "f.event_available<=?", "f.event_available>=?"]
    parameters: list[object] = [
        query.dataset_id,
        query.policy_id,
        query.known_as_of_ns,
        query.earliest_ns,
    ]
    if policy.type_condition is TypeCondition.EXACT:
        where.append("f.event_type=?")
        parameters.append(target.event_type)
    elif policy.type_condition is TypeCondition.SAME_FAMILY:
        where.append("f.family=?")
        parameters.append(target.family)
    else:
        where.append("f.event_type IN (" + ",".join("?" for _ in policy.allowed_types) + ")")
        parameters.extend(policy.allowed_types)
    if policy.same_issuer:
        if len(target.entity_ids) != 1:
            raise ValidationError("same issuer blocking requires unique target identity")
        where.append("EXISTS(SELECT 1 FROM analogue_entities e WHERE e.row=f.id AND e.entity=?)")
        parameters.append(target.entity_ids[0])
    if policy.sector_condition is SectorCondition.REQUIRE_SAME:
        if target.sector is None:
            # No automatic cross-sector fallback.
            where.append("0")
        else:
            where.append("f.sector=?")
            parameters.append(target.sector)
    for dimension in policy.blocking_dimensions:
        context = next(
            (c for c in target.contexts if c.dimension == dimension and c.value is not None), None
        )
        if context is None:
            raise ValidationError("blocking requires a visible target context category")
        where.append(
            "EXISTS(SELECT 1 FROM analogue_contexts c WHERE c.row=f.id AND c.dimension=?"
            " AND c.category=?)"
        )
        parameters.extend((dimension, str(context.value)))
    ids = repo.evidence._ids(
        "SELECT f.id FROM analogue_features f WHERE " + " AND ".join(where) + " ORDER BY f.id",
        tuple(parameters),
        repo.limits.pool_candidates,
    )
    rows = tuple(repo.require_feature(i) for i in ids)
    responses: list[ResponseObservation] = []
    incompatible = []
    for row in rows:
        matches = repo.store._rows(
            "SELECT id FROM analogue_responses WHERE row=? AND spec=? "
            "AND available<? AND completed<? ORDER BY id LIMIT 3",
            (row.row_id, query.response.fingerprint, query.known_as_of_ns, query.known_as_of_ns),
        )
        if len(matches) > 1:
            raise ValidationError("multiple response vintages require explicit feature row lineage")
        if matches:
            if len(responses) >= repo.limits.responses:
                raise ValidationError("collected response observation bound exceeded")
            responses.append(repo.require_response(str(matches[0][0])))
        elif repo.store._rows(
            "SELECT 1 FROM analogue_responses WHERE row=? AND available<? "
            "AND completed<? AND spec<>? LIMIT 1",
            (row.row_id, query.known_as_of_ns, query.known_as_of_ns, query.response.fingerprint),
        ):
            incompatible.append(row.row_id)
    total = repo.store._rows(
        (
            "SELECT COUNT(DISTINCT event) FROM analogue_features WHERE dataset=? AND pol"
            "icy=? AND event_available<? AND event_available>=?"
        ),
        (query.dataset_id, query.policy_id, query.known_as_of_ns, query.earliest_ns),
    )[0][0]
    assert isinstance(total, int)
    groups: tuple[tuple[str, ...], ...] = ()
    if query.event_resolution_id is not None:
        run = EvidenceRepository(repo.store).stored_event_run(query.event_resolution_id)
        groups = tuple(g.member_event_ids for g in run.groups)
    return PoolInputs(
        target,
        rows,
        tuple(responses),
        target_exclusions(repo, query, target),
        total,
        tuple(sorted(incompatible)),
        groups,
    )


def prior_feature(row: FeatureRow, query: AnalogueQuery) -> bool:
    return (
        row.event_available_ns < query.known_as_of_ns
        and row.feature_cutoff_ns < query.known_as_of_ns
        and row.available_ns <= query.available_ns
        and (row.build_id is not None or row.available_ns < query.known_as_of_ns)
    )


def admissibility(
    row: FeatureRow,
    response: ResponseObservation | None,
    data: PoolInputs,
    query: AnalogueQuery,
    policy: AnaloguePolicy,
) -> Exclusion | None:
    if (
        set(row.alias_event_ids) & set(data.excluded_event_ids)
        or row.event_id in data.excluded_event_ids
    ):
        return Exclusion.SAME_EVENT
    if not prior_feature(row, query):
        return Exclusion.NOT_PRIOR
    if (
        (policy.type_condition is TypeCondition.EXACT and row.event_type != data.target.event_type)
        or (policy.type_condition is TypeCondition.SAME_FAMILY and row.family != data.target.family)
        or (
            policy.type_condition is TypeCondition.ALLOWED_SET
            and row.event_type not in policy.allowed_types
        )
    ):
        return Exclusion.INCOMPATIBLE_TYPE
    if row.policy_id != policy.policy_id or row.dataset_id != query.dataset_id:
        raise ValidationError("pool feature policy/dataset mismatch")
    if response is None or response.value is None:
        return (
            Exclusion.INCOMPATIBLE_RETURN_TYPE
            if row.row_id in data.incompatible_row_ids
            else Exclusion.INSUFFICIENT_RESPONSE
        )
    if response.spec != query.response:
        return Exclusion.INCOMPATIBLE_RETURN_TYPE
    if (
        response.available_ns >= query.known_as_of_ns
        or response.completed_ns >= query.known_as_of_ns
    ):
        return Exclusion.NOT_PRIOR
    if policy.reject_reconstruction and (
        row.reconstruction_reasons
        or set(response.reconstruction_reasons) - {"AUTHORITATIVE_POINT_IN_TIME"}
    ):
        return Exclusion.RECONSTRUCTION_LIMITATION
    if policy.reject_data_gaps and "DATA_GAP" in response.quality:
        return Exclusion.DATA_GAP
    if policy.reject_coarse_time and "COARSE_EVENT_TIME" in response.quality:
        return Exclusion.COARSE_TIME
    if (policy.contamination is ContaminationPolicy.EXCLUDE and response.contaminated) or (
        policy.contamination is ContaminationPolicy.SEPARATE_COHORT
        and response.contaminated != policy.contaminated_cohort
    ):
        return Exclusion.CONTAMINATED
    return None


def unique_rows(
    data: PoolInputs,
    query: AnalogueQuery,
    policy: AnaloguePolicy,
) -> tuple[dict[str, Exclusion | None], tuple[FeatureRow, ...]]:
    responses = {r.feature_row_id: r for r in data.responses}
    reasons = {
        r.row_id: admissibility(r, responses.get(r.row_id), data, query, policy) for r in data.rows
    }
    # A pinned EIE-11 run is authoritative for its covered identities, including
    # explicit splits. Older feature memberships cannot rejoin those groups.
    current = {member: min(group) for group in data.alias_groups for member in group}
    parents: dict[str, str] = {}

    def root(identity: str) -> str:
        parents.setdefault(identity, identity)
        if len(parents) > 100000:
            raise ValidationError("analogue alias membership bound exceeded")
        while parents[identity] != identity:
            parents[identity] = parents[parents[identity]]
            identity = parents[identity]
        return identity

    # Canonicalize connected, supported memberships across visible prior rows. Choosing
    # each row's own minimum independently can double count a later lower-ID alias.
    for row in data.rows:
        if (
            not prior_feature(row, query)
            or reasons[row.row_id] is Exclusion.SAME_EVENT
            or row.event_id in current
        ):
            continue
        for member in row.alias_event_ids:
            if member in current:
                continue
            a, b = root(row.event_id), root(member)
            parents[max(a, b)] = min(a, b)
    selected: dict[str, FeatureRow] = {}
    for row in sorted(data.rows, key=lambda r: (r.feature_cutoff_ns, r.revision, r.row_id)):
        if reasons[row.row_id] is not None:
            continue
        key = current[row.event_id] if row.event_id in current else root(row.event_id)
        old = selected.get(key)
        if old is not None:
            if (old.event_id, old.revision, old.feature_cutoff_ns) == (
                row.event_id,
                row.revision,
                row.feature_cutoff_ns,
            ) and old.values != row.values:
                raise ValidationError(
                    "ambiguous historical feature vintages require explicit lineage"
                )
            reasons[old.row_id] = Exclusion.SUPERSEDED_ROW
        selected[key] = row
    return reasons, tuple(sorted(selected.values(), key=lambda r: r.row_id))
