"""One SQLite snapshot collects, freezes and persists ex-ante retrieval."""

from eie.analogues.models import AnalogueQuery, AnalogueResult
from eie.analogues.pool import collect
from eie.analogues.repository import AnalogueRepository
from eie.analogues.retrieval import retrieve


class AnalogueEngine:
    def __init__(self, repository: AnalogueRepository) -> None:
        self.repository = repository

    def analogues(self, query: AnalogueQuery) -> AnalogueResult:
        repo = self.repository
        with repo.store.transaction():
            old = repo.store._rows(
                "SELECT id FROM analogue_results WHERE query=? ORDER BY id", (query.query_id,)
            )
            if old:
                result = repo.get(str(old[0][0]))
                assert result is not None
                return result
            policy = repo.policy(query.policy_id)
            value = retrieve(collect(repo, query, policy), query, policy)
            repo._result(value)
            return value
