"""Typed descriptive queries and comparisons; no provider ranking or precedence edits."""

from eie.core.errors import ValidationError
from eie.core.time import require_ns
from eie.reliability.engine import ReliabilityEngine
from eie.reliability.models import ComponentDelta, SourceComparison, SourceReliabilityResult


class ReliabilityQueries(ReliabilityEngine):
    def history(
        self, dataset: str, provider: str, resource: str, known_as_of_ns: int, *, maximum: int = 100
    ) -> tuple[SourceReliabilityResult, ...]:
        require_ns(known_as_of_ns)
        if not 1 <= maximum <= 1000:
            raise ValidationError("reliability history bound exceeded")
        repo = self.repository
        with repo.store.transaction():
            ids = repo.ids(
                "SELECT id FROM reliability_artifacts WHERE kind='SourceReliabilityResult' "
                "AND dataset=? AND provider=? AND resource=? AND known<=? ORDER BY known,id",
                (dataset, provider, resource, known_as_of_ns),
                maximum,
            )
            return tuple(repo.artifact(i, SourceReliabilityResult) for i in ids)

    def compare(
        self, left: SourceReliabilityResult, right: SourceReliabilityResult
    ) -> SourceComparison:
        # Both resource and period comparisons retain the explicit input identities.
        if (
            self.repository.artifact(left.artifact_id, SourceReliabilityResult) != left
            or self.repository.artifact(right.artifact_id, SourceReliabilityResult) != right
        ):
            raise ValidationError("comparison requires retained source diagnostic results")
        if left.query.policy.conflict_policy != right.query.policy.conflict_policy or (
            left.query.policy.correction_definition != right.query.policy.correction_definition
        ):
            raise ValidationError("comparison requires matching metric definitions")

        def components(result: SourceReliabilityResult) -> dict[str, float | None]:
            values = {
                r.name: r.ratio
                for r in (
                    *result.behavior,
                    *result.consistency,
                    result.corroboration,
                    result.conflict,
                )
            }
            values.update(
                {
                    d.start.value + "_TO_" + d.end.value + "_MEDIAN": d.median
                    for d in result.timeliness
                }
            )
            return values

        a, b = components(left), components(right)
        deltas = tuple(
            ComponentDelta(
                k, a[k], b[k], None if (av := a[k]) is None or (bv := b[k]) is None else bv - av
            )
            for k in sorted(a)
        )
        result = SourceComparison(left.artifact_id, right.artifact_id, deltas)
        with self.repository.store.transaction():
            self.repository.put(result)
        return result
