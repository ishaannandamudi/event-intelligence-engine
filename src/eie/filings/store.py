"""Append-only point-in-time filing metadata index."""

from eie.core.errors import PersistenceError, TemporalConsistencyError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.filings.models import FilingMetadata
from eie.providers.sec.models import CIK, Accession


class InMemoryFilingStore:
    def __init__(self) -> None:
        self._by_id: dict[str, list[FilingMetadata]] = {}
        self._by_accession: dict[str, str] = {}

    def append(self, metadata: FilingMetadata) -> bool:
        prior_id = self._by_accession.get(metadata.accession.formatted)
        if prior_id is not None and prior_id != metadata.filing_id:
            raise PersistenceError("SEC accession mapped to conflicting CIK/filing")
        history = self._by_id.setdefault(metadata.filing_id, [])
        if metadata.revision_number <= len(history):
            if history[metadata.revision_number - 1] != metadata:
                raise PersistenceError("conflicting filing revision")
            return False
        if metadata.revision_number != len(history) + 1:
            raise PersistenceError("filing revision gap")
        if history and metadata.available_from_ns <= history[-1].available_from_ns:
            raise TemporalConsistencyError("filing revision knowledge must increase")
        history.append(metadata)
        self._by_accession[metadata.accession.formatted] = metadata.filing_id
        return True

    def get(self, filing_id: str, as_of_ns: int) -> FilingMetadata | None:
        require_ns(as_of_ns, "as_of_ns")
        return next(
            (
                item
                for item in reversed(self._by_id.get(filing_id, ()))
                if item.available_from_ns <= as_of_ns
            ),
            None,
        )

    def by_accession(self, accession: Accession, as_of_ns: int) -> FilingMetadata | None:
        filing_id = self._by_accession.get(accession.formatted)
        return None if filing_id is None else self.get(filing_id, as_of_ns)

    def history(self, filing_id: str) -> tuple[FilingMetadata, ...]:
        return tuple(self._by_id.get(filing_id, ()))

    def known_as_of(
        self,
        as_of_ns: int,
        *,
        issuer_id: str | None = None,
        cik: CIK | None = None,
        exact_form: str | None = None,
        base_form: str | None = None,
        amendment: bool | None = None,
        report_period: str | None = None,
    ) -> tuple[FilingMetadata, ...]:
        require_ns(as_of_ns, "as_of_ns")
        results = []
        for filing_id in sorted(self._by_id):
            item = self.get(filing_id, as_of_ns)
            if item is None:
                continue
            if issuer_id is not None and item.issuer_id != issuer_id:
                continue
            if cik is not None and item.cik != cik:
                continue
            if exact_form is not None and item.form.raw != exact_form:
                continue
            if base_form is not None and item.form.base != base_form:
                continue
            if amendment is not None and item.form.is_amendment != amendment:
                continue
            if report_period is not None and item.report_period != report_period:
                continue
            results.append(item)
        return tuple(
            sorted(results, key=lambda item: (item.accepted_at_ns, item.accession.formatted))
        )

    def amendments_for(self, filing_id: str, as_of_ns: int) -> tuple[FilingMetadata, ...]:
        return tuple(
            item for item in self.known_as_of(as_of_ns) if item.amends_filing_id == filing_id
        )

    def recent(self, as_of_ns: int, limit: int = 10) -> tuple[FilingMetadata, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("recent filing limit must be positive")
        return tuple(reversed(self.known_as_of(as_of_ns)[-limit:]))

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "filings": [item for key in sorted(self._by_id) for item in self._by_id[key]],
            }
        )
