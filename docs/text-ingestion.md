# Text ingestion (EIE-12)

`TextProvider` wraps an existing configured `EvidenceProvider`. Source identity,
source family, dataset, acquisition parser, normalized retention, raw retention,
currency and reference timezone are trusted configuration. A document cannot grant
itself permissions or source authority. No text transport is included.

`providers.text.adapter.document()` creates a frozen `TextDocument` from a
`RawSourceObservation` and its exact `AcquisitionLineage`. It records document type,
language, format, source action, optional title and preceding source revision.
The document ID hashes the metadata and source content hash. The document thread
pins dataset, provider, resource and source item. Changed content requires a new
source version; corrections and withdrawals identify an earlier document.

## Normalization and locations

`normalize(payload, format, settings)` accepts strict UTF-8. It rejects unsupported
controls and oversized input; it never silently truncates. CRLF/CR become LF and
Unicode clusters become NFC, including Hangul composition. Whitespace is otherwise
preserved. A hash-pinned `NormalizationMap` compresses contiguous linear offsets
and explicitly maps composed characters/entities to their original ranges.
Offsets count Unicode characters, never UTF-8 bytes.

Explicit HTML uses the standard library's inert parser. Script, style, template,
noscript and comments are excluded. Attributes, URLs and scripts are never executed
or fetched. Incomplete syntax, declarations, processing instructions, unsupported
tables, oversized tags and excessive hidden-element nesting fail closed. This is
a restricted text conversion policy, not a browser or general HTML table extractor.

The first nonblank line is a headline segment; later nonblank lines are paragraphs.
Segments preserve offsets, fragment hash, document/map IDs and dense ordinal.
A separate supplied title is metadata and does not independently establish a fact.

## Bounds and retention

Defaults: 1 MB input, 200,000 characters, 100,000 estimated tokens, 512 segments,
1,024 total mentions, 128 combined candidates, 2,048 spans, 1 MB model output,
1,000 accepted claims and 10,000 query results. Token estimation uses UTF-8 byte
length as a conservative proxy; an external model adapter must honor its own
context limits. Limits are typed, positive and bounded. Oversized input raises an
explicit validation error before a completed extraction can be stored.

Normalized fields require `RetentionPermission.ALLOWED`. Raw content defaults to
metadata only. `BOUNDED_RAW` additionally requires explicit permission and its byte
limit. Licensed original bytes are stored once by SHA-256. A metadata-only provider
cannot retrieve another provider's retained content through a shared hash.
Normalized bodies and model request bodies are not repeated in claims or manifests.
Content not retained remains hash/reference backed; restart validation reports
`VALID_WITH_WARNINGS` because exact source-body recalculation is unavailable.

## History

`TextEngine.ingest()` accepts an explicit extraction availability clock. Source
observed/ingested clocks remain unchanged. Accepted claims use an additive
`ExtractionAcquisition` subtype whose knowledge time includes actual processing
availability. Historical documents pin a completed EIE-7 build, exact fact and exact
archive acquisition. Source availability does not imply that a future extraction
was already available historically.

New model, prompt, parser settings, availability or reprocessing predecessor create
new immutable runs. Same inputs are idempotent. Source corrections update EIE-11
assertions; explicit source withdrawals retract active source-item claim threads.
Old documents, extractions and claim values remain available at earlier cutoffs.
For a separately selected historical re-extraction dataset, use an explicit new
build/dataset lineage; never overwrite a completed old build.
