"""Bounded inert normalization with a reversible location map, never semantic rewriting."""

import re
import unicodedata
from dataclasses import dataclass, field
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.nlp.checks import bounded, checked
from eie.nlp.schema import TextFormat
from eie.studies.checks import digest


@dataclass(frozen=True, slots=True)
class TextLimits:
    document_bytes: int = 1_000_000
    document_characters: int = 200_000
    estimated_tokens: int = 100_000
    segments: int = 512
    mentions: int = 1024
    candidates: int = 128
    spans: int = 2048
    model_output_bytes: int = 1_000_000
    accepted_claims: int = 1000
    query_size: int = 10000

    def __post_init__(self) -> None:
        checked(self)
        for v, maximum in (
            (self.document_bytes, 10_000_000),
            (self.document_characters, 1_000_000),
            (self.estimated_tokens, 1_000_000),
            (self.segments, 4096),
            (self.mentions, 10000),
            (self.candidates, 1000),
            (self.spans, 10000),
            (self.model_output_bytes, 10_000_000),
            (self.accepted_claims, 10000),
            (self.query_size, 100000),
        ):
            bounded(v, maximum)


@dataclass(frozen=True, slots=True)
class NormalizationSettings:
    version: str = "mapped-nfc-1"
    limits: TextLimits = TextLimits()

    def __post_init__(self) -> None:
        checked(self)


@dataclass(frozen=True, slots=True)
class OffsetRun:
    normalized_start: int
    normalized_end: int
    original_start: int
    original_end: int
    linear: bool

    def __post_init__(self) -> None:
        checked(self)
        if not (
            0 <= self.normalized_start < self.normalized_end
            and 0 <= self.original_start < self.original_end
        ):
            raise ValidationError("positive normalization map coordinates required")
        if (
            self.linear
            and self.normalized_end - self.normalized_start
            != self.original_end - self.original_start
        ):
            raise ValidationError("linear map must preserve character length")


@dataclass(frozen=True, slots=True)
class NormalizationMap:
    original_hash: str
    normalized_hash: str
    original_characters: int
    normalized_characters: int
    runs: tuple[OffsetRun, ...]
    settings: NormalizationSettings
    format: TextFormat
    transformations: tuple[str, ...]

    def __post_init__(self) -> None:
        checked(self)
        digest(self.original_hash)
        digest(self.normalized_hash)
        cursor = 0
        prior = 0
        for row in self.runs:
            if (
                row.normalized_start != cursor
                or row.original_start < prior
                or row.original_end > self.original_characters
            ):
                raise ValidationError("complete ordered normalization mapping required")
            cursor, prior = row.normalized_end, row.original_start
        if cursor != self.normalized_characters:
            raise ValidationError("normalization map does not cover normalized text")

    @property
    def map_id(self) -> str:
        return "tmap_" + fingerprint("eie:text-normalization-map:v1", self)

    def original_range(self, start: int, end: int) -> tuple[int, int]:
        if not 0 <= start < end <= self.normalized_characters:
            raise ValidationError("normalized evidence offsets outside text")
        selected = tuple(
            r for r in self.runs if r.normalized_start < end and start < r.normalized_end
        )
        first, last = selected[0], selected[-1]
        a = (
            first.original_start + start - first.normalized_start
            if first.linear
            else first.original_start
        )
        b = last.original_start + end - last.normalized_start if last.linear else last.original_end
        return a, b


@dataclass(frozen=True, slots=True)
class NormalizedText:
    text: str = field(repr=False)
    mapping: NormalizationMap

    def __post_init__(self) -> None:
        checked(self, text_fields=frozenset({"text"}))
        if (
            len(self.text) != self.mapping.normalized_characters
            or sha256(self.text.encode()).hexdigest() != self.mapping.normalized_hash
        ):
            raise ValidationError("normalized text hash/length mismatch")


DEFAULT_NORMALIZATION = NormalizationSettings()


_BLOCK = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "section", "article"})
_HIDDEN = frozenset({"script", "style", "noscript", "template"})
_TABLE = frozenset({"table", "tr", "td", "th", "thead", "tbody", "tfoot"})


