"""Synthetic offline facts ingested through the existing EIE-11 owner."""

from dataclasses import replace

from eie.core.fingerprints import fingerprint
from eie.evidence.engine import EvidenceEngine
from eie.evidence.repository import EvidenceRepository
from eie.evidence.schema import ClaimStatus
from eie.expectations.units import Quantity
from eie.nlp.documents import ExtractionAcquisition
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import EvidenceBatch, EvidenceProvider
from eie.reliability.clocks import exact
from eie.reliability.inputs import from_claim
from eie.reliability.models import ProcessingTiming, SourceHistoryRow
from eie.reliability.repository import ReliabilityRepository
from eie.sources.trust import SourceClass
from examples.intelligence_fixtures import DATASET, ISSUER, USD, batch, source

SECOND = 1_000_000_000


def provider(name: str = "A", family: str | None = None) -> EvidenceProvider:
    return source(
        name, family=family or name + "_ORIGIN", source_class=SourceClass.OFFICIAL_PRIMARY
    )


def ingest(
    store: SQLiteEventStore,
    name: str,
    known: int,
    *,
    key: str = "release",
    value: float = 100,
    prior: EvidenceBatch | None = None,
    status: ClaimStatus = ClaimStatus.ASSERTED,
    family: str | None = None,
    valid: int | None = 0,
    publication: int | None = None,
    full_clocks: bool = True,
    instruction: str | None = None,
) -> tuple[EvidenceBatch, SourceHistoryRow]:
    p = provider(name, family)
    item = batch(p, known, Quantity(value, USD), key=key, prior=prior, status=status)
    # Keep acquisition timestamps independent from processing availability.
    raw = replace(
        item.raw,
        published_at_ns=publication,
        source_observed_at_ns=known - 2 * SECOND if full_clocks else known,
        ingested_at_ns=known - SECOND if full_clocks else known,
    )
    if instruction is not None:
        raw = replace(raw, metadata=(("untrusted-comment", instruction),))
    lineage = ExtractionAcquisition(
        raw.observation_id,
        raw.source_observed_at_ns,
        raw.ingested_at_ns,
        item.lineage.parser_version,
        item.lineage.settings_fingerprint,
        extraction_available_ns=known,
    )
    event = replace(
        item.events[0], evidence_observation_ids=(raw.observation_id,), published_at_ns=publication
    )
    # Raw identity excludes acquisition clocks; normalized field references remain valid.
    claim = replace(item.claims[0], valid_from_ns=valid)
    item = replace(item, raw=raw, lineage=lineage, events=(event,), claims=(claim,))
    evidence = EvidenceRepository(store)
    existing = store.get_event(event.event_id, known)
    shared_owner = (
        existing is not None
        and store.get_observation(existing.evidence_observation_ids[0]).source.name != name
    )
    receipt = EvidenceEngine(evidence).ingest(p, replace(item, events=()) if shared_owner else item)
    assertion = evidence.assertion(receipt.assertion_ids[0])
    provisional = from_claim(evidence, assertion)
    timing = (
        None
        if not full_clocks
        else ProcessingTiming(
            assertion.observation_id,
            provisional.source_fingerprint,
            "synthetic-processing:" + fingerprint("eie:demo-timing:v1", (name, key, known)),
            known + SECOND,
            exact(known),
            exact(known + SECOND),
        )
    )
    return item, ReliabilityRepository(store).index_claim(assertion.observation_id, timing)


__all__ = ["DATASET", "ISSUER", "SECOND", "provider", "ingest"]
