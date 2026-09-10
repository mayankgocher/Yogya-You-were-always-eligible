"""The rule engine is the part that must never be wrong.

Every other component can degrade — the model can be offline, the portal can be
down, retrieval can rank badly — and a family still gets a correct answer. If
this file goes red, someone is being told they do not qualify when they do.
"""

from __future__ import annotations

import pytest

from yogya.models.household import Household, Member, RationCardType
from yogya.models.scheme import (
    Criterion,
    EligibilityVerdict,
    Operator,
    Scheme,
)
from yogya.rules.engine import (
    RuleEvaluationError,
    evaluate_criterion,
    evaluate_many,
    evaluate_scheme,
)


def criterion(**overrides) -> Criterion:
    base = dict(
        criterion_id="c1",
        field="annual_income",
        op=Operator.LTE,
        value=100_000,
        description="income under a lakh",
        citation="Test Guidelines, cl. 1",
    )
    base.update(overrides)
    return Criterion(**base)


def scheme_with(*criteria: Criterion, **overrides) -> Scheme:
    base = dict(
        scheme_id="test-scheme",
        name="Test Scheme",
        benefit_summary="Something useful",
        criteria=list(criteria),
        rule_version="1.0.0",
    )
    base.update(overrides)
    return Scheme(**base)


# ── operators ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "op,value,actual,expected",
    [
        (Operator.LTE, 100, 100, True),
        (Operator.LTE, 100, 101, False),
        (Operator.LT, 100, 100, False),
        (Operator.GTE, 60, 60, True),
        (Operator.GT, 60, 60, False),
        (Operator.EQ, "sc", "sc", True),
        (Operator.NEQ, "sc", "obc", True),
        (Operator.IN, ["aay", "phh"], "aay", True),
        (Operator.IN, ["aay", "phh"], "apl", False),
        (Operator.NOT_IN, ["apl"], "aay", True),
        (Operator.IS_TRUE, None, True, True),
        (Operator.IS_TRUE, None, False, False),
        (Operator.IS_FALSE, None, False, True),
        (Operator.INTERSECTS, ["farmer"], {"farmer", "salaried"}, True),
        (Operator.INTERSECTS, ["farmer"], {"salaried"}, False),
    ],
)
def test_every_operator_behaves(op, value, actual, expected):
    result = evaluate_criterion(
        criterion(field="probe", op=op, value=value), {"probe": actual}
    )
    assert result.passed is expected
    assert result.unknown is False


def test_unknown_field_is_a_corpus_bug_not_a_silent_pass():
    """A criterion naming a field that does not exist must fail loudly.

    Silently treating it as unknown would let a typo in the corpus quietly
    disable an eligibility condition.
    """
    with pytest.raises(RuleEvaluationError, match="unknown field"):
        evaluate_criterion(criterion(field="no_such_field"), {"annual_income": 1})


def test_incomparable_types_raise_rather_than_guess():
    with pytest.raises(RuleEvaluationError, match="cannot compare"):
        evaluate_criterion(
            criterion(field="probe", op=Operator.LTE, value=100), {"probe": "a lot"}
        )


# ── three-valued logic ─────────────────────────────────────────────────────


def test_none_is_unknown_not_zero():
    """The single most dangerous default in a benefits system.

    Treating an unknown income as 0 would mark a family eligible for
    everything; treating it as infinity would mark them eligible for nothing.
    Both are wrong; unknown is a third answer.
    """
    result = evaluate_criterion(criterion(), {"annual_income": None})
    assert result.unknown is True
    assert result.actual is None


def test_unknown_enum_string_is_also_unknown():
    result = evaluate_criterion(
        criterion(field="ration_card", op=Operator.IN, value=["aay"]),
        {"ration_card": "unknown"},
    )
    assert result.unknown is True


def test_non_blocking_unknown_passes_but_stays_flagged():
    result = evaluate_criterion(
        criterion(unknown_is_blocking=False), {"annual_income": None}
    )
    assert result.passed is True
    assert result.unknown is True


def test_blocking_unknown_fails():
    result = evaluate_criterion(
        criterion(unknown_is_blocking=True), {"annual_income": None}
    )
    assert result.passed is False
    assert result.unknown is True


# ── verdicts ───────────────────────────────────────────────────────────────


def test_all_pass_is_eligible(household):
    scheme = scheme_with(criterion(value=100_000))
    assert evaluate_scheme(household, scheme).verdict is EligibilityVerdict.ELIGIBLE


def test_any_hard_failure_is_not_eligible(household):
    scheme = scheme_with(criterion(value=100_000), criterion(criterion_id="c2", value=10))
    result = evaluate_scheme(household, scheme)
    assert result.verdict is EligibilityVerdict.NOT_ELIGIBLE
    assert [r.criterion_id for r in result.failed] == ["c2"]


