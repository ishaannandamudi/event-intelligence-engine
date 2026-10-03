"""Representative offline preprocessing, fake model, persistence and restart profile."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from eie.nlp.engine import TextEngine
from eie.nlp.extraction import deterministic, extract, prepare
from eie.nlp.interchange import proposal_bytes
from eie.nlp.model_protocol import OfflineExtractionModel
from eie.nlp.models import ModelDescriptor
from eie.nlp.queries import TextQueries
from eie.nlp.repository import TextRepository
from eie.nlp.validation import validate_store
from eie.nlp.validation_models import ValidationStatus
from eie.persistence.codec import dumps
from eie.persistence.sqlite import SQLiteEventStore
from examples.text_demo import BODY, CATALOG, DATASET, item, source


def profile(
    root: Path, *, documents: int = 10000, persisted: int = 25, model_every: int = 200
) -> dict[str, object]:
    if not 1 <= persisted <= documents <= 10000 or not 1 <= model_every <= 10000:
        raise ValueError("bounded positive workload required")
    provider = source("PROFILE")
    characters = candidates = spans = model_calls = 0
    fixtures = []
    normalization_s = extraction_s = verification_s = 0.0
    start = perf_counter()
    for n in range(documents):
        text = BODY.replace("$5.2 billion", f"${5.2 + (n % 20) / 10:.1f} billion")
        doc, payload = item(provider, text, 100 + n, item_id=f"profile-{n:05d}")
        characters += len(text)
        clock = perf_counter()
        p = prepare(doc, payload, CATALOG, 100000 + n)
        normalization_s += perf_counter() - clock
        clock = perf_counter()
        proposals = deterministic(p)
        extraction_s += perf_counter() - clock
        model = None
        if n % model_every == 0:
            model = OfflineExtractionModel(
                ModelDescriptor("profile-fixture", "1"), proposal_bytes(proposals)
            )
            model_calls += 1
        clock = perf_counter()
        result = extract(doc, payload, CATALOG, 100000 + n, model=model)
        verification_s += perf_counter() - clock
        candidates += len(result.candidates)
        spans += len(result.spans)
        if n < persisted:
            fixtures.append((doc, payload, model))
    preprocessing_s = perf_counter() - start
    path = root / "text-profile.sqlite"
    clock = perf_counter()
    with SQLiteEventStore(path) as store:
        repo = TextRepository(store)
        engine = TextEngine(repo)
        runs = [
            engine.ingest(provider, doc, payload, CATALOG, 100000 + n, model=model)
            for n, (doc, payload, model) in enumerate(fixtures)
        ]
        persistence_s = perf_counter() - clock
        queries = TextQueries(repo, DATASET.dataset_id)
        clock = perf_counter()
        before = tuple(dumps(queries.audit(r.run_id, 200000)) for r in runs)
        query_s = perf_counter() - clock
        table_counts = {
            name: store._rows(f"SELECT COUNT(*) FROM {name}")[0][0]
            for name in (
                "te_documents",
                "te_contents",
                "te_artifacts",
                "te_runs",
                "te_accepted",
                "ce_claims",
                "ce_assertions",
                "ce_references",
            )
        }
    clock = perf_counter()
    with SQLiteEventStore(path) as store:
        repo = TextRepository(store)
        queries = TextQueries(repo, DATASET.dataset_id)
        assert tuple(dumps(queries.audit(r.run_id, 200000)) for r in runs) == before
        assert validate_store(repo).status is ValidationStatus.VALID
    restart_s = perf_counter() - clock
    return {
        "documents_preprocessed": documents,
        "documents_persisted": persisted,
        "characters": characters,
        "candidates": candidates,
        "spans": spans,
        "fake_model_calls": model_calls,
        "preprocessing_seconds": preprocessing_s,
        "docs_per_second": documents / preprocessing_s,
        "characters_per_second": characters / preprocessing_s,
        "normalization_and_mentions_seconds": normalization_s,
        "deterministic_proposals_seconds": extraction_s,
        "full_extraction_and_verification_seconds": verification_s,
        "persistence_seconds": persistence_s,
        "audit_query_seconds": query_s,
        "restart_and_validation_seconds": restart_s,
        "database_bytes": path.stat().st_size,
        "tables": table_counts,
        "restart_byte_identical": True,
        "network_calls": 0,
        "timing_threshold": None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=int, default=10000)
    parser.add_argument("--persisted", type=int, default=25)
    parser.add_argument("--model-every", type=int, default=200)
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        print(
            json.dumps(
                profile(
                    Path(directory),
                    documents=args.documents,
                    persisted=args.persisted,
                    model_every=args.model_every,
                ),
                sort_keys=True,
                indent=2,
            )
        )
