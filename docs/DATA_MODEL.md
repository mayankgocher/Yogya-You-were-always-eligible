# Data model

## Why the field names look like that

`Household` deliberately mirrors the vocabulary of Indian scheme notifications:
`annual_income`, `ration_card`, `land_holding_hectares`, `social_category`,
`owns_pucca_house`. A criterion written straight from a gazette notification can
then be evaluated against the object with no translation layer — and a
translation layer is exactly where an eligibility bug would hide.

`RationCardType` carries the values that matter: `aay` (Antyodaya, poorest of
the poor), `phh` (priority household), `apl`, `none`, `unknown`. Ration card
colour is the single most load-bearing eligibility proxy in Indian welfare,
which is why it is a first-class enum rather than a string.

Every enum has an `UNKNOWN` member. That is not laziness — see ADR-002.

## `rule_context()`

The flat namespace criteria are evaluated against. It is the contract between
the corpus and the engine:

```
state · district · residence · annual_income · social_category · ration_card
land_holding_hectares · owns_pucca_house · has_bank_account · has_electricity
has_lpg_connection · is_income_tax_payer · household_size
has_disabled_member · has_widow · has_pregnant_member · has_student
has_senior · has_girl_child_under_10 · occupations · max_age · min_age
```

Derived properties (`has_widow`, `max_age`, …) exist so a criterion can be a
simple triple. Adding a field here is how you make a new kind of criterion
expressible; a corpus test asserts every criterion names a key that exists.

## Eligibility

```
Scheme ──has──► Criterion   (field, op, value, description, citation,
                             remediable, unknown_is_blocking)
                    │ evaluate_criterion(criterion, context)
                    ▼
              CriterionResult  (passed, unknown, actual, expected,
                                description, citation, remediable)
                    │ collected
                    ▼
              EligibilityResult (verdict, rule_version, results[],
                                 missing_fields[], explanation)
```

`EligibilityResult` keeps the full per-criterion breakdown, not just the
verdict. That is what the UI renders as ticks and crosses, what the audit trail
cites, and what an appeal letter quotes.

Convenience views: `.failed`, `.blocking_failures` (failures that are *not*
remediable), `.citations`.

## Application lifecycle

```
DRAFT
  ├─► BLOCKED_ON_DOCUMENTS        blocking gap; never reaches a portal
  └─► AWAITING_APPROVAL           form filled, waiting on the human
        ├─► ABANDONED             human skipped it
        ├─► DRAFT                 human held it
        └─► SUBMITTED ─► WAITING
                          ├─► APPROVED      terminal
                          └─► REJECTED
                                ├─ defensible ──► stays REJECTED
                                └─ wrongful ──► APPEALED ─► re-poll
                                                   └─ attempts spent ─► ESCALATED
```

Terminal: `APPROVED`, `ESCALATED`, `ABANDONED`. Note `REJECTED` is *not*
terminal — a rejection is the beginning of the interesting part.

`Application.rule_version` pins the rules it was filed under, which is what the
grievance agent re-evaluates against.

`application_id` is `app-{household_id}-{scheme_id}` — deterministic, so
re-running a case updates the same application rather than duplicating it.

## Audit

```python
AuditEntry(entry_id, household_id, actor, action, detail,
           scheme_id, application_id, citations[], rule_version, at)
```

Append-only, built only via `yogya.audit.entry()`. `actor` is an agent name or
`"social_worker"` — human decisions are recorded in the same trail as machine
ones, which is what makes "who decided this" answerable.

## Metrics

`RunMetrics` counters are summed field-wise by `merge_metrics`, so parallel
branches can each report their own contribution.

`rupees_recovered_annual` counts cash only. Insurance cover is
`insurance_cover_secured_inr`. See ADR-009.

`form_error_rate` = flagged fields ÷ total fields. Low is good; zero achieved
by guessing would be much worse than a high number.
