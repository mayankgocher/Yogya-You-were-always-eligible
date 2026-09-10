"""Deterministic eligibility rule engine.

The central architectural commitment of Yogya: **the LLM never decides
eligibility**. It extracts a profile, it explains a decision, it drafts a form
or an appeal — but the verdict is produced here, by pure functions over
structured criteria, and is reproducible byte-for-byte given the same household
and the same rule version.

Three-valued logic is deliberate. A criterion is not just pass/fail; it can be
*unknown*, because a family who does not know their own annual income is the
normal case, not an edge case. An unknown criterion never silently fails —
it either warns (and the scheme surfaces as NEEDS_INFO) or blocks, per the
criterion's own `unknown_is_blocking` flag.
"""

from __future__ import annotations

from typing import Any, Iterable

from yogya.models.household import Household
from yogya.models.scheme import (
    Criterion,
    CriterionResult,
    EligibilityResult,
    EligibilityVerdict,
    Operator,
    Scheme,
)


class RuleEvaluationError(ValueError):
    """Raised when a criterion is malformed — a corpus bug, not a data bug."""


def _as_set(value: Any) -> set:
    if isinstance(value, (set, frozenset)):
        return set(value)
    if isinstance(value, (list, tuple)):
        return set(value)
    return {value}


def _compare(op: Operator, actual: Any, expected: Any) -> bool:
    """Apply one operator. `actual` is guaranteed non-None by the caller."""
    if op is Operator.LTE:
        return actual <= expected
    if op is Operator.LT:
        return actual < expected
    if op is Operator.GTE:
        return actual >= expected
    if op is Operator.GT:
        return actual > expected
    if op is Operator.EQ:
        return actual == expected
    if op is Operator.NEQ:
        return actual != expected
    if op is Operator.IN:
        return actual in _as_set(expected)
    if op is Operator.NOT_IN:
        return actual not in _as_set(expected)
    if op is Operator.IS_TRUE:
        return bool(actual) is True
    if op is Operator.IS_FALSE:
        return bool(actual) is False
    if op is Operator.INTERSECTS:
        return bool(_as_set(actual) & _as_set(expected))
    raise RuleEvaluationError(f"unsupported operator: {op}")


def evaluate_criterion(criterion: Criterion, context: dict[str, Any]) -> CriterionResult:
    """Evaluate one criterion against a household's rule context."""
    if criterion.field not in context:
        raise RuleEvaluationError(
            f"criterion {criterion.criterion_id!r} references unknown field "
            f"{criterion.field!r}"
        )

    actual = context[criterion.field]

    # "unknown" sentinels: None, and the explicit UNKNOWN enum values that the
    # profiler writes when it could not determine a field.
    is_unknown = actual is None or (
        isinstance(actual, str) and actual == "unknown"
    )

    if is_unknown:
        return CriterionResult(
            criterion_id=criterion.criterion_id,
            passed=not criterion.unknown_is_blocking,
            unknown=True,
            actual=None,
            expected=criterion.value,
            description=criterion.description,
            citation=criterion.citation,
            remediable=criterion.remediable,
        )

    try:
        passed = _compare(criterion.op, actual, criterion.value)
    except TypeError as exc:  # comparing str to int, etc. — corpus bug
        raise RuleEvaluationError(
            f"criterion {criterion.criterion_id!r}: cannot compare "
            f"{actual!r} to {criterion.value!r} with {criterion.op.value}"
        ) from exc

    return CriterionResult(
        criterion_id=criterion.criterion_id,
        passed=passed,
        unknown=False,
        actual=actual if not isinstance(actual, set) else sorted(actual),
        expected=criterion.value,
        description=criterion.description,
        citation=criterion.citation,
        remediable=criterion.remediable,
    )


def evaluate_scheme(household: Household, scheme: Scheme) -> EligibilityResult:
    """Produce a full, explainable verdict for one household/scheme pair.

    Verdict logic:
      * any hard (non-unknown) failure          -> NOT_ELIGIBLE
      * else any unknown criterion              -> NEEDS_INFO
      * else                                    -> ELIGIBLE

    A NOT_ELIGIBLE caused only by *remediable* failures is still NOT_ELIGIBLE —
    but the failures are marked remediable so the Document Gap agent can turn
    them into an action list rather than a dead end.
    """
    context = household.rule_context()
    results = [evaluate_criterion(c, context) for c in scheme.criteria]

    hard_failures = [r for r in results if not r.passed and not r.unknown]
    unknowns = [r for r in results if r.unknown]

    if hard_failures:
        verdict = EligibilityVerdict.NOT_ELIGIBLE
    elif unknowns:
        verdict = EligibilityVerdict.NEEDS_INFO
    else:
        verdict = EligibilityVerdict.ELIGIBLE

    return EligibilityResult(
        scheme_id=scheme.scheme_id,
        scheme_name=scheme.name,
        household_id=household.household_id,
        verdict=verdict,
        rule_version=scheme.rule_version,
        results=results,
        missing_fields=sorted(
            {
                next(c.field for c in scheme.criteria if c.criterion_id == r.criterion_id)
                for r in unknowns
            }
        ),
        estimated_annual_value_inr=scheme.benefit_value_annual_inr,
    )


def evaluate_many(
    household: Household, schemes: Iterable[Scheme]
) -> list[EligibilityResult]:
    """Convenience batch form, used by tests and the CLI demo.

    The graph does not call this — it fans out with Send so that each scheme is
    a separately observable, separately retryable node.
    """
    return [evaluate_scheme(household, s) for s in schemes]
