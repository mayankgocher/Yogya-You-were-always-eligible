"""Ingestion from myScheme.

Tested entirely against recorded payloads. The live portal is unreachable from
many networks (including this project's CI), and more importantly a test that
depends on a government website staying up is a test that will eventually fail
for reasons unrelated to the code.
"""

from __future__ import annotations

import json

import httpx
import pytest

from yogya.data.loader import SchemeCorpus
from yogya.ingest.myscheme import (
    IngestError,
    MySchemeClient,
    infer_benefit_kind,
    infer_documents,
    ingest,
    normalise,
    propose_criteria,
)
from yogya.models.household import DocumentKind
from yogya.models.scheme import BenefitKind, Operator


RECORD = {
    "slug": "test-pension-scheme",
    "schemeName": "Test Old Age Pension Scheme",
    "state": {"value": "Uttar Pradesh"},
    "nodalMinistryName": "Department of Social Welfare",
    "briefDescription": "Monthly pension for the elderly poor.",
    "benefits": ["A monthly pension of Rs 1000 is credited to the bank account."],
    "eligibilityDescription": (
        "The applicant should be aged above 60 years. The applicant must belong "
        "to a below poverty line family and should reside in a rural area. "
        "Annual family income should not exceed Rs 1.5 lakh. The applicant "
        "should not be an income tax payer."
    ),
    "documents_required": [
        "Aadhaar card",
        "Income certificate",
        "Bank account passbook",
        "Passport size photograph",
    ],
}


def fake_client(records: list[dict]) -> MySchemeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("from", 0))
        page = records[offset : offset + 100]
        return httpx.Response(
            200, json={"data": {"hits": {"items": page}}}
        )

    transport = httpx.MockTransport(handler)
    return MySchemeClient(client=httpx.Client(transport=transport), rate_limit_s=0)


# ── criterion proposal ─────────────────────────────────────────────────────


def test_income_ceiling_in_lakh_is_converted_to_rupees():
    proposed = propose_criteria("income should not exceed Rs 1.5 lakh", "src")
    income = next(c for c in proposed if c.field == "annual_income")
    assert income.value == 150_000
    assert income.op is Operator.LTE


def test_plain_rupee_ceiling_is_read():
    proposed = propose_criteria("family income not exceeding Rs 46,080", "src")
    assert next(c for c in proposed if c.field == "annual_income").value == 46080


def test_minimum_age_is_read():
    proposed = propose_criteria("applicant should be aged above 60 years", "src")
    assert next(c for c in proposed if c.field == "max_age").value == 60


def test_bpl_language_maps_to_the_ration_card_criterion():
    proposed = propose_criteria("must belong to a below poverty line family", "src")
    card = next(c for c in proposed if c.field == "ration_card")
    assert set(card.value) == {"aay", "phh"}
    assert card.remediable is True


def test_category_language_maps_to_social_category():
    proposed = propose_criteria("open to scheduled caste students only", "src")
    assert next(c for c in proposed if c.field == "social_category").value == "sc"


def test_rural_and_urban_are_not_both_asserted():
    both = propose_criteria("available in rural and urban areas", "src")
    assert not any(c.field == "residence" for c in both)


def test_every_proposed_criterion_is_marked_for_review():
    """The flag is what stops a regex silently deciding who eats."""
    for criterion in propose_criteria(RECORD["eligibilityDescription"], "src"):
        assert "NEEDS REVIEW" in criterion.citation


def test_nothing_is_proposed_from_prose_with_no_rules():
    assert propose_criteria("This scheme is very good for everyone.", "src") == []


# ── inference helpers ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a monthly pension", BenefitKind.PENSION),
        ("scholarship for students", BenefitKind.SCHOLARSHIP),
        ("insurance cover of Rs 2 lakh", BenefitKind.INSURANCE),
        ("construction of a house", BenefitKind.HOUSING),
        ("skill training programme", BenefitKind.SERVICE),
    ],
)
def test_benefit_kind_inference(text, expected):
    assert infer_benefit_kind(text) is expected


