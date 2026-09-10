# The seven agents

Every agent returns `(result, AuditEntry)` — or a tuple ending in one — so that
nothing the system concludes can exist without a line in the trail explaining
it.

The column that matters is the last one.

| # | Agent | File | What the model does | What happens if the model fails |
|---|-------|------|---------------------|-------------------------------|
| 1 | Household Profiler | `agents/profiler.py` | Extracts structured fields from Hinglish prose | Profile is empty; every field unknown; the human gate catches it |
| 2 | Entitlement Matcher | `agents/matcher.py` | Phrases a verdict it was handed | Template wording. **The verdict is unchanged.** |
| 3 | Document Gap | `agents/documents.py` | *Nothing.* Guidance is curated | Nothing to fail |
| 4 | Form Filler | `agents/forms.py` | Writes the free-text declaration | Template declaration. **Factual fields are unaffected.** |
| 5 | Application Tracker | `agents/tracker.py` | *Nothing* | Nothing to fail |
| 6 | Grievance Escalation | `agents/grievance.py` | Drafts the appeal letter | Template letter. **The wrongful/defensible judgement is the rule engine's.** |
| 7 | Social Worker Interrupt | `agents/interrupts.py` | *Nothing* | Nothing to fail |

Three of seven never call a model. Three more call it only for wording. Exactly
one — the profiler — depends on it for substance, and its output passes through
a human gate before anything is done with it.

---

## 1. Household Profiler

**Job:** caseworker's notes → `Household`.

The one place an LLM is genuinely the right tool: the input is messy human
narrative in mixed Hindi and English, the output is a fixed schema.

Also the highest-risk place, because an invented income figure silently
corrupts every downstream verdict. Two guards:

- The prompt instructs the model to answer *unknown* rather than guess, and to
  list every field it was unsure about in `unknown_fields`.
- `_detect_unknowns` catches the ones it forgot to declare. An undeclared
  unknown is how a null becomes a zero.

`CRITICAL_FIELDS` (state, residence, income, ration card, social category) are
the fields that change *which schemes a family sees*. If any is unknown, the
profile gate demands a human answer.

`known=` lets a returning family skip re-intake: last month's *confirmed*
values pre-fill, and only genuinely new extractions override them. Values the
profiler was previously unsure about are never carried forward — that would
launder a guess into a fact.

## 2. Entitlement Matcher

**Job:** household + scheme → `EligibilityResult`.

```python
result = evaluate_scheme(household, scheme)   # decided. no model.
result.explanation = self._explain(result, scheme)   # phrased. model.
```

The prompt tells the model it is given a decision that has already been made,
must not contradict it, must not re-decide, and must not mention a criterion it
was not given. If it does anyway, nothing happens: `_explain` only ever writes
to `.explanation`.

`_template_explanation` is the fallback and is used verbatim in offline mode.
It names the failed requirement and, separately, anything marked remediable —
"this could be fixed" is the difference between a dead end and an errand.

## 3. Document Gap

**Job:** which papers are missing, and *where to get them*.

Deliberately model-free. "Where do I get an income certificate in Uttar
Pradesh" has one correct answer, and a hallucinated office name costs a family
a day's wages and a bus fare. `DOCUMENT_GUIDANCE` is a curated table: issuing
authority, typical turnaround, indicative cost.

`errand_plan()` orders the errands blocking-first, then slowest-first — the
45-day disability certificate should be applied for on day one, not last.

A test asserts every document required by any scheme in the corpus has an entry
here. An unmapped document kind is a dead end on screen.

## 4. Form Filler

**Job:** fill the application; flag what it cannot fill.

Judged on **form error rate**, so the design goal is not "fill every field" but
"never fill a field wrongly". `FIELD_MAP` copies from structured profile data.
Anything it cannot supply becomes `<<NEEDS HUMAN>>`, and
`is_submittable()` is false while any flag remains — a mechanical guard behind
the human gate.

The model writes only the declaration, and is told to state no fact it was not
given.

## 5. Application Tracker

**Job:** submit, and read the status back.

The only component permitted to move an application to `SUBMITTED`, and it
refuses if the form has flagged fields or blocking document gaps. The human
gate is the policy check; this is the mechanical one. Both must pass.

`poll()` passes the appeal attempt number to the portal, because a poll after
an appeal is a different question from the original decision.

`is_overdue()` exists because being left undecided past a department's own
service standard is itself a ground for grievance — a family left waiting
indefinitely has been refused in practice without anyone writing it down.

## 6. Grievance Escalation

**Job:** was this refusal wrong? If so, appeal it. If appeals are exhausted,
escalate.

The critical ordering:

```python
scheme  = corpus.get(app.scheme_id, version=app.rule_version)  # rules as filed
recheck = evaluate_scheme(household, scheme)                   # engine decides
is_wrongful = recheck.verdict is ELIGIBLE                      # engine, not model
```

The model is consulted *after* the engine has decided, and only for wording.
`test_the_model_cannot_manufacture_a_wrongful_denial` scripts a model that
insists every refusal is an outrage and asserts the system disagrees.

Using `app.rule_version` rather than the current rules matters: re-checking a
2023 rejection against 2024's looser income ceiling would invent a wrongful
denial that never happened.

Escalation names the statutory grievance channel from the scheme record. Two
failed appeals produce an escalation, not a shrug.

## 7. Social Worker Interrupt

Not really an agent — a policy expressed as typed payloads.

| Gate | Decision model | Options |
|------|----------------|---------|
| `CONFIRM_PROFILE` | `ProfileDecision` | confirm · amend |
| `SELECT_SCHEMES` | `SchemeSelection` | file which · already receiving which |
| `APPROVE_SUBMISSION` | `SubmissionDecision` | submit · hold · skip |
| `APPROVE_APPEAL` | `AppealDecision` | file · edit and file · abandon |

`require_human_approval=False` downgrades the first two for tests. It does not
touch the submission gate. `SUBMISSION_GATE_IS_MANDATORY` is named loudly so
that anyone adding a bypass has to delete that line and explain it in a diff.
