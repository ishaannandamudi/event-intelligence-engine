"""Historical serving readiness is independent of optional live ingestion."""

from eie.core.errors import PersistenceError
from eie.persistence.migrations import STORE_SCHEMA_VERSION
from eie.service.models import ReadinessResult
from eie.service.repository import ServiceRepository
from eie.service.schema import ReadinessState


def readiness(
    repository: ServiceRepository,
    *,
    catalogs: tuple[str, ...] = (),
    live_ingestion: bool | None = None,
) -> ReadinessResult:
    reasons = []
    try:
        store = repository.store
        if store.schema_version != STORE_SCHEMA_VERSION:
            reasons.append("STORE_VERSION_MISMATCH")
        if store._rows("PRAGMA quick_check") != [("ok",)] or store._rows(
            "PRAGMA foreign_key_check"
        ):
            reasons.append("STORE_INTEGRITY_FAILURE")
        for table in (
            "service_publications",
            "service_members",
            "service_coverage",
            "service_snapshots",
            "service_seals",
        ):
            store._rows("SELECT 1 FROM " + table + " LIMIT 1")
        for catalog in catalogs:
            if not store._rows(
                "SELECT 1 FROM service_publications WHERE catalog=? LIMIT 1", (catalog,)
            ):
                reasons.append("CONFIGURED_CATALOG_UNAVAILABLE")
    except PersistenceError:
        reasons.append("STORE_UNAVAILABLE")
    fatal = bool(reasons)
    if live_ingestion is False:
        reasons.append("LIVE_INGESTION_UNAVAILABLE")
    state = (
        ReadinessState.NOT_READY
        if fatal
        else ReadinessState.DEGRADED
        if reasons
        else ReadinessState.READY
    )
    return ReadinessResult(state, tuple(sorted(set(reasons))), not fatal, live_ingestion)