def test_document_inference_reads_a_requirements_list():
    documents = infer_documents(" ".join(RECORD["documents_required"]))
    assert DocumentKind.AADHAAR in documents
    assert DocumentKind.INCOME_CERTIFICATE in documents
    assert DocumentKind.BANK_PASSBOOK in documents


# ── normalisation ──────────────────────────────────────────────────────────


def test_normalise_produces_a_review_pending_record():
    record = normalise(RECORD, "snap-1")
    assert record["needs_review"] is True
    assert record["scheme_id"] == "test-pension-scheme"
    assert record["level"] == "state"
    assert record["state"] == "uttar-pradesh"
    assert record["source_snapshot"] == "snap-1"
    assert record["criteria"]


def test_a_scheme_without_a_state_is_central():
    record = normalise({**RECORD, "state": None}, "snap-1")
    assert record["level"] == "central"
    assert record["state"] is None


def test_normalised_records_keep_the_source_text_for_the_reviewer():
    """The reviewer must be able to compare proposed criteria against the
    original prose without opening a browser."""
    record = normalise(RECORD, "snap-1")
    assert "below poverty line" in record["raw_eligibility_text"]


def test_a_normalised_record_is_not_loadable_until_reviewed(tmp_path):
    record = normalise(RECORD, "snap-1")
    (tmp_path / "snap.pending.json").write_text(
        json.dumps({"snapshot_id": "snap-1", "schemes": [record]})
    )
    from yogya.data.loader import CorpusError

    with pytest.raises(CorpusError):
        SchemeCorpus.from_dir(tmp_path)


# ── the pipeline ───────────────────────────────────────────────────────────


def test_ingest_writes_a_pending_snapshot(tmp_path):
    report = ingest(
        out_dir=tmp_path, client=fake_client([RECORD]), snapshot_id="snap-1"
    )
    assert report.fetched == 1
    assert report.normalised == 1
    assert report.needs_review == 1
    assert report.written_to.name.endswith(".pending.json")

    payload = json.loads(report.written_to.read_text())
    assert all(s["needs_review"] for s in payload["schemes"])


def test_schemes_with_no_extractable_criteria_are_skipped(tmp_path):
    vague = {**RECORD, "eligibilityDescription": "Anyone may apply.", "slug": "vague"}
    report = ingest(
        out_dir=tmp_path, client=fake_client([vague]), snapshot_id="snap-2"
    )
    assert report.normalised == 0
    assert report.skipped[0][1] == "no criteria could be proposed"


def test_ingest_refuses_to_write_an_empty_snapshot(tmp_path):
    """Better no snapshot than a snapshot that silently drops every scheme."""
    with pytest.raises(IngestError, match="empty snapshot"):
        ingest(out_dir=tmp_path, client=fake_client([]), snapshot_id="snap-3")


def test_a_network_failure_leaves_the_corpus_untouched(tmp_path):
    def handler(request):
        raise httpx.ConnectError("blocked by network policy")

    client = MySchemeClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)), rate_limit_s=0
    )
    with pytest.raises(IngestError, match="has not been modified"):
        ingest(out_dir=tmp_path, client=client, snapshot_id="snap-4")
    assert list(tmp_path.iterdir()) == []


def test_paging_stops_at_the_limit(tmp_path):
    records = [{**RECORD, "slug": f"scheme-{i}"} for i in range(250)]
    report = ingest(
        out_dir=tmp_path, client=fake_client(records), limit=120, snapshot_id="s"
    )
    assert report.fetched == 120


def test_one_malformed_record_does_not_abort_the_run(tmp_path):
    records = [RECORD, {"slug": "broken", "state": 12345}, {**RECORD, "slug": "ok-2"}]
    report = ingest(out_dir=tmp_path, client=fake_client(records), snapshot_id="s")
    assert report.normalised >= 2
