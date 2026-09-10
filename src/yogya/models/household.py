"""Household profile — the single input the whole system reasons about.

Field names deliberately mirror the vocabulary used in Indian scheme
notifications (annual income, land holding, ration card colour, social
category) so that a rule written from a gazette notification can be evaluated
against this object without a translation layer.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SocialCategory(str, Enum):
    GENERAL = "general"
    OBC = "obc"
    SC = "sc"
    ST = "st"
    EWS = "ews"
    UNKNOWN = "unknown"


class RationCardType(str, Enum):
    """Ration card colour is the single most load-bearing eligibility proxy."""

    ANTYODAYA = "aay"  # poorest of the poor
    PRIORITY = "phh"  # priority household
    APL = "apl"
    NONE = "none"
    UNKNOWN = "unknown"


class Gender(str, Enum):
    FEMALE = "female"
    MALE = "male"
    OTHER = "other"
    UNKNOWN = "unknown"


class Residence(str, Enum):
    RURAL = "rural"
    URBAN = "urban"
    UNKNOWN = "unknown"


class Occupation(str, Enum):
    FARMER = "farmer"
    AGRI_LABOURER = "agricultural_labourer"
    CONSTRUCTION = "construction_worker"
    DOMESTIC_WORKER = "domestic_worker"
    STREET_VENDOR = "street_vendor"
    WEAVER_ARTISAN = "weaver_artisan"
    FISHERFOLK = "fisherfolk"
    SALARIED = "salaried"
    SELF_EMPLOYED = "self_employed"
    UNEMPLOYED = "unemployed"
    OTHER = "other"
    UNKNOWN = "unknown"


class DocumentKind(str, Enum):
    AADHAAR = "aadhaar"
    RATION_CARD = "ration_card"
    INCOME_CERTIFICATE = "income_certificate"
    CASTE_CERTIFICATE = "caste_certificate"
    DOMICILE_CERTIFICATE = "domicile_certificate"
    BANK_PASSBOOK = "bank_passbook"
    LAND_RECORD = "land_record"
    DISABILITY_CERTIFICATE = "disability_certificate"
    BIRTH_CERTIFICATE = "birth_certificate"
    DEATH_CERTIFICATE = "death_certificate"
    MARRIAGE_CERTIFICATE = "marriage_certificate"
    SCHOOL_CERTIFICATE = "school_certificate"
    LABOUR_CARD = "labour_card"
    ELECTRICITY_BILL = "electricity_bill"
    PHOTOGRAPH = "photograph"


class Member(BaseModel):
    """One person in the household."""

    member_id: str
    name: str
    relation: str = "member"
    age: int | None = None
    gender: Gender = Gender.UNKNOWN
    is_student: bool = False
    school_level: str | None = None  # "primary" | "secondary" | "higher_secondary" | "college"
    has_disability: bool = False
    disability_percent: int | None = None
    is_pregnant: bool = False
    is_widow: bool = False
    occupation: Occupation = Occupation.UNKNOWN
    documents: list[DocumentKind] = Field(default_factory=list)

    @field_validator("age")
    @classmethod
    def _sane_age(cls, v: int | None) -> int | None:
        if v is not None and not 0 <= v <= 120:
            raise ValueError("age must be between 0 and 120")
        return v

    def has(self, doc: DocumentKind) -> bool:
        return doc in self.documents


class Household(BaseModel):
    """A single family, as known to the system at a point in time."""

    household_id: str
    head_name: str
    state: str  # e.g. "uttar_pradesh"
    district: str | None = None
    residence: Residence = Residence.UNKNOWN

    annual_income: int | None = None  # rupees per year, whole household
    social_category: SocialCategory = SocialCategory.UNKNOWN
    ration_card: RationCardType = RationCardType.UNKNOWN
    land_holding_hectares: float | None = None
    owns_pucca_house: bool | None = None
    has_bank_account: bool | None = None
    has_electricity: bool | None = None
    has_lpg_connection: bool | None = None
    is_income_tax_payer: bool | None = None

    members: list[Member] = Field(default_factory=list)
    documents: list[DocumentKind] = Field(default_factory=list)

    # Free-text intake note captured by the social worker, kept verbatim for audit.
    intake_notes: str = ""
    profiled_on: date | None = None
    # Fields the profiler could not determine with confidence.
    unknown_fields: list[str] = Field(default_factory=list)

    # ---- convenience accessors used by rule expressions -------------------
    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def has_disabled_member(self) -> bool:
        return any(m.has_disability for m in self.members)

    @property
    def has_widow(self) -> bool:
        return any(m.is_widow for m in self.members)

    @property
    def has_pregnant_member(self) -> bool:
        return any(m.is_pregnant for m in self.members)

    @property
    def has_student(self) -> bool:
        return any(m.is_student for m in self.members)

    @property
    def has_senior(self) -> bool:
        return any(m.age is not None and m.age >= 60 for m in self.members)

    @property
    def has_girl_child_under_10(self) -> bool:
        return any(
            m.gender == Gender.FEMALE and m.age is not None and m.age < 10
            for m in self.members
        )

    @property
    def occupations(self) -> set[str]:
        return {m.occupation.value for m in self.members}

    @property
    def available_documents(self) -> set[DocumentKind]:
        docs = set(self.documents)
        for m in self.members:
            docs |= set(m.documents)
        return docs

    def member(self, member_id: str) -> Member | None:
        return next((m for m in self.members if m.member_id == member_id), None)

    def rule_context(self) -> dict[str, Any]:
        """Flat namespace exposed to the deterministic rule engine."""
        return {
            "state": self.state,
            "district": self.district,
            "residence": self.residence.value,
            "annual_income": self.annual_income,
            "social_category": self.social_category.value,
            "ration_card": self.ration_card.value,
            "land_holding_hectares": self.land_holding_hectares,
            "owns_pucca_house": self.owns_pucca_house,
            "has_bank_account": self.has_bank_account,
            "has_electricity": self.has_electricity,
            "has_lpg_connection": self.has_lpg_connection,
            "is_income_tax_payer": self.is_income_tax_payer,
            "household_size": self.size,
            "has_disabled_member": self.has_disabled_member,
            "has_widow": self.has_widow,
            "has_pregnant_member": self.has_pregnant_member,
            "has_student": self.has_student,
            "has_senior": self.has_senior,
            "has_girl_child_under_10": self.has_girl_child_under_10,
            "occupations": self.occupations,
            "max_age": max((m.age for m in self.members if m.age is not None), default=None),
            "min_age": min((m.age for m in self.members if m.age is not None), default=None),
        }
