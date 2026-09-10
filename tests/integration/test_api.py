"""The HTTP API and the console it serves.

The API's contract is narrow — start a session, answer the gate it is waiting
on — so these tests mostly check that the contract holds under the awkward
cases: resuming a session that is not waiting, asking for one that does not
exist, and a caseworker who answers a gate with a partial payload.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from yogya.api.app import build_app
from yogya.demo import HOUSEHOLD_ID, INTAKE_NOTES, app_id, demo_llm
from yogya.portal import SimulatedPortal, approved, rejected

pytestmark = pytest.mark.integration


@pytest.fixture
def portal_with_appeal() -> SimulatedPortal:
    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script(
        app_id("pmjay"),
        rejected("Not a below poverty line family.", code="BPL"),
        approved("REF-OK"),
    )
    return portal


@pytest.fixture
def client(settings, corpus, case_store, portal_with_appeal) -> TestClient:
    app = build_app(
        settings=settings,
        llm=demo_llm(),
        corpus=corpus,
        portal=portal_with_appeal,
        case_store=case_store,
    )
    return TestClient(app)


def drive(client: TestClient, thread: str, decisions: dict, limit: int = 40) -> dict:
    """Answer gates until the session completes."""
    state = client.get(f"/api/sessions/{thread}").json()
    for _ in range(limit):
        if state["status"] != "awaiting_human":
            return state
        kind = state["pending_interrupt"]["kind"]
        decision = decisions.get(kind)
        payload = decision(state["pending_interrupt"]) if callable(decision) else decision
        state = client.post(
            f"/api/sessions/{thread}/resume", json={"decision": payload}
        ).json()
    raise AssertionError("session never completed")


# ── metadata ───────────────────────────────────────────────────────────────


def test_health_reports_the_corpus_and_model(client, corpus):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["corpus_snapshot"] == corpus.snapshot_id
    assert body["schemes"] == len(corpus)
    assert body["llm_offline"] is True


def test_schemes_can_be_browsed(client, corpus):
    body = client.get("/api/schemes").json()
    assert len(body) == len(corpus)
    assert all(s["criteria_count"] > 0 for s in body)


def test_schemes_can_be_filtered_by_state(client):
    up = {s["scheme_id"] for s in client.get("/api/schemes?state=uttar_pradesh").json()}
    bihar = {s["scheme_id"] for s in client.get("/api/schemes?state=bihar").json()}
    assert "up-kanya-sumangala" in up
    assert "up-kanya-sumangala" not in bihar


def test_schemes_can_be_searched(client):
    body = client.get("/api/schemes?q=widow+pension").json()
    assert body
    assert any("idow" in s["name"] or "ahila" in s["name"] for s in body)


def test_scheme_detail_lists_every_rule_version(client):
    body = client.get("/api/schemes/up-old-age-pension").json()
    assert body["available_versions"] == ["1.0.0", "1.1.0"]
    assert body["criteria"]


def test_a_specific_rule_version_can_be_requested(client):
    body = client.get("/api/schemes/up-old-age-pension?version=1.0.0").json()
    assert body["rule_version"] == "1.0.0"


def test_unknown_scheme_is_404(client):
    assert client.get("/api/schemes/nope").status_code == 404


# ── sessions ───────────────────────────────────────────────────────────────


def test_starting_a_session_stops_at_the_first_gate(client):
    body = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    assert body["status"] == "awaiting_human"
    assert body["pending_interrupt"]["kind"] == "confirm_profile"
    assert body["household"]["head_name"] == "Sunita Devi"


def test_empty_notes_are_rejected(client):
    response = client.post(
        "/api/sessions", json={"household_id": "hh", "intake_notes": ""}
    )
    assert response.status_code == 422


def test_the_screening_runs_after_the_profile_gate(client):
    started = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    body = client.post(
        f"/api/sessions/{started['thread_id']}/resume",
        json={"decision": {"action": "confirm"}},
    ).json()
    assert body["pending_interrupt"]["kind"] == "select_schemes"
    assert sum(1 for e in body["eligibility"] if e["verdict"] == "eligible") > 5


def test_a_full_run_reaches_a_summary(client):
    started = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    final = drive(
        client,
        started["thread_id"],
        {
            "confirm_profile": {"action": "confirm"},
            "select_schemes": {"file_scheme_ids": ["pmjay", "mgnrega"]},
            "approve_submission": {"action": "submit"},
            "approve_appeal": {"action": "file"},
        },
    )
    assert final["status"] == "complete"
    assert final["summary"]["applications_filed"] == 2
    assert final["summary"]["wrongful_denials_recovered"] == 1
    assert final["audit"]


def test_the_appeal_gate_shows_the_draft_letter(client):
    started = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    thread = started["thread_id"]
    client.post(f"/api/sessions/{thread}/resume", json={"decision": {"action": "confirm"}})
    client.post(
        f"/api/sessions/{thread}/resume",
        json={"decision": {"file_scheme_ids": ["pmjay"]}},
    )
    state = client.post(
        f"/api/sessions/{thread}/resume", json={"decision": {"action": "submit"}}
    ).json()

    assert state["pending_interrupt"]["kind"] == "approve_appeal"
    payload = state["pending_interrupt"]["payload"]
    assert payload["appeal"]["letter_text"].strip()
    assert payload["application"]["rejection"]["assessed_as_wrongful"] is True


def test_an_edited_appeal_letter_is_kept(client):
    started = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    thread = started["thread_id"]
    final = drive(
        client,
        thread,
        {
            "confirm_profile": {"action": "confirm"},
            "select_schemes": {"file_scheme_ids": ["pmjay"]},
            "approve_submission": {"action": "submit"},
            "approve_appeal": {
                "action": "edit_and_file",
                "letter_text": "MY OWN WORDS",
            },
        },
    )
    assert final["applications"][0]["appeals"][0]["letter_text"] == "MY OWN WORDS"


def test_corrections_at_the_profile_gate_are_applied(client):
    started = client.post(
        "/api/sessions",
        json={"household_id": "hh-corrected", "intake_notes": INTAKE_NOTES},
    ).json()
    body = client.post(
        f"/api/sessions/{started['thread_id']}/resume",
        json={
            "decision": {
                "action": "amend",
                "corrections": {"district": "Hardoi", "annual_income": 61000},
            }
        },
    ).json()
    assert body["household"]["district"] == "Hardoi"
    assert body["household"]["annual_income"] == 61000


def test_resuming_a_finished_session_is_a_conflict(client):
    started = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    thread = started["thread_id"]
    drive(
        client,
        thread,
        {
            "confirm_profile": {"action": "confirm"},
            "select_schemes": {"file_scheme_ids": []},
        },
    )
    response = client.post(
        f"/api/sessions/{thread}/resume", json={"decision": {"action": "confirm"}}
    )
    assert response.status_code == 409


def test_unknown_session_is_404(client):
    assert client.get("/api/sessions/no-such-thread").status_code == 404


def test_a_partial_decision_payload_is_accepted(client):
    """The console posts only the fields it collected; the rest must default."""
    started = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    body = client.post(
        f"/api/sessions/{started['thread_id']}/resume",
        json={"decision": {"action": "confirm"}},
    ).json()
    assert body["pending_interrupt"]["kind"] == "select_schemes"


def test_an_empty_decision_is_refused(client):
    """An empty POST must not be read as consent.

    SubmissionDecision defaults to action="submit", so accepting `{}` would
    let a malformed request file an application with a government department —
    exactly the thing the interrupt gate exists to prevent.
    """
    started = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    response = client.post(
        f"/api/sessions/{started['thread_id']}/resume", json={"decision": {}}
    )
    assert response.status_code == 422
    assert "must not be empty" in response.json()["detail"]


# ── the long-lived case ────────────────────────────────────────────────────


def test_the_case_is_readable_after_the_session_ends(client):
    started = client.post(
        "/api/sessions",
        json={"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
    ).json()
    drive(
        client,
        started["thread_id"],
        {
            "confirm_profile": {"action": "confirm"},
            "select_schemes": {"file_scheme_ids": ["mgnrega"]},
            "approve_submission": {"action": "submit"},
        },
    )
    case = client.get(f"/api/households/{HOUSEHOLD_ID}/case").json()
    assert case["household"]["head_name"] == "Sunita Devi"
    assert case["applications"]
    assert case["audit"]


def test_an_unknown_household_has_no_case(client):
    assert client.get("/api/households/nobody/case").status_code == 404


# ── the console ────────────────────────────────────────────────────────────


def test_the_console_is_served_from_the_same_process(client):
    """One service, one free-tier deployment, no separate frontend host."""
    page = client.get("/")
    assert page.status_code == 200
    assert "Yogya" in page.text
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_the_console_loads_no_third_party_assets(client):
    """It must work on a slow connection and inside a locked-down network —
    and a CDN outage must not take the caseworker's screen down."""
    page = client.get("/").text
    assert "https://" not in page.replace("http://www.w3.org", "")
