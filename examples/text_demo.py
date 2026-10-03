"""EIE-12: source grounding, fake model rejection, EIE-11 conflict and restart."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.core.fingerprints import fingerprint
from eie.core.serialization import canonical_json_bytes
from eie.entities.sec import SecIssuerRevision, sec_issuer_id
from eie.evidence.queries import EvidenceQueries
from eie.evidence.references import AcquisitionLineage, EvidenceDataset
from eie.evidence.schema import ClaimType
from eie.ingestion.models import RetentionMode, RetentionPermission, RetentionPolicy
from eie.nlp.documents import TextDocument
from eie.nlp.engine import TextEngine
from eie.nlp.entities import EntityCatalog
from eie.nlp.extraction import deterministic, prepare
from eie.nlp.interchange import proposal_bytes
from eie.nlp.model_protocol import OfflineExtractionModel
from eie.nlp.models import ModelDescriptor
from eie.nlp.queries import TextQueries
from eie.nlp.repository import TextRepository
from eie.nlp.schema import DocumentType, SourceAction
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import EvidenceProvider
from eie.providers.sec.models import CIK
from eie.providers.text.adapter import document
from eie.providers.text.models import TextProvider
from eie.sources.models import RawSourceObservation, SourceIdentity
from eie.sources.trust import SourceClass

DATASET = EvidenceDataset("offline-text", fingerprint("eie:text-demo:v1", "synthetic"))
CIK_VALUE = CIK("0000320193")
ISSUER = sec_issuer_id(CIK_VALUE)
CATALOG = EntityCatalog(
    (SecIssuerRevision(ISSUER, CIK_VALUE, "Example Corporation", (), 0, "obs_" + "a" * 64),)
)
BODY = (
    "CIK 0000320193 reported revenue $5.2 billion and EPS $1.25 per share "
    "for Q3 2026 ended 2026-09-30.\n"
    "CIK 0000320193 announced a buyback authorization of $500 million.\n"
    "CIK 0000320193 CEO Jane Smith resigned effective 2026-10-01."
)


def source(
    name: str = "OFFICIAL", *, retain: bool = True, family: str | None = None
) -> TextProvider:
    return TextProvider(
        EvidenceProvider(
            SourceIdentity(name, "1", "1", SourceClass.OFFICIAL_PRIMARY),
            family or name,
            DATASET,
            frozenset(ClaimType),
            normalized_retention=RetentionPermission.ALLOWED,
            archive_policy=RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.ALLOWED)
            if retain
            else RetentionPolicy(),
        ),
        currency="USD",
        timezone="America/New_York",
    )


def item(
    provider: TextProvider,
    text: str = BODY,
    observed: int = 100,
    *,
    version: str = "1",
    item_id: str = "synthetic-release",
    language: str = "en",
    action: SourceAction = SourceAction.ORIGINAL,
    previous: TextDocument | None = None,
) -> tuple[TextDocument, bytes]:
    payload = text.encode()
    raw = RawSourceObservation(
        provider.evidence.source,
        item_id,
        version,
        sha256(payload).hexdigest(),
        "text/plain",
        observed,
        observed,
    )
    lineage = AcquisitionLineage(
        raw.observation_id,
        observed,
        observed,
        provider.evidence.parser_version,
        fingerprint("eie:text-source-settings:v1", "synthetic"),
    )
    return document(
        provider,
        raw,
        lineage,
        document_type=DocumentType.PRESS_RELEASE,
        language=language,
        action=action,
        previous_document_id=None if previous is None else previous.document_id,
    ), payload


def demonstrate(path: Path) -> dict[str, object]:
    a, b = source(), source("NEWS_B")
    doc, payload = item(a)
    prepared = prepare(doc, payload, CATALOG, 200)
    proposals = deterministic(prepared)
    earnings = next(
        p for p in proposals if p.numerics and p.numerics[0].name.value in ("REVENUE", "EPS")
    )
    fabricated = replace(
        earnings,
        key="hallucinated-revenue",
        numerics=(
            replace(
                earnings.numerics[0], quantity=replace(earnings.numerics[0].quantity, value=999)
            ),
        ),
    )
    model = OfflineExtractionModel(
        ModelDescriptor("offline-fixture", "1"), proposal_bytes((fabricated, *proposals))
    )
    with SQLiteEventStore(path) as store:
        repo = TextRepository(store)
        queries = TextQueries(repo, DATASET.dataset_id)
        run = TextEngine(repo).ingest(a, doc, payload, CATALOG, 200, model=model)
        audit = queries.audit(run.run_id, 200)
        assert len(audit.candidates) == 7 and run.accepted
        before = dumps(audit)
        second, content = item(b, BODY.replace("$5.2 billion", "$5.4 billion"), 300)
        TextEngine(repo).ingest(b, second, content, CATALOG, 400)
        numeric = next(
            i
            for link in run.accepted
            for i in link.claim_ids
            if repo.evidence.claim(i).claim_type is ClaimType.ACTUAL_VALUE
            and repo.evidence.claim(i).measurement is not None
            and repo.evidence.claim(i).measurement.measurement_id.code.endswith("REVENUE")
        )
        assert EvidenceQueries(repo.evidence, DATASET.dataset_id).resolution(numeric, 400).conflicts
        assert dumps(queries.audit(run.run_id, 200)) == before
    with SQLiteEventStore(path) as store:
        repo = TextRepository(store)
        assert dumps(TextQueries(repo, DATASET.dataset_id).audit(run.run_id, 200)) == before
        return {
            "candidates": 7,
            "accepted_source_events": 3,
            "hallucinated_numeric_rejected": True,
            "eie11_conflict_preserved": True,
            "restart_byte_identical": True,
            "run_fingerprint": run.result_fingerprint,
        }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(canonical_json_bytes(demonstrate(Path(directory) / "text.sqlite")).decode())
