"""Transactional indexed source diagnostics; independent owners are never rewritten."""

from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.reliability import bitemporal, coverage, latency, reliability
from eie.reliability.models import (
    BitemporalAuditResult,
    CoverageUniverse,
    InformationLatency,
    ReliabilityQuery,
    ReliabilityRunManifest,
    SourceCoverageResult,
    SourceReliabilityResult,
)
from eie.reliability.repository import ReliabilityRepository


class ReliabilityEngine:
    def __init__(self, repository: ReliabilityRepository) -> None:
        self.repository = repository

    @staticmethod
    def bounded(value: object, maximum: int) -> None:
        if len(canonical_json_bytes(value)) > maximum:
            raise ValidationError("source diagnostic output byte bound exceeded")

    def source_reliability(self, query: ReliabilityQuery) -> SourceReliabilityResult:
        repo = self.repository
        with repo.store.transaction():
            cached = repo.store._rows(
                "SELECT result FROM reliability_requests WHERE request=?", (query.artifact_id,)
            )
            if cached:
                result = repo.artifact(str(cached[0][0]), SourceReliabilityResult)
                if result.query != query:
                    raise ValidationError("cached reliability request mapping disagreement")
                return result
            rows, peers = repo.collect(query)
            cover = None
            if query.coverage_universe_id is not None:
                universe = repo.artifact(query.coverage_universe_id, CoverageUniverse)
                if (universe.dataset_id, universe.provider, universe.resource_id) != (
                    query.dataset_id,
                    query.provider,
                    query.resource_id,
                ) or query.scope_id not in (None, universe.scope_id):
                    raise ValidationError("reliability coverage universe attribution disagreement")
                cover = self.coverage(
                    query.coverage_universe_id,
                    query.known_as_of_ns,
                    maximum=query.policy.max_observations,
                )
            result = reliability.calculate(
                query,
                rows,
                peers,
                None if cover is None else cover.artifact_id,
                frozenset() if cover is None else cover.quality,
            )
            self.bounded(result, query.policy.max_bytes)
            repo.put(query.policy)
            repo.put(result)
            repo.store._connection.execute(
                "INSERT INTO reliability_requests VALUES (?,?)",
                (query.artifact_id, result.artifact_id),
            )
            repo.seal(query.dataset_id, query.known_as_of_ns)
            return result

    def latency(self, row_id: str, known_as_of_ns: int) -> InformationLatency:
        require_ns(known_as_of_ns)
        repo = self.repository
        with repo.store.transaction():
            row = repo.row(row_id)
            result = latency.calculate(row, known_as_of_ns)
            repo.put(result)
            repo.seal(row.dataset_id, known_as_of_ns)
            return result

    def audit(
        self,
        dataset: str,
        subject: str,
        known_as_of_ns: int,
        valid_at_ns: int,
        *,
        maximum: int = 10000,
    ) -> BitemporalAuditResult:
        require_ns(known_as_of_ns)
        require_ns(valid_at_ns)
        if not 1 <= maximum <= 10000:
            raise ValidationError("audit query count bound exceeded")
        repo = self.repository
        with repo.store.transaction():
            ids = repo.ids(
                "SELECT e.id FROM reliability_rows e WHERE dataset=? AND known<=? "
                "AND available<=? AND "
                "(subject=? OR thread=? OR source_revision=?) AND "
                + repo.latest_frame(known_as_of_ns, inclusive=True)
                + " ORDER BY e.id",
                (dataset, known_as_of_ns, known_as_of_ns, subject, subject, subject),
                maximum,
            )
            result = bitemporal.calculate(
                dataset, subject, known_as_of_ns, valid_at_ns, tuple(repo.row(i) for i in ids)
            )
            self.bounded(result, 5_000_000)
            repo.put(result)
            repo.seal(dataset, known_as_of_ns, "audit")
            return result

    def coverage(
        self, universe_id: str | None, known_as_of_ns: int, *, maximum: int = 10000
    ) -> SourceCoverageResult:
        require_ns(known_as_of_ns)
        if not 1 <= maximum <= 100000:
            raise ValidationError("coverage query bound exceeded")
        repo = self.repository
        with repo.store.transaction():
            universe = None if universe_id is None else repo.artifact(universe_id, CoverageUniverse)
            if universe is not None and len(universe.slots) > maximum:
                raise ValidationError("coverage denominator slot bound exceeded")
            result = coverage.calculate(
                universe,
                ()
                if universe_id is None
                else repo.assessments(universe_id, known_as_of_ns, maximum),
                known_as_of_ns,
            )
            self.bounded(result, 5_000_000)
            repo.put(result)
            if universe is not None:
                repo.seal(universe.dataset_id, known_as_of_ns)
            return result

    def run(self, queries: tuple[ReliabilityQuery, ...]) -> ReliabilityRunManifest:
        if not queries or len(queries) > min(q.policy.max_run_members for q in queries):
            raise ValidationError("source diagnostic run member bound exceeded")
        if len({(q.known_as_of_ns, q.policy.artifact_id) for q in queries}) != 1:
            raise ValidationError("run requires one explicit cutoff and policy")
        if len(
            {(q.provider, q.resource_id, q.event_type, q.field, q.scope_id) for q in queries}
        ) > min(q.policy.max_groups for q in queries):
            raise ValidationError("run group bound exceeded")
        with self.repository.store.transaction():
            results = tuple(
                sorted(
                    {
                        r.artifact_id: r for r in (self.source_reliability(q) for q in queries)
                    }.values(),
                    key=lambda r: r.artifact_id,
                )
            )
            manifest = ReliabilityRunManifest(
                queries[0].known_as_of_ns,
                tuple(r.artifact_id for r in results),
                tuple(r.fingerprint for r in results),
                queries[0].policy.artifact_id,
            )
            self.bounded((manifest, results), queries[0].policy.max_bytes)
            self.repository.put(manifest)
            return manifest
