"""Offline envelope adapter. No transport, credential lookup or source-driven actions."""

from eie.evidence.references import AcquisitionLineage
from eie.nlp.documents import TextDocument
from eie.nlp.schema import DocumentType, SourceAction, TextFormat
from eie.providers.text.models import TextProvider
from eie.sources.models import RawSourceObservation


def document(
    provider: TextProvider,
    raw: RawSourceObservation,
    lineage: AcquisitionLineage,
    *,
    resource_id: str = "documents",
    document_type: DocumentType = DocumentType.NEWS_ARTICLE,
    format: TextFormat = TextFormat.PLAIN,
    language: str = "en",
    title: str | None = None,
    action: SourceAction = SourceAction.ORIGINAL,
    previous_document_id: str | None = None,
) -> TextDocument:
    return TextDocument(
        raw,
        provider.evidence.dataset.dataset_id,
        resource_id,
        provider.evidence.source_family,
        document_type,
        format,
        language,
        lineage,
        provider.evidence.archive_policy,
        action,
        title,
        provider.timezone,
        provider.currency,
        previous_document_id,
    )
