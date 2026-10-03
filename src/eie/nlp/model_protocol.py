"""A model receives data and returns bytes; runtime capabilities are never supplied."""

from dataclasses import dataclass, field
from typing import Protocol

from eie.nlp.models import ModelDescriptor
from eie.nlp.prompts import ExtractionRequest


class StructuredExtractionModel(Protocol):
    @property
    def descriptor(self) -> ModelDescriptor: ...

    def extract(self, request: ExtractionRequest) -> bytes: ...


@dataclass(frozen=True, slots=True)
class OfflineExtractionModel:
    """Deterministic explicit response fixture; no transport, tools or side effects."""

    descriptor: ModelDescriptor
    response: bytes = field(repr=False)

    def extract(self, request: ExtractionRequest) -> bytes:
        if request.model != self.descriptor:
            raise ValueError("model request descriptor mismatch")
        return self.response
