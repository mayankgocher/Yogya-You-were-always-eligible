"""The demo case: one family, screened end to end.

Everything here is deterministic and offline. No API key, no network, no
database — `python -m yogya.demo` runs the whole graph including both human
gates, the parallel eligibility sweep, a rejection, an appeal and an
escalation.

The family is fictional but the shape is not: a widowed agricultural labourer
in rural Uttar Pradesh with an Antyodaya card, an elderly father-in-law with a
disability, three children in school, and three documents to her name. On
paper she is one of the most heavily entitled categories of person in India.
In practice she receives almost nothing, because nobody has ever sat down and
enumerated it.
"""

from __future__ import annotations

import json

from yogya.agents.interrupts import SchemeSelection, SubmissionDecision
from yogya.audit import render, reset_entry_ids
from yogya.config import Settings
from yogya.data.loader import load_corpus
from yogya.graph.build import build_graph
from yogya.graph.runner import AutoCaseworker, run_config
from yogya.llm.fake import FakeLLM
from yogya.models.household import (
    DocumentKind,
    Gender,
    Household,
    Member,
    Occupation,
    RationCardType,
    Residence,
    SocialCategory,
)
from yogya.portal import SimulatedPortal, approved, rejected

HOUSEHOLD_ID = "hh-sunita-devi"

INTAKE_NOTES = """\
Sunita Devi, umar 44, Sitapur district, gaon mein rehti hai. Pati ka dehant
2019 mein ho gaya tha. Khet hai thoda sa, lagbhag aadha hectare, usi par kaam
karti hai aur dusron ke khet mein mazdoori bhi karti hai.

Ghar mein: sasur Ram Kumar, umar 68, ek pair se viklang hain (certificate nahi
hai). Beti Anjali 16 saal, class 10 mein padh rahi hai. Beta Rohit 9 saal,
primary school. Choti beti Meena 7 saal.

Antyodaya (peela) ration card hai. Bank account hai SBI mein. Gas connection
nahi hai, lakdi par khana banta hai. Kaccha ghar hai. Saal ka kamai lagbhag
48,000 rupaye. Income tax nahi bharti.

Aadhaar sabka hai, ration card hai, bank passbook hai, foto bhi hai, aur
bachchon ka janm praman patra hai. Income certificate, jaati praman patra,
viklangta praman patra, pati ka mrityu praman patra — inme se koi nahi hai.
"""


def demo_household() -> Household:
    """The profile a correct extraction of INTAKE_NOTES should produce."""
    return Household(
        household_id=HOUSEHOLD_ID,
        head_name="Sunita Devi",
        state="uttar_pradesh",
        district="Sitapur",
        residence=Residence.RURAL,
        annual_income=48000,
        social_category=SocialCategory.SC,
        ration_card=RationCardType.ANTYODAYA,
        land_holding_hectares=0.5,
        owns_pucca_house=False,
        has_bank_account=True,
        has_electricity=True,
        has_lpg_connection=False,
        is_income_tax_payer=False,
        members=[
            Member(
                member_id="m1",
                name="Sunita Devi",
                relation="self",
                age=44,
                gender=Gender.FEMALE,
                is_widow=True,
                occupation=Occupation.AGRI_LABOURER,
            ),
            Member(
                member_id="m2",
                name="Ram Kumar",
                relation="father-in-law",
                age=68,
                gender=Gender.MALE,
                has_disability=True,
                disability_percent=60,
                occupation=Occupation.UNEMPLOYED,
            ),
            Member(
                member_id="m3",
                name="Anjali",
                relation="daughter",
                age=16,
                gender=Gender.FEMALE,
                is_student=True,
                school_level="secondary",
            ),
            Member(
                member_id="m4",
                name="Rohit",
                relation="son",
                age=9,
                gender=Gender.MALE,
                is_student=True,
                school_level="primary",
            ),
            Member(
                member_id="m5",
                name="Meena",
                relation="daughter",
                age=7,
                gender=Gender.FEMALE,
                is_student=True,
                school_level="primary",
            ),
        ],
        documents=[
            DocumentKind.AADHAAR,
            DocumentKind.RATION_CARD,
            DocumentKind.BANK_PASSBOOK,
            DocumentKind.PHOTOGRAPH,
            DocumentKind.BIRTH_CERTIFICATE,
        ],
        intake_notes=INTAKE_NOTES,
    )


def demo_llm() -> FakeLLM:
    """A FakeLLM scripted to extract the demo household from the demo notes.

    This stands in for what a real model does with the Hinglish intake note.
    Scripting it keeps the demo reproducible; swapping in a real provider is a
    one-line config change and the rest of the run is unaffected, because the
    model's output is only ever a *profile* — never a verdict.
    """
    llm = FakeLLM()
    household = demo_household()

    def profile(_system: str, _user: str) -> dict:
        data = household.model_dump(mode="json")
        return {
            **{
                k: data[k]
                for k in (
                    "head_name",
                    "state",
                    "district",
                    "residence",
                    "annual_income",
                    "social_category",
                    "ration_card",
                    "land_holding_hectares",
                    "owns_pucca_house",
                    "has_bank_account",
                    "has_electricity",
                    "has_lpg_connection",
                    "is_income_tax_payer",
                    "documents",
                )
            },
            "members": [
                {k: v for k, v in m.items() if k not in {"member_id", "documents"}}
                for m in data["members"]
            ],
            "unknown_fields": [],
            "reasoning": "Extracted from the caseworker's Hinglish intake note.",
        }

    llm.script("ProfileOutput", profile)
    return llm