class _InertHTML(HTMLParser):
    def __init__(self, original: str) -> None:
        super().__init__(convert_charrefs=False)
        self.original = original
        self.lines = [0, *(m.end() for m in re.finditer("\n", original))]
        self.characters: list[tuple[str, int, int]] = []
        self.hidden: list[str] = []

    def position(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def tag(self, name: str, closing: bool, length: int) -> None:
        start = self.position()
        if length > 8192 or name in _TABLE:
            raise ValidationError("bounded HTML tags required; tables are unsupported")
        if name in _HIDDEN:
            if closing:
                if not self.hidden or self.hidden[-1] != name:
                    raise ValidationError("malformed hidden HTML nesting")
                self.hidden.pop()
            else:
                if len(self.hidden) >= 128:
                    raise ValidationError("hidden HTML nesting bound exceeded")
                self.hidden.append(name)
        elif name in _BLOCK and not self.hidden:
            self.characters.append(("\n", start, start + length))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw_tag = self.get_starttag_text()
        assert raw_tag is not None
        self.tag(tag, False, len(raw_tag))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            raise ValidationError("self-closing hidden HTML is unsupported")
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        start = self.position()
        end = self.original.find(">", start)
        if end < 0:
            raise ValidationError("unclosed HTML tag")
        self.tag(tag, True, end - start + 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            if re.search(r"<\s*/?\s*(?:script|style|noscript|template)\b", data, re.I):
                raise ValidationError("malformed hidden HTML tag")
            start = self.position()
            self.characters.extend((c, start + i, start + i + 1) for i, c in enumerate(data))

    def entity(self, name: str, prefix: str) -> None:
        if not self.hidden:
            start = self.position()
            raw = prefix + name
            if self.original[start + len(raw) : start + len(raw) + 1] == ";":
                raw += ";"
            self.characters.extend((c, start, start + len(raw)) for c in unescape(raw))

    def handle_entityref(self, name: str) -> None:
        self.entity(name, "&")

    def handle_charref(self, name: str) -> None:
        self.entity(name, "&#")

    def handle_decl(self, decl: str) -> None:
        raise ValidationError("HTML declarations are unsupported")

    def unknown_decl(self, data: str) -> None:
        raise ValidationError("HTML declarations are unsupported")

    def handle_pi(self, data: str) -> None:
        raise ValidationError("HTML processing instructions are unsupported")


def _characters(original: str, format: TextFormat) -> list[tuple[str, int, int]]:
    if format is TextFormat.PLAIN:
        return [(c, i, i + 1) for i, c in enumerate(original)]
    if "<!doctype" in original.lower() or "<!entity" in original.lower():
        raise ValidationError("HTML declarations are unsupported")
    parser = _InertHTML(original)
    parser.feed(original)
    if parser.rawdata:
        raise ValidationError("incomplete HTML syntax is unsupported")
    parser.close()
    if parser.hidden:
        raise ValidationError("unclosed hidden HTML element")
    return parser.characters


def normalize(
    payload: bytes,
    format: TextFormat = TextFormat.PLAIN,
    settings: NormalizationSettings = DEFAULT_NORMALIZATION,
) -> NormalizedText:
    if not isinstance(payload, bytes) or len(payload) > settings.limits.document_bytes:
        raise ValidationError("document byte bound exceeded")
    try:
        original = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValidationError("UTF-8 source text required") from exc
    if len(original) > settings.limits.document_characters or any(
        unicodedata.category(c) in ("Cc", "Cs") and c not in "\r\n\t" for c in original
    ):
        raise ValidationError("document character/control policy violated")
    chars = _characters(original, format)
    transformed: list[tuple[str, int, int]] = []
    i = 0
    while i < len(chars):
        c, start, end = chars[i]
        if c == "\r":
            if i + 1 < len(chars) and chars[i + 1][0] == "\n":
                i += 1
                end = chars[i][2]
            transformed.append(("\n", start, end))
        else:
            cluster = c
            while i + 1 < len(chars) and (
                unicodedata.combining(chars[i + 1][0])
                or unicodedata.normalize("NFC", cluster + chars[i + 1][0])
                != unicodedata.normalize("NFC", cluster)
                + unicodedata.normalize("NFC", chars[i + 1][0])
            ):
                i += 1
                cluster += chars[i][0]
                end = chars[i][2]
            transformed.extend((v, start, end) for v in unicodedata.normalize("NFC", cluster))
        i += 1
    text = "".join(c for c, _, _ in transformed)
    if (
        len(text) > settings.limits.document_characters
        or len(text.encode()) > settings.limits.document_bytes
        or len(text.encode()) > settings.limits.estimated_tokens
    ):
        raise ValidationError("normalized document/token upper bound exceeded")
    runs: list[OffsetRun] = []
    for i, (_, a, b) in enumerate(transformed):
        row = OffsetRun(i, i + 1, a, b, b - a == 1)
        if runs and row.linear and runs[-1].linear and runs[-1].original_end == a:
            old = runs.pop()
            row = OffsetRun(old.normalized_start, i + 1, old.original_start, b, True)
        runs.append(row)
    mapping = NormalizationMap(
        sha256(payload).hexdigest(),
        sha256(text.encode()).hexdigest(),
        len(original),
        len(text),
        tuple(runs),
        settings,
        format,
        (
            "UTF8_STRICT",
            "CRLF_TO_LF",
            "NFC_CLUSTER",
            "UTF8_BYTE_TOKEN_BOUND",
            "HTML_INERT" if format is TextFormat.HTML else "PLAIN",
        ),
    )
    return NormalizedText(text, mapping)
