"""Shared fixtures.

Two principles hold across the whole suite:

* **No network, no keys, no clock dependence.** Every test runs against the
  checked-in corpus, the `FakeLLM` and the `SimulatedPortal`. `pytest` should
  pass on a laptop in aeroplane mode.
* **Fixtures build the *inputs*, never the expected outputs.** A test that
  asserts eligibility computes it from the corpus rules it is testing against,
  so the corpus and the assertions cannot silently drift apart.
"""

from __future__ import annotations

import pytest

from yogya.audit import reset_entry_ids
from yogya.config import Settings
from yogya.data.loader import SchemeCorpus, load_corpus
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
from yogya.portal import SimulatedPortal
from yogya.store import CaseStore


@pytest.fixture(autouse=True)
def _stable_audit_ids():
    """Audit ids restart at 1 in every test, so assertions can name them."""
    reset_entry_ids()
    yield
    reset_entry_ids()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        llm_offline=True,
        require_human_approval=True,
        max_appeal_attempts=2,
    )


@pytest.fixture
def corpus() -> SchemeCorpus:
    return load_corpus(Settings().corpus_dir)


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def portal() -> SimulatedPortal:
    # rejection_rate 0 keeps unscripted applications on the happy path, so a
    # test that cares about rejection must script it explicitly rather than
    # depending on a hash landing the right way.
    return SimulatedPortal(rejection_rate=0.0)


@pytest.fixture
def case_store() -> CaseStore:
    return CaseStore()


@pytest.fixture
def household() -> Household:
    """A well-specified rural UP household: nothing unknown, so verdicts are
    ELIGIBLE or NOT_ELIGIBLE and never NEEDS_INFO. Tests that want unknowns
    build them explicitly."""
    return Household(
        household_id="hh-test",
        head_name="Test Devi",
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
                name="Test Devi",
                age=44,
                gender=Gender.FEMALE,
                is_widow=True,
                occupation=Occupation.AGRI_LABOURER,
            ),
            Member(
                member_id="m2",
                name="Elder",
                age=68,
                gender=Gender.MALE,
                has_disability=True,
                disability_percent=60,
            ),
            Member(
                member_id="m3",
                name="Student",
                age=16,
                gender=Gender.FEMALE,
                is_student=True,
            ),
            Member(
                member_id="m4",
                name="Small",
                age=7,
                gender=Gender.FEMALE,
                is_student=True,
            ),
        ],
        documents=[
            DocumentKind.AADHAAR,
            DocumentKind.RATION_CARD,
            DocumentKind.BANK_PASSBOOK,
            DocumentKind.PHOTOGRAPH,
            DocumentKind.BIRTH_CERTIFICATE,
        ],
    )


@pytest.fixture
def sparse_household() -> Household:
    """A household as it looks after a hurried intake: most fields unknown."""
    return Household(
        household_id="hh-sparse",
        head_name="Unknown Devi",
        state="uttar_pradesh",
        residence=Residence.UNKNOWN,
        social_category=SocialCategory.UNKNOWN,
        ration_card=RationCardType.UNKNOWN,
        members=[Member(member_id="m1", name="Unknown Devi")],
        unknown_fields=["annual_income", "ration_card", "residence"],
    )
