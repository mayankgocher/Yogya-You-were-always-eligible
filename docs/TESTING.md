# Testing

```bash
python -m pytest                          # everything, ~11s
python -m pytest -m "not integration"     # units only
python -m pytest --cov=yogya --cov-report=term-missing
```

**236 tests · 96% coverage · no network, no API key, no database, no clock
dependence.** The suite passes in aeroplane mode. That is a design property, not
a coincidence: every dependency is injected, and `FakeLLM` plus
`SimulatedPortal` are complete substitutes rather than approximations.

## Layout

```
tests/
  conftest.py                    fixtures — inputs only, never expected outputs
  unit/
    test_rules_engine.py         36  the part that must never be wrong
    test_corpus.py               20  loading, versioning, and the shipped data
    test_agents.py               41  the seven agents
    test_infrastructure.py       34  retrieval, LLM layer, store, audit, portal
    test_llm_client.py           12  the real transport, with litellm stubbed
    test_ingest.py               24  myScheme, against recorded payloads
  integration/
    test_lifecycle.py             9  the subgraph alone
    test_graph.py                23  the compiled graph end to end
    test_api.py                  22  HTTP contract and the console
    test_demo.py                 15  the demo, pinned as a regression
```

## The invariants

Most tests assert what the system is **not allowed to do**. An agent that
produces a good answer most of the time but occasionally invents an income
figure is worse than no agent, because a caseworker will have stopped checking
by then.

| Invariant | Test |
|-----------|------|
| The model cannot make an ineligible family eligible | `test_matcher_never_asks_the_model_to_decide` |
| The model cannot manufacture a wrongful denial | `test_the_model_cannot_manufacture_a_wrongful_denial` |
| A dead provider does not change any verdict | `test_matcher_survives_a_broken_model` |
| Nothing reaches a portal without the submission gate | `test_nothing_is_submitted_without_passing_the_submission_gate` |
| A blocked application never reaches a portal | `test_a_blocked_application_never_reaches_the_portal` |
| A form with flagged fields cannot be submitted | `test_tracker_refuses_a_form_with_flagged_fields` |
| An unknown field is flagged, never guessed | `test_unknown_fields_are_flagged_not_invented` |
| Unknown income is not zero | `test_none_is_unknown_not_zero` |
| A definite no outranks a maybe | `test_hard_failure_beats_unknown` |
| The appeal cycle terminates and escalates | `test_appeals_are_bounded_and_end_in_escalation` |
| A rejection is judged against the rules as filed | `test_assessment_uses_the_rule_version_the_case_was_filed_under` |
| Verdicts are reproducible byte for byte | `test_evaluation_is_reproducible` |
| Insurance cover is not counted as cash | `test_insurance_cover_is_not_counted_as_cash` |
| The audit trail does not duplicate | `test_audit_trail_does_not_duplicate` |
| An empty resume payload is not consent | `test_an_empty_decision_is_refused` |
| The console loads no third-party assets | `test_the_console_loads_no_third_party_assets` |

## Corpus tests are data tests

`tests/unit/test_corpus.py` validates the shipped data, not just the loader:

- every criterion names a field the engine actually exposes
- every criterion carries a citation
- every required document has guidance on where to obtain it
- no duplicate rule versions
- the two pension versions really do differ in their income ceiling

These catch the failure mode that matters most in this project — a typo in a
JSON file silently disabling an eligibility condition — which no amount of code
testing would find.

## Fixtures build inputs, never expected outputs

`conftest.py` provides a `household`, a `sparse_household`, a corpus and test
doubles. It never asserts what the corpus *should* say about them. Tests that
check eligibility compute it from the same rules they are testing against, so
adding a scheme does not break unrelated tests, and changing a rule cannot pass
because a fixture was updated to match.

`test_the_scripted_profile_matches_what_the_rules_are_told` guards the demo's
own fixture: the `FakeLLM` must produce exactly the household the demo claims
to describe, or every downstream number is about a different family.

## `FakeLLM`

Generic rather than canned: it synthesises a *valid instance of whatever schema
was requested*, then applies any scripted override. Adding a field to an agent's
output model therefore does not break every test.

```python
llm.script(ProfileOutput, lambda system, user: {"head_name": "Sunita", ...})
llm.calls_for(ProfileOutput)     # assert on how the model was used
```

`test_fake_never_touches_the_network` removes `litellm` from `sys.modules` and
asserts the fake still works — otherwise an accidental import would break
offline CI for reasons nobody would connect to that file.

## `SimulatedPortal`

Deterministic. `script(app_id, *outcomes)` sets outcomes per appeal attempt, so
index 0 is the original decision and index 1 the decision after the first
appeal — which is exactly the behaviour the appeal path exists to exploit.
Unscripted applications fall back to a stable hash of their id, so even
unscripted runs are reproducible.

## Adding a test

- Testing a rule? `tests/unit/test_rules_engine.py`, and add the scheme case to
  `test_corpus.py` if it is about shipped data.
- Testing an agent? `tests/unit/test_agents.py` — and write the negative case
  first: what must this agent refuse to do?
- Testing routing? `tests/integration/test_lifecycle.py` for the subgraph,
  `test_graph.py` for the outer graph.
- Found a bug? Write the test that fails, fix it, and add a line to
  `HISTORY.md` if the cause was non-obvious.
