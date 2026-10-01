"""Pure parsers for SEC's submissions and ticker association JSON shapes."""

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.core.time import datetime_to_ns
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.providers.sec.models import (
    CIK,
    Accession,
    HistoryFileRef,
    SecFilingItem,
    SecForm,
    SecResourceKind,
    SecSnapshot,
    SecTickerMap,
    iso_date,
    validate_sec_url,
)

SUBMISSIONS_VERSION = "sec-submissions-json-v1"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
EASTERN = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = (
    "accessionNumber",
    "filingDate",
    "acceptanceDateTime",
    "form",
    "primaryDocument",
)


def _json(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderParseError("invalid SEC JSON") from exc
    if not isinstance(value, dict):
        raise ProviderSchemaMismatchError("SEC JSON root must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderSchemaMismatchError(f"SEC field {field} requires trimmed text")
    return value


def acceptance_ns(value: object) -> int:
    text = _text(value, "acceptanceDateTime")
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("invalid SEC acceptance timestamp") from exc
    if stamp.tzinfo is None:
        if (
            re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?", text
            )
            is None
        ):
            raise ValidationError("unsupported SEC acceptance timestamp")
        first = stamp.replace(tzinfo=EASTERN, fold=0)
        second = stamp.replace(tzinfo=EASTERN, fold=1)
        if (
            first.utcoffset() != second.utcoffset()
            or first.astimezone(UTC).astimezone(EASTERN).replace(tzinfo=None) != stamp
        ):
            raise ValidationError("ambiguous or nonexistent SEC acceptance time")
        stamp = first
    return datetime_to_ns(stamp)


def _rows(columns: object, cik: CIK, source_url: str) -> tuple[SecFilingItem, ...]:
    if not isinstance(columns, dict):
        raise ProviderSchemaMismatchError("SEC filing columns missing")
    arrays: dict[str, list[object]] = {}
    for name in REQUIRED_COLUMNS:
        values = columns.get(name)
        if not isinstance(values, list):
            raise ProviderSchemaMismatchError(f"missing SEC filing column {name}")
        arrays[name] = values
    size = len(arrays[REQUIRED_COLUMNS[0]])
    if any(len(values) != size for values in arrays.values()):
        raise ProviderSchemaMismatchError("SEC filing column lengths disagree")
    for name in ("reportDate", "fileNumber", "issuerCik"):
        values = columns.get(name)
        if values is not None:
            if not isinstance(values, list) or len(values) != size:
                raise ProviderSchemaMismatchError(f"invalid SEC filing column {name}")
            arrays[name] = values
    result: list[SecFilingItem] = []
    for index in range(size):
        row = {key: values[index] for key, values in arrays.items()}
        source_fragment = {
            key: values[index]
            for key, values in columns.items()
            if isinstance(values, list) and len(values) == size
        }
        report = row.get("reportDate")
        file_number = row.get("fileNumber")
        issuer_cik = row.get("issuerCik")
        result.append(
            SecFilingItem(
                cik,
                Accession.parse(row["accessionNumber"]),
                SecForm.parse(row["form"]),
                iso_date(row["filingDate"], "filingDate"),
                acceptance_ns(row["acceptanceDateTime"]),
                None if report in (None, "") else iso_date(report, "reportDate"),
                _text(row["primaryDocument"], "primaryDocument"),
                None if file_number in (None, "") else _text(file_number, "fileNumber"),
                source_url,
                sha256(canonical_json_bytes(source_fragment)).hexdigest(),
                None if issuer_cik in (None, "") else CIK.parse(issuer_cik),
            )
        )
    return tuple(sorted(result, key=lambda item: item.accession.formatted))


def parse_submissions(
    payload: bytes,
    source_url: str,
    *,
    expected_cik: CIK | None = None,
    kind: SecResourceKind = SecResourceKind.SUBMISSIONS,
) -> SecSnapshot:
    validate_sec_url(source_url)
    root = _json(payload)
    if kind is SecResourceKind.SUBMISSIONS:
        cik = CIK.parse(root.get("cik"))
        expected_url = f"https://data.sec.gov/submissions/CIK{cik.digits}.json"
        if source_url != expected_url:
            raise ProviderSchemaMismatchError("SEC submissions URL disagrees with CIK")
        if expected_cik is not None and cik != expected_cik:
            raise ProviderSchemaMismatchError("SEC submission CIK disagrees with request")
        company = _text(root.get("name"), "name")
        filings = root.get("filings")
        if not isinstance(filings, dict) or not isinstance(filings.get("files"), list):
            raise ProviderSchemaMismatchError("SEC submissions filing layout changed")
        rows = _rows(filings.get("recent"), cik, source_url)
        refs: list[HistoryFileRef] = []
        for value in filings["files"]:
            if not isinstance(value, dict):
                raise ProviderSchemaMismatchError("invalid SEC history reference")
            name = _text(value.get("name"), "history file name")
            refs.append(
                HistoryFileRef(
                    name,
                    iso_date(value.get("filingFrom"), "filingFrom"),
                    iso_date(value.get("filingTo"), "filingTo"),
                    "https://data.sec.gov/submissions/" + name,
                )
            )
        former = root.get("formerNames", [])
        if not isinstance(former, list):
            raise ProviderSchemaMismatchError("SEC formerNames layout changed")
        aliases = []
        for value in former:
            if not isinstance(value, dict):
                raise ProviderSchemaMismatchError("invalid SEC former name")
            aliases.append(_text(value.get("name"), "former name"))
        tickers, exchanges = root.get("tickers", []), root.get("exchanges", [])
        if (
            not isinstance(tickers, list)
            or not isinstance(exchanges, list)
            or len(tickers) != len(exchanges)
        ):
            raise ProviderSchemaMismatchError("SEC ticker/exchange columns disagree")
        ticker_exchange = tuple(
            sorted(
                {
                    (_text(ticker, "ticker"), _text(exchange, "exchange"))
                    for ticker, exchange in zip(tickers, exchanges, strict=True)
                }
            )
        )
        entity_type = root.get("entityType")
        if entity_type is not None:
            entity_type = _text(entity_type, "entityType")
        return SecSnapshot(
            kind,
            cik,
            company,
            tuple(sorted(set(aliases))),
            ticker_exchange,
            rows,
            tuple(sorted(refs, key=lambda item: item.name)),
            source_url,
            sha256(canonical_json_bytes(root)).hexdigest(),
            entity_type,
        )
    if expected_cik is None or kind is not SecResourceKind.HISTORY:
        raise ValidationError("SEC history requires explicit CIK and resource kind")
    if not source_url.startswith(
        f"https://data.sec.gov/submissions/CIK{expected_cik.digits}-submissions-"
    ):
        raise ProviderSchemaMismatchError("SEC history URL disagrees with CIK")
    rows = _rows(root, expected_cik, source_url)
    return SecSnapshot(
        kind,
        expected_cik,
        None,
        (),
        (),
        rows,
        (),
        source_url,
        sha256(canonical_json_bytes(root)).hexdigest(),
    )


def parse_ticker_map(payload: bytes) -> SecTickerMap:
    root = _json(payload)
    fields, data = root.get("fields"), root.get("data")
    if fields != ["cik", "name", "ticker", "exchange"] or not isinstance(data, list):
        raise ProviderSchemaMismatchError("SEC ticker map layout changed")
    rows: list[tuple[CIK, str, str, str]] = []
    for value in data:
        if not isinstance(value, list) or len(value) != 4:
            raise ProviderSchemaMismatchError("SEC ticker map row changed")
        rows.append(
            (
                CIK.parse(value[0]),
                _text(value[1], "name"),
                _text(value[2], "ticker"),
                _text(value[3], "exchange"),
            )
        )
    return SecTickerMap(
        tuple(sorted(set(rows), key=lambda row: (row[0].digits, row[2], row[3], row[1]))),
        sha256(canonical_json_bytes(root)).hexdigest(),
    )
