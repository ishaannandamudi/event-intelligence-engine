"""Public frozen contracts reject mutable/wrongly typed fields and unsafe identities."""

import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, replace
from hashlib import sha256

import pytest
from nlp_helpers import artifact, source

from eie.core.errors import ValidationError
from eie.nlp.documents import ExtractionAcquisition
from eie.nlp.models import AcceptedCandidateLink, ModelDescriptor
from eie.nlp.normalization import TextLimits
from eie.nlp.prompts import ExtractionRequest, PromptTemplate, UntrustedSourceData
from eie.nlp.schema import NumericBound, SourceAction, VerificationReason, VerificationState
from eie.persistence.codec import dumps, loads


def contracts():
    a = artifact()
    p = a.prepared
    n = next(c.proposal for c in a.candidates if c.proposal.numerics)
    data = UntrustedSourceData(
        p.document.document_id,
        p.normalized.mapping.normalized_hash,
        p.normalized.text,
        tuple((s.segment_id, s.start, s.end) for s in p.segments),
    )
    descriptor = ModelDescriptor("offline-fixture", "1")
    accepted = AcceptedCandidateLink(
        a.candidates[0].candidate_id,
        "evt_" + "a" * 64,
        ("cclaim_" + "a" * 64,),
        ("pclaim_" + "a" * 64,),
    )
    source_acq = p.document.source_lineage
    acq = ExtractionAcquisition(
        **{f.name: getattr(source_acq, f.name) for f in fields(source_acq)},
        extraction_available_ns=200,
        processing_fingerprint="a" * 64,
    )
    return (
        p.document,
        p.normalized,
        p.normalized.mapping,
        p.normalized.mapping.settings,
        p.normalized.mapping.settings.limits,
        p.normalized.mapping.runs[0],
        *p.segments,
        *p.entities,
        *p.numerics,
        *p.temporal,
        *p.periods,
        *a.candidates,
        *a.verifications,
        *a.spans,
        a.run,
        n,
        n.span,
        *n.numerics,
        n.period,
        descriptor,
        accepted,
        PromptTemplate(),
        data,
        ExtractionRequest(PromptTemplate(), descriptor, data),
        source(),
        acq,
    )


CASES = [
    (value, f.name) for value in contracts() if value is not None for f in fields(value) if f.init
]


@pytest.mark.parametrize(
    "value,name",
    CASES,
    ids=lambda value: type(value).__name__ if not isinstance(value, str) else value,
)
def test_wrong_typed_fields_fail_closed(value, name):
    with pytest.raises(ValidationError):
        replace(value, **{name: object()})


def test_all_persistable_contracts_roundtrip_and_are_frozen():
    for value in contracts():
        if value is None:
            continue
        assert loads(dumps(value)) == value
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "mutate")


@pytest.mark.parametrize("name", [f.name for f in fields(TextLimits())])
@pytest.mark.parametrize("invalid", [0, -1, True, 1_000_000_000])
def test_all_resource_caps_are_explicit(name, invalid):
    with pytest.raises(ValidationError):
        replace(TextLimits(), **{name: invalid})


@pytest.mark.parametrize(
    "identifier",
    [
        "../../escape",
        "sk-synthetic-not-a-secret",
        "ghp_synthetic",
        "xoxb-synthetic",
        "a" * 81,
        "contains spaces",
    ],
)
def test_model_identifiers_cannot_store_credentials_or_paths(identifier):
    with pytest.raises(ValidationError):
        ModelDescriptor(identifier, "1")


def test_contract_semantic_checks():
    a = artifact()
    p = a.prepared
    for kwargs in (
        {"schema_version": "9"},
        {"currency": "US"},
        {"timezone": "Invalid/Zone"},
        {"action": SourceAction.CORRECTED},
        {"previous_document_id": "bad"},
    ):
        with pytest.raises(ValidationError):
            replace(p.document, **kwargs)
    for kwargs in ({"start": 1, "end": 1}, {"end": 1_000_001}, {"fragment_hash": "bad"}):
        with pytest.raises(ValidationError):
            replace(a.candidates[0].proposal.span, **kwargs)
    with pytest.raises(ValidationError):
        replace(p.normalized, text="changed")
    with pytest.raises(ValidationError):
        replace(p.normalized.mapping, runs=())
    with pytest.raises(ValidationError):
        replace(p.normalized.mapping.runs[0], original_end=1)
    with pytest.raises(ValidationError):
        replace(p.segments[0], index=-1)
    with pytest.raises(ValidationError):
        replace(p.numerics[0], bound=NumericBound.CLOSED_RANGE)
    with pytest.raises(ValidationError):
        replace(
            a.verifications[0],
            state=VerificationState.VERIFIED,
            reasons=(VerificationReason.NUMERIC_MISMATCH,),
        )
    with pytest.raises(ValidationError):
        replace(a.candidates[0].proposal, key="../bad")
    with pytest.raises(ValidationError):
        replace(a.run, status="SUCCESS")
    with pytest.raises(ValidationError):
        replace(a.run, model=ModelDescriptor("offline", "1"))
    with pytest.raises(ValidationError):
        replace(
            a.run,
            accepted=(
                AcceptedCandidateLink(
                    a.candidates[0].candidate_id,
                    "evt_" + "a" * 64,
                    ("cclaim_" + "a" * 64,),
                    ("pclaim_" + "a" * 64,),
                ),
            ),
        )
    with pytest.raises(ValidationError):
        replace(a.run, receipt_id="receipt-without-accepted-claims")
    with pytest.raises(ValidationError):
        replace(a.run, failures=("not typed",))
    with pytest.raises(ValidationError):
        ModelDescriptor("valid", "1", temperature=float("nan"))
    assert sha256(p.original.encode()).hexdigest() == p.document.raw.payload_hash
    assert p.original not in repr(p) and p.original not in repr(p.normalized)


def test_independent_hashseed_processes_are_byte_identical():
    code = (
        "from nlp_helpers import artifact; from eie.persistence.codec import dumps; "
        "a=artifact(); print(dumps((a.run,a.candidates,a.verifications,a.spans)).hex())"
    )
    outputs = []
    for seed in ("1", "71", "98765"):
        env = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "src:.:tests",
        }
        outputs.append(subprocess.check_output([sys.executable, "-c", code], env=env))
    assert len(set(outputs)) == 1
