"""Optional in-memory fragment verification; no automatic archive or file access."""

import csv
import io
import json
import re
from hashlib import sha256
from xml.etree import ElementTree

from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.evidence.references import EvidenceReference, LocatorKind, SpanKind


def verify_fragments(payload: bytes, references: tuple[EvidenceReference, ...]) -> None:
    for ref in references:
        locator = ref.locator
        fragment: bytes
        try:
            if locator.span_kind is not None:
                assert locator.start is not None and locator.end is not None
                content = payload if locator.span_kind is SpanKind.BYTE else payload.decode("utf-8")
                if len(content) != locator.content_length:
                    raise ValidationError("evidence span content length mismatch")
                selected = content[locator.start : locator.end]
                fragment = selected if isinstance(selected, bytes) else selected.encode("utf-8")
            elif locator.kind in (
                LocatorKind.JSON_POINTER,
                LocatorKind.FIELD,
                LocatorKind.SEC_METADATA,
            ):
                from eie.persistence.codec import strict_json

                value = strict_json(payload)
                parts = (
                    locator.path[1:].split("/")
                    if locator.kind is LocatorKind.JSON_POINTER
                    else [locator.path]
                )
                for part in parts:
                    part = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(value, dict):
                        value = value[part]
                    elif isinstance(value, list) and part.isascii() and part.isdecimal():
                        value = value[int(part)]
                    else:
                        raise ValidationError("evidence field cannot be located")
                fragment = canonical_json_bytes(value)
            elif locator.kind is LocatorKind.CSV_FIELD:
                rows = csv.DictReader(io.StringIO(payload.decode("utf-8")))
                if rows.fieldnames is None or len(rows.fieldnames) != len(set(rows.fieldnames)):
                    raise ValidationError("CSV evidence fields must have distinct names")
                assert locator.row is not None
                if locator.row > 10000:
                    raise ValidationError("CSV evidence row bound exceeded")
                record = next(r for i, r in enumerate(rows) if i == locator.row)
                fragment = canonical_json_bytes(record[locator.path])
            else:
                if (
                    b"\x00" in payload
                    or b"<!DOCTYPE" in payload.upper()
                    or b"<!ENTITY" in payload.upper()
                    or re.fullmatch(
                        r"/[A-Za-z_][A-Za-z0-9_.-]*(?:/[A-Za-z_][A-Za-z0-9_.-]*)*", locator.path
                    )
                    is None
                ):
                    raise ValidationError("static safe XML field location required")
                root = ElementTree.fromstring(payload)
                parts = locator.path[1:].split("/")
                if parts[0] != root.tag:
                    raise ValidationError("XML root disagrees with evidence location")
                element = root
                for part in parts[1:]:
                    children = list(element.findall(part))
                    if len(children) != 1:
                        raise ValidationError("XML evidence field must be unique")
                    element = children[0]
                fragment = canonical_json_bytes(element.text)
            if sha256(fragment).hexdigest() != ref.fragment_hash:
                raise ValidationError("evidence fragment hash mismatch")
        except (
            UnicodeError,
            ValueError,
            KeyError,
            IndexError,
            StopIteration,
            ElementTree.ParseError,
            json.JSONDecodeError,
        ) as exc:
            raise ValidationError("invalid structured evidence fragment") from exc
