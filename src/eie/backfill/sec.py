"""Current SEC submissions plus validated historical metadata references; no documents."""

import json
import re
from threading import Event
from typing import cast

from eie.backfill.models import (
    CoverageClaim,
    HistoricalCapability,
    Partition,
    ReconstructionQuality,
    SourceSpec,
)
from eie.backfill.sources import HistoricalPage
from eie.config.settings import Settings
from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.datasets.models import HistoricalFact
from eie.entities.sec import SecFilerRevision, SecIssuerRevision, sec_filer_id, sec_issuer_id
from eie.filings.store import InMemoryFilingStore
from eie.providers.sec.models import CIK, SecResourceKind
from eie.providers.sec.normalization import ADAPTER_VERSION, filing_observation, normalize_filing
from eie.providers.sec.parser import SUBMISSIONS_VERSION, parse_submissions
from eie.runtime.clock import RuntimeClock
from eie.runtime.providers import sec_provider
from eie.runtime.sources import HttpPollingSource
from eie.sources.trust import SourceClass
from eie.transport.errors import TransientTransportError, TransportPolicyError
from eie.transport.protocols import HttpTransport
from eie.transport.retry import RetryingClient


def resource_cik(resource: str) -> CIK:
    if re.fullmatch(r"cik:[0-9]{10}", resource) is None:
        raise ValidationError("historical SEC resource requires explicit canonical CIK")
    return CIK.parse(resource[4:])


class SecHistoricalSource:
    def __init__(
        self, ciks: tuple[CIK, ...], client: RetryingClient, *, issuer_ciks: tuple[CIK, ...] = ()
    ) -> None:
        if (
            not ciks
            or len(set(ciks)) != len(ciks)
            or client.policy.allowed_hosts != ("data.sec.gov",)
        ):
            raise ValidationError("explicit unique SEC CIK watchlist and SEC transport required")
        if len(set(issuer_ciks)) != len(issuer_ciks) or not set(issuer_ciks) <= set(ciks):
            raise ValidationError("verified issuer CIKs must be unique members of the watchlist")
        self.client = client
        self.spec = SourceSpec(
            "SEC_EDGAR",
            tuple(sorted("cik:" + cik.digits for cik in ciks)),
            ADAPTER_VERSION,
            SUBMISSIONS_VERSION,
            HistoricalCapability(
                max_range_ns=366 * 86_400_000_000_000,
                assumptions=(
                    "CURRENT_METADATA_NOT_COMPLETE_VINTAGE_HISTORY",
                    "HISTORICAL_LISTINGS_NOT_INFERRED_FROM_CURRENT_TICKERS",
                ),
            ),
            SourceClass.OFFICIAL_PRIMARY,
            tuple(sorted(("cik:" + cik.digits, sec_issuer_id(cik)) for cik in issuer_ciks)),
        )

    def fetch(self, partition: Partition, cursor: str | None, stop: Event) -> HistoricalPage:
        cik = resource_cik(partition.resource_id)
        current_url = f"https://data.sec.gov/submissions/CIK{cik.digits}.json"
        names: tuple[str, ...] = ()
        index = -1
        if cursor is not None:
            try:
                value = json.loads(cursor)
                if not isinstance(value, dict) or set(value) != {"names", "index"}:
                    raise ValueError
                names, index = tuple(value["names"]), value["index"]
                if (
                    not names
                    or len(names) > self.spec.capability.max_pages
                    or isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(names)
                ):
                    raise ValueError
                if names != tuple(sorted(set(names))) or any(
                    not isinstance(n, str)
                    or re.fullmatch(rf"CIK{cik.digits}-submissions-[0-9]+\.json", n) is None
                    for n in names
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError) as exc:
                raise ValidationError("invalid SEC historical reference cursor") from exc
        url = current_url if cursor is None else "https://data.sec.gov/submissions/" + names[index]
        response = self.client.fetch(url, stop)
        if response.status != 200:
            if response.status in (429, 500, 502, 503, 504):
                raise TransientTransportError("SEC historical resource temporarily unavailable")
            raise TransportPolicyError("SEC historical resource unavailable")
        if cursor is None:
            snapshot = parse_submissions(response.body, url, expected_cik=cik)
            names = tuple(ref.name for ref in snapshot.history_files)
            if len(names) > self.spec.capability.max_pages - 1 or any(
                re.fullmatch(rf"CIK{cik.digits}-submissions-[0-9]+\.json", n) is None for n in names
            ):
                raise ValidationError("SEC historical reference chain exceeds policy or CIK")
        next_index = index + 1
        next_cursor = (
            None
            if next_index >= len(names)
            else canonical_json_bytes({"names": names, "index": next_index}).decode()
        )
        return HistoricalPage(
            response.body, url, next_cursor=next_cursor, coverage=CoverageClaim.UNKNOWN
        )


