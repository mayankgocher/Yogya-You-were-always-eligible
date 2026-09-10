"""Ingestion from myscheme.gov.in.

myScheme is the Government of India's own aggregator — several thousand central
and state schemes with structured eligibility text. It is the right source, and
this module is how the corpus stays current.

Three things about the design are deliberate:

**Ingestion is offline-first.** It writes a *snapshot* into the corpus
directory; nothing in the running system ever calls myScheme. A portal outage,
a layout change, or a rate limit can therefore never take Yogya down or change
a verdict mid-case. The corpus a decision was made against is recorded in the
audit trail by snapshot id.

**Eligibility is not auto-converted.** The portal's eligibility text is prose.
Turning "applicant should belong to a BPL family and should not be an income
tax payer" into two machine-checkable `Criterion` objects is a translation that
changes who gets money, so this module extracts and *proposes* criteria, marks
them `needs_review`, and refuses to promote a scheme into the active corpus
until a human has signed off. `scripts/review_ingest.py` is that step.

**It degrades honestly.** If the endpoint is unreachable — which it is from
many networks, including sandboxed CI — ingestion fails loudly and leaves the
existing snapshot untouched, rather than writing a half-empty corpus.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import httpx

from yogya.models.household import DocumentKind
from yogya.models.scheme import BenefitKind, Criterion, Operator

logger = logging.getLogger(__name__)

BASE_URL = "https://api.myscheme.gov.in"
SEARCH_PATH = "/search/v4/schemes"
DETAIL_PATH = "/schemes/v5/public/schemes"
PAGE_SIZE = 100
DEFAULT_TIMEOUT = 30.0


class IngestError(RuntimeError):
    pass


@dataclass
class IngestReport:
    snapshot_id: str
    fetched: int = 0
    normalised: int = 0
    needs_review: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    written_to: Path | None = None

    def summary(self) -> str:
        return (
            f"snapshot {self.snapshot_id}: fetched {self.fetched}, "
            f"normalised {self.normalised}, {self.needs_review} need human "
            f"review, {len(self.skipped)} skipped"
        )


class MySchemeClient:
    """Thin HTTP client for the myScheme public API."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        rate_limit_s: float = 0.4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limit_s = rate_limit_s
        self._client = client

    def _get(self, path: str, params: dict | None = None) -> dict:
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = client.get(
                f"{self.base_url}{path}",
                params=params,
                headers={
                    "accept": "application/json",
                    "user-agent": "yogya-ingest/1.0 (welfare entitlement finder)",
                },
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise IngestError(
                f"myScheme request failed for {path}: {exc}. "
                "The corpus on disk has not been modified."
            ) from exc
        finally:
            if self._client is None:
                client.close()

    def iter_schemes(self, state: str | None = None, limit: int | None = None) -> Iterator[dict]:
        """Page through the scheme list."""
        fetched = 0
        offset = 0
        while True:
            payload = self._get(
                SEARCH_PATH,
                params={
                    "lang": "en",
                    "q": json.dumps(
                        [{"identifier": "state", "value": state}] if state else []
                    ),
                    "keyword": "",
                    "sort": "",
                    "from": offset,
                    "size": PAGE_SIZE,
                },
            )
            items = _dig(payload, "data", "hits", "items") or []
            if not items:
                return
            for item in items:
                yield item
                fetched += 1
                if limit and fetched >= limit:
                    return
            offset += PAGE_SIZE
            time.sleep(self.rate_limit_s)

    def scheme_detail(self, slug: str) -> dict:
        return self._get(f"{DETAIL_PATH}/{slug}")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_INCOME = re.compile(
    r"(?:income).{0,40}?(?:not\s+exceed(?:ing)?|less\s+than|below|up\s+to|maximum\s+of)\s*"
    r"(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(lakh|lakhs|crore)?",
    re.IGNORECASE,
)
_AGE_MIN = re.compile(r"(?:age|aged).{0,20}?(?:above|over|at\s+least|minimum)\s*(\d{1,3})", re.I)
_AGE_MAX = re.compile(r"(?:age|aged).{0,20}?(?:below|under|not\s+exceed|maximum)\s*(\d{1,3})", re.I)

_CATEGORY_TERMS = {
    "sc": ("scheduled caste", "sc candidate", "sc student"),
    "st": ("scheduled tribe", "st candidate"),
    "obc": ("other backward class", "obc"),
    "ews": ("economically weaker section", "ews"),
}

_BENEFIT_TERMS = {
    BenefitKind.PENSION: ("pension",),
    BenefitKind.SCHOLARSHIP: ("scholarship", "stipend", "fellowship"),
    BenefitKind.INSURANCE: ("insurance", "cover", "bima"),
    BenefitKind.HOUSING: ("house", "housing", "awas", "dwelling"),
    BenefitKind.FOOD: ("foodgrain", "ration", "nutrition"),
    BenefitKind.SUBSIDY: ("subsidy", "loan", "interest", "concession"),
    BenefitKind.CASH_TRANSFER: ("cash", "financial assistance", "instalment", "dbt"),
}

_DOCUMENT_TERMS = {
    DocumentKind.AADHAAR: ("aadhaar", "aadhar"),
    DocumentKind.RATION_CARD: ("ration card",),
    DocumentKind.INCOME_CERTIFICATE: ("income certificate",),
    DocumentKind.CASTE_CERTIFICATE: ("caste certificate", "community certificate"),
    DocumentKind.DOMICILE_CERTIFICATE: ("domicile", "residence certificate"),
    DocumentKind.BANK_PASSBOOK: ("bank account", "passbook", "bank details"),
    DocumentKind.LAND_RECORD: ("land record", "khatauni", "khasra", "land holding"),
    DocumentKind.DISABILITY_CERTIFICATE: ("disability certificate", "udid"),
    DocumentKind.BIRTH_CERTIFICATE: ("birth certificate",),
    DocumentKind.DEATH_CERTIFICATE: ("death certificate",),
    DocumentKind.MARRIAGE_CERTIFICATE: ("marriage certificate",),
    DocumentKind.SCHOOL_CERTIFICATE: ("bonafide", "school certificate", "marksheet"),
    DocumentKind.LABOUR_CARD: ("labour card", "registration with the board"),
    DocumentKind.PHOTOGRAPH: ("photograph", "passport size"),
}


def _dig(payload: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def _to_rupees(amount: str, unit: str | None) -> int:
    value = float(amount.replace(",", ""))
    if unit and unit.lower().startswith("lakh"):
        value *= 100_000
    elif unit and unit.lower().startswith("crore"):
        value *= 10_000_000
    return int(value)


def propose_criteria(eligibility_text: str, source: str) -> list[Criterion]:
    """Propose machine-checkable criteria from prose. Never authoritative.

    Everything returned here is a *suggestion* for a human reviewer. The
    patterns are narrow and conservative: it is far better to propose three
    criteria a reviewer must supplement than eight they must audit.
    """
    text = " ".join(eligibility_text.split())
    lower = text.lower()
    proposed: list[Criterion] = []
    n = 0

    def add(field: str, op: Operator, value, description: str, remediable=False):
        nonlocal n
        n += 1
        proposed.append(
            Criterion(
                criterion_id=f"proposed-{n}",
                field=field,
                op=op,
                value=value,
                description=description,
                citation=f"{source} (auto-extracted, NEEDS REVIEW)",
                remediable=remediable,
            )
        )

    match = _INCOME.search(text)
    if match:
        ceiling = _to_rupees(match.group(1), match.group(2))
        add(
            "annual_income",
            Operator.LTE,
            ceiling,
            f"Annual family income must not exceed Rs {ceiling:,}.",
        )

    match = _AGE_MIN.search(text)
    if match:
        add("max_age", Operator.GTE, int(match.group(1)),
            f"At least one member must be aged {match.group(1)} or above.")

    match = _AGE_MAX.search(text)
    if match:
        add("min_age", Operator.LTE, int(match.group(1)),
            f"At least one member must be under {match.group(1)}.")

    for category, terms in _CATEGORY_TERMS.items():
        if any(t in lower for t in terms):
            add("social_category", Operator.EQ, category,
                f"The applicant must belong to the {category.upper()} category.")
            break

    if "below poverty line" in lower or "bpl" in lower or "antyodaya" in lower:
        add("ration_card", Operator.IN, ["aay", "phh"],
            "The household must be below the poverty line.", remediable=True)

    if "widow" in lower:
        add("has_widow", Operator.IS_TRUE, None, "The household must include a widow.")
    if "disab" in lower or "divyang" in lower:
        add("has_disabled_member", Operator.IS_TRUE, None,
            "The household must include a member with a disability.")
    if "rural" in lower and "urban" not in lower:
        add("residence", Operator.EQ, "rural", "The household must live in a rural area.")
    elif "urban" in lower and "rural" not in lower:
        add("residence", Operator.EQ, "urban", "The household must live in an urban area.")
    if "income tax" in lower:
        add("is_income_tax_payer", Operator.IS_FALSE, None,
            "No family member may be an income tax payer.")

    return proposed


def infer_benefit_kind(text: str) -> BenefitKind:
    lower = text.lower()
    for kind, terms in _BENEFIT_TERMS.items():
        if any(t in lower for t in terms):
            return kind
    return BenefitKind.SERVICE


def infer_documents(text: str) -> list[DocumentKind]:
    lower = text.lower()
    return [
        doc for doc, terms in _DOCUMENT_TERMS.items() if any(t in lower for t in terms)
    ]


def normalise(raw: dict, snapshot_id: str) -> dict:
    """Map one myScheme record onto Yogya's Scheme shape.

    The result is a dict, not a `Scheme`, and carries `needs_review: True`.
    It is not loadable by the corpus loader until a reviewer removes that flag
    — which is the point.
    """
    slug = raw.get("slug") or raw.get("schemeShortTitle") or raw.get("id", "unknown")
    name = raw.get("schemeName") or raw.get("schemeShortTitle") or slug
    state = (raw.get("state") or {}).get("value") if isinstance(raw.get("state"), dict) else raw.get("state")
    level = "state" if state else "central"

    benefits_text = _flatten_text(raw.get("briefDescription"), raw.get("benefits"))
    eligibility_text = _flatten_text(raw.get("eligibilityDescription"), raw.get("eligibility"))
    documents_text = _flatten_text(raw.get("documents_required"), raw.get("documents"))
    source_url = f"https://www.myscheme.gov.in/schemes/{slug}"

    return {
        "scheme_id": _slugify(slug),
        "name": name,
        "level": level,
        "state": _slugify(state) if state else None,
        "ministry": raw.get("nodalMinistryName") or raw.get("ministry"),
        "benefit_kind": infer_benefit_kind(benefits_text or name).value,
        "benefit_summary": _truncate(benefits_text or name, 400),
        "description": _truncate(raw.get("briefDescription") or "", 1200),
        "criteria": [
            c.model_dump(mode="json")
            for c in propose_criteria(eligibility_text, source_url)
        ],
        "required_documents": [d.value for d in infer_documents(documents_text)],
        "application_mode": "both",
        "application_url": source_url,
        "rule_version": f"{snapshot_id}.0",
        "effective_from": date.today().isoformat(),
        "source_url": source_url,
        "source_snapshot": snapshot_id,
        "keywords": _keywords(name, benefits_text),
        # The gate. Removed by a human in scripts/review_ingest.py.
        "needs_review": True,
        "raw_eligibility_text": eligibility_text,
    }


def ingest(
    *,
    out_dir: Path,
    state: str | None = None,
    limit: int | None = None,
    client: MySchemeClient | None = None,
    snapshot_id: str | None = None,
) -> IngestReport:
    """Fetch, normalise and write a review-pending snapshot."""
    snapshot_id = snapshot_id or f"myscheme-{date.today():%Y-%m}"
    client = client or MySchemeClient()
    report = IngestReport(snapshot_id=snapshot_id)

    normalised: list[dict] = []
    for raw in client.iter_schemes(state=state, limit=limit):
        report.fetched += 1
        try:
            record = normalise(raw, snapshot_id)
        except Exception as exc:  # noqa: BLE001 — one bad record must not stop the run
            report.skipped.append((str(raw.get("slug", "?")), str(exc)))
            continue
        if not record["criteria"]:
            report.skipped.append((record["scheme_id"], "no criteria could be proposed"))
            continue
        normalised.append(record)
        report.normalised += 1
        report.needs_review += 1

    if report.fetched == 0:
        raise IngestError(
            "the portal returned no schemes; refusing to write an empty snapshot"
        )
    if not normalised:
        # Everything fetched was too vague to propose criteria from. That is a
        # real outcome worth reporting, but there is nothing to write, and an
        # empty file would look like a corpus that had lost its schemes.
        logger.warning(
            "%s: fetched %s schemes but proposed criteria for none of them; "
            "no snapshot written",
            snapshot_id,
            report.fetched,
        )
        return report

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{snapshot_id}{'-' + state if state else ''}.pending.json"
    target.write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "level": "state" if state else "mixed",
                "state": _slugify(state) if state else None,
                "note": (
                    "AUTO-INGESTED FROM myscheme.gov.in — NOT ACTIVE. Every scheme "
                    "here carries needs_review: true and proposed criteria that a "
                    "human must verify against the scheme notification before this "
                    "file is renamed to .json and loaded."
                ),
                "schemes": normalised,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report.written_to = target
    logger.info(report.summary())
    return report


# ---- small helpers --------------------------------------------------------


def _slugify(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _truncate(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _flatten_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(_flatten_text(v) for v in value)
        elif isinstance(value, dict):
            parts.extend(_flatten_text(v) for v in value.values())
    return " ".join(p for p in parts if p)


_STOPWORDS = {
    "the", "and", "for", "of", "to", "a", "in", "is", "scheme", "yojana",
    "under", "with", "by", "on", "as", "an", "or", "be", "will", "are",
}


def _keywords(name: str, text: str) -> list[str]:
    tokens = re.findall(r"[a-z]{3,}", f"{name} {text}".lower())
    seen: dict[str, int] = {}
    for token in tokens:
        if token in _STOPWORDS:
            continue
        seen[token] = seen.get(token, 0) + 1
    return [w for w, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:12]]