def app_id(scheme_id: str) -> str:
    """Application ids are deterministic, so the demo can script outcomes."""
    return f"app-{HOUSEHOLD_ID}-{scheme_id}"


def demo_portal() -> SimulatedPortal:
    """A portal scripted to exercise every branch of the lifecycle.

    Two departments say no. Both refusals are then re-checked against the
    scheme's own criteria by the rule engine — not by the model — and both come
    back contradicted, so both are appealed.

    * The old-age pension office relents on appeal. That is the recovered
      benefit: Rs 7,200 a year that the family would simply never have received,
      because nobody appeals a pension rejection on their own.
    * The LPG office refuses twice more. Appeals are exhausted and the case is
      escalated to the grievance authority rather than quietly dropped, which
      is the outcome most systems don't model at all.

    Everything else is approved. The mix is scripted rather than random so the
    demo tells the same story every time you run it in front of someone.
    """
    portal = SimulatedPortal(rejection_rate=0.0)
    portal.script(
        app_id("nsap-ignoaps"),
        rejected(
            "Rejected: applicant does not appear to satisfy the below-poverty-line "
            "condition.",
            code="BPL_NOT_ESTABLISHED",
        ),
        approved("REF-IGNOAPS-APPEAL"),
    )
    portal.script(
        app_id("pmuy"),
        rejected(
            "Rejected: connection already exists against this household.",
            code="DUPLICATE_CONNECTION",
        ),
        rejected(
            "Appeal dismissed: no fresh grounds furnished.", code="APPEAL_DISMISSED"
        ),
        rejected(
            "Second appeal dismissed.", code="APPEAL_DISMISSED"
        ),
    )
    return portal


def run_demo(verbose: bool = True) -> dict:
    """Run the full graph over the demo family and return the final state."""
    reset_entry_ids()
    settings = Settings(llm_offline=True, require_human_approval=True)
    corpus = load_corpus(settings.corpus_dir)
    portal = demo_portal()

    graph = build_graph(
        llm=demo_llm(),
        corpus=corpus,
        portal=portal,
        settings=settings,
    )

    caseworker = AutoCaseworker(
        on_selection=lambda req: SchemeSelection(
            # The family already receives its ration entitlement — the
            # caseworker marks it rather than filing it again.
            file_scheme_ids=[
                e["scheme_id"]
                for e in req["payload"]["eligible"]
                if e["scheme_id"] != "nfsa-phh"
            ],
            already_receiving_scheme_ids=["nfsa-phh"],
            note="Family confirmed they collect ration but nothing else.",
        ),
        on_submission=lambda req: SubmissionDecision(action="submit"),
    )

    config = run_config(thread_id="demo-sunita")
    final = caseworker.run(
        graph,
        {"household_id": HOUSEHOLD_ID, "intake_notes": INTAKE_NOTES},
        config,
    )

    if verbose:
        _print_report(final, caseworker)
    return final


def _print_report(final: dict, caseworker: AutoCaseworker) -> None:
    summary = final.get("summary", {})
    print("=" * 72)
    print("YOGYA — entitlement screening report")
    print("=" * 72)
    print(f"Household : {summary.get('head_name')} ({summary.get('household_id')})")
    print(f"Corpus    : {summary.get('corpus_snapshot')}")
    print()

    print("-- Eligibility sweep " + "-" * 51)
    for result in sorted(final.get("eligibility", []), key=lambda r: r.verdict.value):
        mark = {"eligible": "YES", "needs_info": " ? ", "not_eligible": " - "}[
            result.verdict.value
        ]
        print(f"  [{mark}] {result.scheme_name}")
        if result.verdict.value != "not_eligible":
            print(f"          {result.explanation}")
    print()

    print("-- Applications " + "-" * 56)
    for app in final.get("applications", []):
        line = f"  {app.status.value:<22} {app.scheme_name}"
        if app.appeal_attempts:
            line += f"  (appeals: {app.appeal_attempts})"
        print(line)
        if app.status.value == "blocked_on_documents":
            for gap in app.blocking_gaps:
                print(f"      missing {gap.document.value}: {gap.where_to_get[:70]}")
        if app.rejection:
            verdict = (
                "assessed WRONGFUL"
                if app.rejection.assessed_as_wrongful
                else "assessed defensible"
            )
            print(f"      rejected ({app.rejection.code}) — {verdict}")
    print()

    print("-- Metrics " + "-" * 61)
    for key in (
        "schemes_screened",
        "benefits_discovered",
        "already_receiving",
        "applications_filed",
        "approved",
        "rejected",
        "blocked_on_documents",
        "escalated",
        "appeals_filed",
        "wrongful_denials_recovered",
        "rupees_recovered_annual",
        "insurance_cover_secured_inr",
        "appeal_success_rate",
        "form_error_rate",
        "human_interrupts",
    ):
        print(f"  {key:<28} {summary.get(key)}")
    print()
    print(f"-- Human gates reached: {len(caseworker.seen)} " + "-" * 40)
    for request in caseworker.seen:
        print(f"  {request['kind']:<22} {request['title']}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Yogya demo case.")
    parser.add_argument(
        "--json", action="store_true", help="print the summary as JSON only"
    )
    parser.add_argument(
        "--audit", action="store_true", help="print the full audit trail"
    )
    args = parser.parse_args()

    final = run_demo(verbose=not args.json)
    if args.json:
        print(json.dumps(final.get("summary", {}), indent=2))
    if args.audit:
        print()
        print("-- Audit trail " + "-" * 57)
        print(render(final.get("audit", [])))


if __name__ == "__main__":
    main()
