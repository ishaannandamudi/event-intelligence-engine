"""Indexed, bounded collection from one owned SQLite knowledge snapshot."""

from dataclasses import replace

from eie.core.errors import ValidationError
from eie.graph.repository import GraphRepository
from eie.interactions.models import (
    DAY,
    InteractionEvent,
    InteractionInputManifest,
    InteractionPolicy,
    InteractionQuery,
    PathReference,
)
from eie.interactions.repository import InteractionRepository
from eie.interactions.schema import OrderingBasis
from eie.interactions.windows import horizon_end, time_value, window_interval
from eie.studies.models import SessionPolicy


def dedupe(
    rows: tuple[InteractionEvent, ...],
    groups: tuple[tuple[str, ...], ...],
    target: InteractionEvent | None = None,
) -> tuple[InteractionEvent, ...]:
    identity = {e: g[0] for g in groups for e in g}
    selected: dict[str, InteractionEvent] = {}
    for row in sorted(rows, key=lambda r: (r.known_ns, r.event_id, r.artifact_id)):
        selected[identity.get(row.event_id, row.event_id)] = row
    if target is not None:
        selected[identity.get(target.event_id, target.event_id)] = target
    return tuple(sorted(selected.values(), key=lambda r: r.artifact_id))


def collect(
    repo: InteractionRepository,
    query: InteractionQuery,
    policy: InteractionPolicy,
    *,
    select_resolution: bool = True,
) -> InteractionInputManifest:
    repo.evidence.owned()
    target = repo.latest(query.dataset_id, query.target_event_id, query.known_as_of_ns)
    groups: tuple[tuple[str, ...], ...] = ()
    identity_fp = None
    resolution = query.event_resolution_id
    if resolution is None and select_resolution:
        runs = repo.store._rows(
            (
                "SELECT id FROM ce_event_runs WHERE dataset=? AND known<=? ORDER BY known"
                " DESC,id DESC LIMIT 1"
            ),
            (query.dataset_id, query.known_as_of_ns),
        )
        if runs:
            resolution = str(runs[0][0])
            query = replace(query, event_resolution_id=resolution)
    if resolution is not None:
        run = repo.evidence.stored_event_run(resolution)
        if (
            run.dataset_id != query.dataset_id
            or run.known_as_of_ns > query.known_as_of_ns
            or run.truncated_event_ids
        ):
            raise ValidationError("interaction identity requires an untruncated visible EIE-11 run")
        groups = tuple(sorted(g.member_event_ids for g in run.groups))
        identity_fp = run.result_fingerprint
    paths = []
    if query.graph_id is not None:
        graph = GraphRepository(repo.store).snapshot(query.graph_id)
        valid = target.occurrence.timestamp_ns
        if valid is None:
            valid = target.known_ns
        if (
            graph is None
            or graph.known_as_of_ns > query.known_as_of_ns
            or graph.valid_at_ns != valid
            or graph.truncated
            or set(graph.roots) != set(target.scope.identifiers)
        ):
            raise ValidationError(
                "graph must pin direct roots and historical root valid/knowledge times"
            )
        for p in graph.paths:
            if p.depth > policy.max_hops or any(
                e.relationship_type not in policy.relationship_types for e in p.edges
            ):
                continue
            if any(not e.active or e.known_from_ns > query.known_as_of_ns for e in p.edges):
                raise ValidationError("inactive/future graph edge in interaction path")
            paths.append(
                PathReference(
                    graph.snapshot_id,
                    graph.result_fingerprint,
                    p.path_id,
                    p.nodes,
                    tuple(e.revision_id for e in p.edges),
                    tuple(e.relationship_type for e in p.edges),
                    graph.known_as_of_ns,
                    graph.valid_at_ns,
                    bool(p.quality),
                )
            )
        if len(paths) > policy.max_paths:
            raise ValidationError("interaction graph path bound exceeded")
    paths.sort(key=lambda p: p.artifact_id)
    neighbors = tuple(sorted({p.node_ids[-1] for p in paths} | set(target.scope.identifiers)))
    window = policy.window
    bounds = window_interval(target, window)
    if bounds is None:
        start, end = 0, 0
    else:
        extra = max(
            [window.pre_ns, window.post_ns] + [max(o.pre_ns, o.post_ns) for o in window.overrides]
        )
        # Session clocks may span closed periods. Block by supplied calendar envelope.
        if window.session_policy is not SessionPolicy.ELAPSED:
            start, end = window.sessions[0].open_ns, window.sessions[-1].close_ns
        else:
            start, end = max(0, bounds[0] - extra - DAY), bounds[1] + extra + DAY
    candidates = repo.search(
        query.dataset_id,
        query.known_as_of_ns,
        window.basis,
        start,
        end,
        policy.max_pairs + 1,
        entities=neighbors,
        bundles=target.bundle_ids,
        broad=True,
    )
    by_id = {r.artifact_id: r for r in candidates}
    for override in window.overrides:
        if override.basis is not None and override.basis is not window.basis:
            extra_rows = repo.search(
                query.dataset_id,
                query.known_as_of_ns,
                override.basis,
                start,
                end,
                policy.max_pairs + 1,
                entities=neighbors,
                broad=True,
            )
            by_id.update(
                (r.artifact_id, r)
                for r in extra_rows
                if override.selector in (r.event_type.code, r.event_type.family.value)
            )
    candidates = tuple(by_id.values())
    candidates = dedupe(candidates, groups, target)
    if len(candidates) - 1 > policy.max_pairs:
        raise ValidationError("collision candidate pair bound exceeded")
    sequence_rows = candidates
    transitions = repo.search(
        query.dataset_id,
        query.known_as_of_ns,
        OrderingBasis.KNOWLEDGE_TIME,
        0,
        query.known_as_of_ns,
        policy.max_sequence_events,
        thread=target.thread_id,
        history=True,
    )
    end_horizon = horizon_end(target, policy)
    root_time = time_value(target, policy.chronology_basis).timestamp_ns
    propagation_rows: tuple[InteractionEvent, ...] = ()
    if paths and end_horizon is not None and root_time is not None:
        propagation_rows = dedupe(
            repo.search(
                query.dataset_id,
                query.known_as_of_ns,
                policy.chronology_basis,
                root_time,
                end_horizon,
                policy.max_chronology_entries,
                entities=tuple(sorted({p.node_ids[-1] for p in paths})),
            ),
            groups,
        )
        target_group = next((g for g in groups if target.event_id in g), (target.event_id,))
        propagation_rows = tuple(r for r in propagation_rows if r.event_id not in target_group)
    return InteractionInputManifest(
        query,
        target.artifact_id,
        tuple(r.artifact_id for r in candidates),
        tuple(r.artifact_id for r in sequence_rows),
        tuple(r.artifact_id for r in transitions),
        tuple(r.artifact_id for r in propagation_rows),
        tuple(paths),
        groups,
        identity_fp,
    )