class SecHistoryProcessor:
    adapter_version = ADAPTER_VERSION
    parser_version = SUBMISSIONS_VERSION

    def __init__(self, issuer_ciks: tuple[CIK, ...] = ()) -> None:
        self.identity_bindings = tuple(
            sorted(("cik:" + cik.digits, sec_issuer_id(cik)) for cik in issuer_ciks)
        )

    def normalize(
        self, page: HistoricalPage, partition: Partition, acquired_ns: int, settings: Settings
    ) -> tuple[HistoricalFact, ...]:
        cik = resource_cik(partition.resource_id)
        current_url = f"https://data.sec.gov/submissions/CIK{cik.digits}.json"
        kind = (
            SecResourceKind.SUBMISSIONS
            if page.source_reference == current_url
            else SecResourceKind.HISTORY
        )
        snapshot = parse_submissions(page.body, page.source_reference, expected_cik=cik, kind=kind)
        filings = InMemoryFilingStore()
        result = []
        for item in sorted(
            snapshot.filings, key=lambda f: (f.accepted_at_ns, f.accession.formatted)
        ):
            if not partition.interval.contains(item.accepted_at_ns):
                continue
            raw = filing_observation(item, acquired_ns, acquired_ns)
            filer = SecFilerRevision(
                sec_filer_id(cik), cik, "SEC CIK " + cik.digits, (), acquired_ns, raw.observation_id
            )
            issuer_cik = item.issuer_cik or (item.cik if item.form.base != "4" else None)
            issuer = None
            if issuer_cik is not None and "cik:" + issuer_cik.digits in dict(
                self.identity_bindings
            ):
                issuer = SecIssuerRevision(
                    sec_issuer_id(issuer_cik),
                    issuer_cik,
                    "Verified SEC CIK " + issuer_cik.digits,
                    (),
                    acquired_ns,
                    raw.observation_id,
                )
            normalized = normalize_filing(
                item, raw, filer, issuer, filings, acquired_ns, acquired_ns
            )
            if normalized is None:
                continue
            metadata, event = normalized
            filings.append(metadata)
            for record, identity in ((metadata, metadata.filing_id), (event, event.event_id)):
                result.append(
                    HistoricalFact(
                        "SEC_EDGAR",
                        partition.resource_id,
                        identity,
                        1,
                        item.accepted_at_ns,
                        ReconstructionQuality.SOURCE_TIMESTAMP_RECONSTRUCTED,
                        raw,
                        record,
                        self.adapter_version,
                        self.parser_version,
                    )
                )
        return tuple(result)


def sec_history(
    ciks: tuple[CIK, ...],
    user_agent: str,
    clock: RuntimeClock,
    *,
    transport: HttpTransport | None = None,
    issuer_ciks: tuple[CIK, ...] = (),
) -> tuple[SecHistoricalSource, SecHistoryProcessor]:
    if not ciks:
        raise ValidationError("SEC historical watchlist cannot be empty")
    registration = sec_provider(ciks[0], user_agent, clock, transport=transport)
    client = cast(HttpPollingSource, registration.source).client
    return SecHistoricalSource(ciks, client, issuer_ciks=issuer_ciks), SecHistoryProcessor(
        issuer_ciks
    )
