"""Typed, deterministic corporate views and declared source coverage."""

from dataclasses import dataclass

from eie.core.errors import TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.corporate.models import CorporateEvent, CorporateKind
from eie.entities.protocols import ResolutionResult
from eie.events.models import CanonicalEvent
from eie.providers.sec.models import iso_date


@dataclass(frozen=True, slots=True)
class CorporateCoverage:
    provider_id: str
    kinds: tuple[CorporateKind, ...]
    from_date: str
    to_date: str
    available_from_ns: int

    def __post_init__(self) -> None:
        require_ns(self.available_from_ns, "coverage available_from_ns")
        if not self.provider_id or not self.from_date or not self.to_date:
            raise ValidationError("corporate coverage needs provider and date range")
        iso_date(self.from_date, "coverage from_date")
        iso_date(self.to_date, "coverage to_date")
        if self.from_date > self.to_date:
            raise ValidationError("corporate coverage range reversed")
        if self.kinds != tuple(sorted(set(self.kinds))) or any(
            not isinstance(kind, CorporateKind) for kind in self.kinds
        ):
            raise ValidationError("coverage kinds must be sorted unique")


@dataclass(frozen=True, slots=True)
class CorporateView:
    corporate: CorporateEvent
    canonical: CanonicalEvent
    as_of_ns: int

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if self.corporate.revision != self.canonical.revision:
            raise ValidationError("corporate and canonical revisions disagree")
        if self.corporate.revision.available_from_ns > self.as_of_ns:
            raise TemporalConsistencyError("future corporate event in view")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:corporate-view:v1", self)


@dataclass(frozen=True, slots=True)
class CorporateQueryResult:
    as_of_ns: int
    events: tuple[CorporateView, ...]
    coverage: tuple[CorporateCoverage, ...]
    configuration_fingerprint: str

    def __post_init__(self) -> None:
        require_ns(self.as_of_ns, "as_of_ns")
        if any(view.as_of_ns != self.as_of_ns for view in self.events):
            raise ValidationError("corporate views have mismatched as-of")
        ids = tuple(view.corporate.corporate_id for view in self.events)
        if len(ids) != len(set(ids)):
            raise ValidationError("corporate views must be unique")
        if any(item.available_from_ns > self.as_of_ns for item in self.coverage):
            raise TemporalConsistencyError("future coverage in query")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:corporate-query:v1", self)


@dataclass(frozen=True, slots=True)
class CorporateSymbolQueryResult:
    valid_at_ns: int
    resolution: ResolutionResult
    query: CorporateQueryResult

    def __post_init__(self) -> None:
        require_ns(self.valid_at_ns, "valid_at_ns")

    @property
    def result_fingerprint(self) -> str:
        return fingerprint("eie:corporate-symbol-query:v1", self)
