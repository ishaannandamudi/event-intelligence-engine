"""Append-only revision identity and relation semantics."""

from dataclasses import dataclass
from enum import StrEnum

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns


class RevisionKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    UPDATE = "UPDATE"
    CORRECTION = "CORRECTION"
    REVISION = "REVISION"
    SUPERSEDED = "SUPERSEDED"
    CANCELLATION = "CANCELLATION"
    RESCHEDULE = "RESCHEDULE"


@dataclass(frozen=True, slots=True)
class EventRevision:
    number: int
    kind: RevisionKind
    available_from_ns: int
    supersedes_revision_number: int | None = None
    supersedes_event_id: str | None = None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_ns(self.available_from_ns, "available_from_ns")
        if not isinstance(self.kind, RevisionKind) or not isinstance(self.reason_codes, tuple):
            raise ValidationError("typed immutable revision required")
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise ValidationError("revision number must be positive")
        if self.supersedes_revision_number is not None and (
            isinstance(self.supersedes_revision_number, bool)
            or not isinstance(self.supersedes_revision_number, int)
        ):
            raise ValidationError("superseded revision number must be an integer")
        if self.number == 1:
            if (
                self.kind is not RevisionKind.ORIGINAL
                or self.supersedes_revision_number is not None
            ):
                raise ValidationError("first revision must be ORIGINAL")
        elif (
            self.kind is RevisionKind.ORIGINAL or self.supersedes_revision_number != self.number - 1
        ):
            raise ValidationError("revision must reference the previous revision")
        if self.supersedes_event_id is not None and not self.supersedes_event_id.startswith("evt_"):
            raise ValidationError("invalid superseded event ID")
        if any(
            not isinstance(code, str) or not code or code != code.strip()
            for code in self.reason_codes
        ):
            raise ValidationError("invalid revision reason")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValidationError("reason codes must be sorted and unique")

    def revision_id(self, event_id: str) -> str:
        return "rev_" + fingerprint("eie:event-revision:v1", (event_id, self.number))
