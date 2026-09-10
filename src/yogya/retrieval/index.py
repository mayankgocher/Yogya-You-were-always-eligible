"""Retrieval over scheme rules.

Deliberately *not* a vector database. Three reasons:

1. The corpus is a few hundred documents, not a few million. BM25 over
   normalised text plus hard structural filters beats embeddings here, and it
   is inspectable — you can explain to a social worker why a scheme surfaced.
2. It must run on a free tier with no vector service and no embedding API bill.
3. Recall matters far more than precision at this stage: a missed scheme is a
   family that never learns it qualified, while a spurious candidate costs one
   cheap deterministic rule evaluation. So retrieval is tuned wide, and the
   rule engine — not the retriever — does the rejecting.

The retriever is a *candidate generator*, never a decision maker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from rank_bm25 import BM25Okapi

from yogya.data.loader import SchemeCorpus
from yogya.models.household import Household
from yogya.models.scheme import Scheme

_TOKEN = re.compile(r"[a-z0-9ऀ-ॿ]+")

# Household signals that should pull specific vocabulary into the query. This
# is what turns a structured profile into a retrieval query without an LLM.
_SIGNAL_TERMS: list[tuple[str, tuple[str, ...]]] = [
    ("has_senior", ("pension", "old age", "elderly", "senior", "vridha")),
    ("has_widow", ("widow", "vidhwa", "destitute women", "pension")),
    ("has_disabled_member", ("disability", "divyang", "viklang", "pension")),
    ("has_pregnant_member", ("maternity", "pregnant", "nutrition", "matru")),
    ("has_student", ("scholarship", "student", "education", "school", "college")),
    ("has_girl_child_under_10", ("girl child", "beti", "kanya", "savings")),
]

_OCCUPATION_TERMS: dict[str, tuple[str, ...]] = {
    "farmer": ("farmer", "kisan", "agriculture", "land", "crop"),
    "agricultural_labourer": ("farmer", "agriculture", "wage", "labour"),
    "construction_worker": ("construction", "labour", "shramik", "bocw", "mazdoor"),
    "street_vendor": ("street vendor", "loan", "rehri", "vending"),
    "weaver_artisan": ("artisan", "weaver", "craft", "vishwakarma", "toolkit"),
    "domestic_worker": ("labour", "welfare", "unorganised"),
    "fisherfolk": ("fisher", "matsya", "boat"),
    "unemployed": ("employment", "job card", "wage", "work"),
}


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True)
class Candidate:
    scheme: Scheme
    score: float
    reason: str


class SchemeRetriever:
    """BM25 + structural filtering over one snapshot of the corpus."""

    def __init__(self, corpus: SchemeCorpus, on: date | None = None) -> None:
        self.corpus = corpus
        self.as_of = on or date.today()
        self._schemes = corpus.current(self.as_of)
        self._corpus_tokens = [tokenize(s.searchable_text()) for s in self._schemes]
        self._bm25 = BM25Okapi(self._corpus_tokens)

    # ---- query construction ----------------------------------------------
    @staticmethod
    def build_query(household: Household) -> str:
        """Turn a structured household into a retrieval query.

        No LLM involved: the mapping from profile signal to scheme vocabulary
        is fixed, auditable, and free.
        """
        ctx = household.rule_context()
        terms: list[str] = [household.state, ctx["residence"]]

        for field, words in _SIGNAL_TERMS:
            if ctx.get(field):
                terms.extend(words)

        for occupation in household.occupations:
            terms.extend(_OCCUPATION_TERMS.get(occupation, ()))

        if household.ration_card.value in {"aay", "phh"}:
            terms += ["ration", "food", "bpl", "poor", "subsidy"]
        if household.owns_pucca_house is False:
            terms += ["housing", "house", "awas"]
        if household.has_lpg_connection is False:
            terms += ["lpg", "gas", "cooking fuel"]
        if household.has_bank_account is False:
            terms += ["bank account", "insurance"]
        if household.social_category.value in {"sc", "st", "obc", "ews"}:
            terms += ["scholarship", household.social_category.value]

        # Universal safety net terms so that a sparse profile still retrieves
        # the big, broadly applicable schemes.
        terms += ["insurance", "employment", "health", "pension"]
        if household.intake_notes:
            terms.append(household.intake_notes)
        return " ".join(t for t in terms if t)

    # ---- retrieval --------------------------------------------------------
    def retrieve(
        self,
        household: Household,
        top_k: int = 40,
        include_all_applicable: bool = True,
    ) -> list[Candidate]:
        """Return candidate schemes, highest scoring first.

        `include_all_applicable` is on by default and is the single most
        important knob in the system: when the corpus is small enough to
        evaluate exhaustively, we do, and BM25 only supplies the *ordering*.
        A family should never miss a benefit because a keyword did not match.
        """
        applicable = {
            s.scheme_id
            for s in self.corpus.for_household(household.state, self.as_of)
        }
        query = tokenize(self.build_query(household))
        scores = self._bm25.get_scores(query)

        ranked: list[Candidate] = []
        for scheme, score in zip(self._schemes, scores):
            if scheme.scheme_id not in applicable:
                continue
            ranked.append(
                Candidate(
                    scheme=scheme,
                    score=float(score),
                    reason="keyword match" if score > 0 else "exhaustive sweep",
                )
            )

        ranked.sort(key=lambda c: (-c.score, c.scheme.scheme_id))
        if include_all_applicable:
            return ranked
        return ranked[:top_k]

    def search(self, text: str, top_k: int = 10) -> list[Candidate]:
        """Free-text search, used by the UI's scheme explorer."""
        scores = self._bm25.get_scores(tokenize(text))
        ranked = [
            Candidate(scheme=s, score=float(sc), reason="text search")
            for s, sc in zip(self._schemes, scores)
            if sc > 0
        ]
        ranked.sort(key=lambda c: -c.score)
        return ranked[:top_k]
