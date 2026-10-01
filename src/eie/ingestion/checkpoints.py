"""Numeric resume high watermarks survive overlap and out-of-order delivery."""

from eie.ingestion.models import Cursor, CursorKind, FailureClass
from eie.ingestion.processors import IngestionFailure


def resume_cursor(previous: Cursor | None, candidate: Cursor | None) -> Cursor | None:
    if previous is None:
        return candidate
    if candidate is None:
        return previous
    if previous.kind is not candidate.kind:
        raise IngestionFailure(FailureClass.CHECKPOINT_FAILURE)
    if candidate.kind in (CursorKind.SEQUENCE, CursorKind.TIMESTAMP):
        return previous if int(previous.value) > int(candidate.value) else candidate
    # Opaque/source-item/accession semantics belong to the source contract.
    return candidate
