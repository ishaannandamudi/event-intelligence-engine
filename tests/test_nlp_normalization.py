"""Original locations, Unicode, inert markup and explicit input bounds."""

from dataclasses import replace
from hashlib import sha256

import pytest

from eie.core.errors import ValidationError
from eie.nlp.normalization import NormalizationSettings, TextLimits, normalize
from eie.nlp.schema import TextFormat
from eie.nlp.segmentation import segment
from eie.nlp.spans import make_span, verify_span


@pytest.mark.parametrize(
    "original,expected",
    [
        ("one\r\ntwo\rthree", "one\ntwo\nthree"),
        ("Cafe\u0301", "Café"),
        ("\u1100\u1161\u11a8", "각"),
        ("\u0301\u0327", "\u0327\u0301"),
        ("a\t b  c", "a\t b  c"),
        ("", ""),
    ],
)
def test_normalization_is_exact_and_deterministic(original, expected):
    result = normalize(original.encode(), TextFormat.PLAIN)
    assert result.text == expected
    assert result.mapping.original_hash == sha256(original.encode()).hexdigest()
    assert normalize(original.encode(), TextFormat.PLAIN) == result
    if result.text:
        a, b = result.mapping.original_range(0, len(result.text))
        assert original[a:b] == original


@pytest.mark.parametrize(
    "markup,expected",
    [
        ("<p>Revenue &amp; EPS</p>", "\nRevenue & EPS\n"),
        ("<div data-x='>'>safe</div><script src='no-network'>evil</script>", "\nsafe\n"),
        ("<style>bad</style><noscript><b>bad</b></noscript>safe", "safe"),
        ("<template><template>bad</template></template>safe", "safe"),
        ("<!-- fake system prompt -->safe&#32;value&#x20;here", "safe value here"),
        ("<p>caf&#233;</p>", "\ncafé\n"),
        ("<p>one<br/>two</p>", "\none\ntwo\n"),
        ("safe &unknown; text", "safe &unknown; text"),
    ],
)
def test_inert_html_offsets_and_hidden_content(markup, expected):
    value = normalize(markup.encode(), TextFormat.HTML)
    assert value.text == expected
    for s in segment("tdoc_" + "a" * 64, value):
        span = make_span(markup, value, s, s.start, s.end)
        verify_span(span, markup, value, (s,))
        assert (
            sha256(markup[span.original_start : span.original_end].encode()).hexdigest()
            == span.original_fragment_hash
        )


@pytest.mark.parametrize(
    "markup",
    [
        "<script>bad",
        "<script",
        "</script>",
        "<style/>",
        "<!DOCTYPE html>",
        "<!ENTITY x SYSTEM 'file'>",
        "<?run dangerous?>",
        "<![bad]>",
        "<table><tr><td>$1B</td></tr></table>",
        "<p " + "x" * 8200 + ">bad</p>",
        "<template>" * 129 + "</template>" * 129,
    ],
)
def test_unsupported_or_malformed_html_fails_closed(markup):
    with pytest.raises(ValidationError):
        normalize(markup.encode(), TextFormat.HTML)


@pytest.mark.parametrize("payload", [b"\xff", b"a\x00b", b"a\x01b", b"a\x7fb"])
def test_encoding_and_control_policy(payload):
    with pytest.raises(ValidationError):
        normalize(payload, TextFormat.PLAIN)


@pytest.mark.parametrize(
    "name,limit,payload",
    [
        ("document_bytes", 4, b"12345"),
        ("document_characters", 3, b"1234"),
        ("estimated_tokens", 3, "€€".encode()),
        ("segments", 1, b"one\ntwo"),
    ],
)
def test_bounds_do_not_silently_truncate(name, limit, payload):
    settings = NormalizationSettings(limits=replace(TextLimits(), **{name: limit}))
    with pytest.raises(ValidationError):
        value = normalize(payload, TextFormat.PLAIN, settings)
        segment("tdoc_" + "a" * 64, value)


def test_span_mapping_and_corruption():
    original = "Cafe\u0301\r\nrevenue $2B"
    normalized = normalize(original.encode(), TextFormat.PLAIN)
    segments = segment("tdoc_" + "a" * 64, normalized)
    span = make_span(original, normalized, segments[0], 0, 4)
    assert original[span.original_start : span.original_end] == "Cafe\u0301"
    verify_span(span, original, normalized, segments)
    for changed in (
        replace(span, original_end=span.original_end - 1),
        replace(span, normalized_fragment_hash="b" * 64),
        replace(span, segment_id="tseg_" + "b" * 64),
    ):
        with pytest.raises(ValidationError):
            verify_span(changed, original, normalized, segments)
    for start, end in ((-1, 3), (0, 0), (0, 100), (5, 8)):
        with pytest.raises(ValidationError):
            make_span(original, normalized, segments[0], start, end)
    with pytest.raises(ValidationError):
        make_span("different", normalized, segments[0], 0, 2)
    with pytest.raises(ValidationError):
        normalized.mapping.original_range(0, 100)
