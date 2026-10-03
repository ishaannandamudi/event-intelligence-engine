"""Recalculate frozen compact inputs; source/graph reconstruction is optional."""

from eie.core.errors import PersistenceError
from eie.interactions.engine import calculate
from eie.interactions.models import InteractionContext
from eie.interactions.repository import InteractionRepository


def validate(repo: InteractionRepository, context_id: str, *, deep: bool = False) -> None:
    with repo.store.transaction():
        item = repo.evidence._one("interaction_contexts", context_id, InteractionContext)
        if item is None:
            raise PersistenceError("missing interaction snapshot")
        saved = item[0]
        manifest = repo.manifest(context_id)
        if manifest.query != saved.query or manifest.target_row_id != saved.target_row_id:
            raise PersistenceError("interaction manifest/query disagreement")
        q = manifest.query
        for identity in saved.member_row_ids:
            row = repo.event(identity, deep=deep)
            if row.dataset_id != q.dataset_id or row.known_ns > q.known_as_of_ns:
                raise PersistenceError("future/mismatched event in interaction snapshot")
        if q.event_resolution_id is not None:
            rows = repo.store._rows(
                "SELECT payload FROM ce_event_runs WHERE id=?", (q.event_resolution_id,)
            )
            if not rows:
                raise PersistenceError("missing interaction identity source")
            from eie.evidence.dedup import EventResolutionRun
            from eie.evidence.repository import typed

            # No full upstream resolution rebuild on ordinary reads.
            run = typed(rows[0][0], EventResolutionRun)
            if (
                run.result_fingerprint != manifest.identity_fingerprint
                or run.known_as_of_ns > q.known_as_of_ns
                or run.dataset_id != q.dataset_id
                or tuple(sorted(g.member_event_ids for g in run.groups)) != manifest.identity_groups
            ):
                raise PersistenceError("interaction identity source disagreement")
            if deep:
                repo.evidence.stored_event_run(q.event_resolution_id)
        if q.graph_id is not None:
            rows = repo.store._rows(
                "SELECT known,valid,fingerprint FROM graph_snapshots WHERE id=?", (q.graph_id,)
            )
            if not rows or not isinstance(rows[0][0], int) or rows[0][0] > q.known_as_of_ns:
                raise PersistenceError("missing/future graph snapshot")
            for path in manifest.paths:
                if (path.known_ns, path.valid_ns, path.graph_fingerprint) != rows[
                    0
                ] or path.graph_id != q.graph_id:
                    raise PersistenceError("graph path reference disagreement")
            if deep:
                from eie.graph.repository import GraphRepository

                graph = GraphRepository(repo.store).snapshot(q.graph_id)
                assert graph is not None
                for path in manifest.paths:
                    original = next((p for p in graph.paths if p.path_id == path.path_id), None)
                    if (
                        original is None
                        or original.nodes != path.node_ids
                        or tuple(e.revision_id for e in original.edges) != path.edge_revision_ids
                        or tuple(e.relationship_type for e in original.edges)
                        != path.relationship_types
                    ):
                        raise PersistenceError("exact graph path provenance disagreement")
        if deep:
            from eie.interactions.collection import collect

            if collect(repo, q, repo.policy(q.policy_id), select_resolution=False) != manifest:
                raise PersistenceError("frozen interaction collection disagreement")
        expected = calculate(repo, manifest, repo.policy(q.policy_id))
        if expected.context != saved:
            raise PersistenceError("interaction snapshot recalculation disagreement")
        for child in expected.children:
            if repo.artifact(child.artifact_id, type(child)) != child:
                raise PersistenceError("interaction child recalculation disagreement")
        for c in expected.collisions:
            if repo.store._rows(
                (
                    "SELECT left_row,right_row,type FROM interaction_pairs WHERE collision=? "
                    "ORDER BY type"
                ),
                (c.artifact_id,),
            ) != [(c.left_row_id, c.right_row_id, k.value) for k in c.categories]:
                raise PersistenceError("collision category index disagreement")
        for e in expected.chronology:
            if repo.store._rows(
                "SELECT root,later,path FROM interaction_chronology WHERE id=?", (e.artifact_id,)
            ) != [(e.root_row_id, e.later_row_id, e.path_id)]:
                raise PersistenceError("chronology membership disagreement")