def test_unknown_without_failure_is_needs_info(sparse_household):
    scheme = scheme_with(criterion())
    result = evaluate_scheme(sparse_household, scheme)
    assert result.verdict is EligibilityVerdict.NEEDS_INFO
    assert result.missing_fields == ["annual_income"]


def test_hard_failure_beats_unknown(sparse_household):
    """A definite no outranks a maybe — we must not promise what is refused."""
    scheme = scheme_with(
        criterion(),  # unknown income
        criterion(criterion_id="c2", field="state", op=Operator.EQ, value="kerala"),
    )
    assert (
        evaluate_scheme(sparse_household, scheme).verdict
        is EligibilityVerdict.NOT_ELIGIBLE
    )


def test_remediable_failure_is_still_not_eligible_but_marked(household):
    """A missing bank account is a no *today*, and a to-do item, not a dead end."""
    household.has_bank_account = False
    scheme = scheme_with(
        criterion(
            criterion_id="bank",
            field="has_bank_account",
            op=Operator.IS_TRUE,
            value=None,
            remediable=True,
        )
    )
    result = evaluate_scheme(household, scheme)
    assert result.verdict is EligibilityVerdict.NOT_ELIGIBLE
    assert result.failed[0].remediable is True
    assert result.blocking_failures == []


# ── explainability ─────────────────────────────────────────────────────────


def test_every_result_carries_its_citation(household, corpus):
    """A verdict a caseworker cannot defend at a counter is worthless."""
    for scheme in corpus.current():
        result = evaluate_scheme(household, scheme)
        assert len(result.results) == len(scheme.criteria)
        for item in result.results:
            assert item.citation, f"{scheme.scheme_id}/{item.criterion_id} has no citation"
            assert item.description


def test_result_records_the_rule_version(household, corpus):
    scheme = corpus.get("up-old-age-pension")
    result = evaluate_scheme(household, scheme)
    assert result.rule_version == scheme.rule_version


# ── determinism ────────────────────────────────────────────────────────────


def test_evaluation_is_reproducible(household, corpus):
    """Same household, same rules, same answer — every time, no exceptions.

    This is the property that lets an appeal say 'your own criteria say yes'.
    """
    first = evaluate_many(household, corpus.current())
    second = evaluate_many(household, corpus.current())
    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]


def test_whole_corpus_evaluates_without_error(household, corpus):
    """Guards against a corpus edit that names a field the engine cannot see."""
    results = evaluate_many(household, corpus.current())
    assert len(results) == len(corpus.current())


def test_corpus_evaluates_for_a_sparse_profile(sparse_household, corpus):
    results = evaluate_many(sparse_household, corpus.current())
    assert all(
        r.verdict
        in {
            EligibilityVerdict.ELIGIBLE,
            EligibilityVerdict.NEEDS_INFO,
            EligibilityVerdict.NOT_ELIGIBLE,
        }
        for r in results
    )


# ── real corpus behaviour ──────────────────────────────────────────────────


def test_antyodaya_family_qualifies_for_food_entitlement(household, corpus):
    result = evaluate_scheme(household, corpus.get("nfsa-phh"))
    assert result.verdict is EligibilityVerdict.ELIGIBLE


def test_apl_family_does_not_qualify_for_food_entitlement(household, corpus):
    household.ration_card = RationCardType.APL
    result = evaluate_scheme(household, corpus.get("nfsa-phh"))
    assert result.verdict is EligibilityVerdict.NOT_ELIGIBLE
    assert any("Priority Household" in r.description for r in result.failed)


def test_urban_family_does_not_qualify_for_rural_housing(household, corpus):
    from yogya.models.household import Residence

    household.residence = Residence.URBAN
    result = evaluate_scheme(household, corpus.get("pmay-g"))
    assert result.verdict is EligibilityVerdict.NOT_ELIGIBLE


def test_household_without_senior_fails_old_age_pension(corpus):
    young = Household(
        household_id="hh-young",
        head_name="Young",
        state="uttar_pradesh",
        ration_card=RationCardType.ANTYODAYA,
        annual_income=40000,
        has_bank_account=True,
        members=[Member(member_id="m1", name="Young", age=30)],
    )
    result = evaluate_scheme(young, corpus.get("nsap-ignoaps"))
    assert result.verdict is EligibilityVerdict.NOT_ELIGIBLE


def test_income_ceiling_boundary_is_inclusive(household, corpus):
    """56,460 is the ceiling for the UP pension: at it, in; a rupee over, out."""
    scheme = corpus.get("up-old-age-pension")
    ceiling = next(c.value for c in scheme.criteria if c.field == "annual_income")

    household.annual_income = ceiling
    assert evaluate_scheme(household, scheme).verdict is EligibilityVerdict.ELIGIBLE

    household.annual_income = ceiling + 1
    assert evaluate_scheme(household, scheme).verdict is EligibilityVerdict.NOT_ELIGIBLE
