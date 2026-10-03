"""Shared offline text fixtures; no text-derived configuration or live model."""

from dataclasses import replace

from eie.nlp.extraction import extract
from eie.nlp.interchange import proposal_bytes
from eie.nlp.model_protocol import OfflineExtractionModel
from eie.nlp.models import ModelDescriptor
from eie.nlp.schema import UnscheduledFamily
from examples.text_demo import BODY, CATALOG, DATASET, ISSUER, item, source

__all__ = [
    "BODY",
    "CATALOG",
    "DATASET",
    "ISSUER",
    "item",
    "source",
    "artifact",
    "proposal",
    "model",
]


def artifact(text=BODY, *, provider=None, available=200, **options):
    provider = provider or source()
    doc, payload = item(provider, text, **options)
    return extract(doc, payload, CATALOG, available)


def proposal(text=BODY, family=UnscheduledFamily.EARNINGS_RESULT):
    return next(c.proposal for c in artifact(text).candidates if c.proposal.family is family)


def model(proposals=(), *, version="1"):
    return OfflineExtractionModel(
        ModelDescriptor("offline-test", version), proposal_bytes(tuple(proposals))
    )


def altered(value, **fields):
    return replace(value, **fields)
