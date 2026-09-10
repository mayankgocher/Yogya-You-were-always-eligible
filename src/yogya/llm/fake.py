"""Deterministic stand-in for the LLM.

Used by the entire test suite and by the app whenever `YOGYA_LLM_OFFLINE=1`.

It is generic on purpose. Rather than hard-coding one canned answer per prompt,
it synthesises a *valid instance of whatever schema was asked for*, then applies
any scripted override registered for that schema. Adding a field to an agent's
output model therefore does not break every test — the fake keeps returning
something valid.

It also records every call, so tests can assert on how agents used the model
(and, just as importantly, assert that the rule engine did *not*).
"""

from __future__ import annotations

import enum
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Union, get_args, get_origin

from pydantic import BaseModel

ScriptedHandler = Callable[[str, str], dict[str, Any]]


@dataclass
class RecordedCall:
    kind: str  # "structured" | "text"
    schema: str | None
    system: str
    user: str


def _is_optional(annotation: Any) -> bool:
    return get_origin(annotation) in (Union, types.UnionType) and type(None) in get_args(
        annotation
    )


def _non_none_type(annotation: Any) -> Any:
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args[0] if args else str
    return annotation


def _synthesise(annotation: Any) -> Any:
    """Produce a plausible, deterministic value for one annotation."""
    annotation = _non_none_type(annotation)
    origin = get_origin(annotation)

    if origin in (list, set, tuple):
        return []
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return list(annotation)[0].value
        if issubclass(annotation, BaseModel):
            return _synthesise_model(annotation)
        if annotation is bool:
            return False
        if annotation is int:
            return 0
        if annotation is float:
            return 0.0
        if annotation is str:
            return ""
    return None


def _synthesise_model(model: type[BaseModel]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if not info.is_required():
            continue  # let the model's own default stand
        if _is_optional(info.annotation):
            data[name] = None
            continue
        data[name] = _synthesise(info.annotation)
    return data


class FakeLLM:
    """Drop-in replacement for LLMClient with no network access."""

    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []
        self._scripts: dict[str, ScriptedHandler] = {}
        self._text_script: Callable[[str, str], str] | None = None
        self._install_defaults()

    # ---- scripting --------------------------------------------------------
    def script(self, schema: type[BaseModel] | str, handler: ScriptedHandler) -> None:
        """Register an override for one output schema.

        The handler receives (system, user) and returns a dict merged over the
        synthesised default, so it only needs to specify the fields under test.
        """
        key = schema if isinstance(schema, str) else schema.__name__
        self._scripts[key] = handler

    def script_text(self, handler: Callable[[str, str], str]) -> None:
        self._text_script = handler

    # ---- LLMClient interface ---------------------------------------------
    def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> BaseModel:
        self.calls.append(
            RecordedCall("structured", schema.__name__, system, user)
        )
        data = _synthesise_model(schema)
        handler = self._scripts.get(schema.__name__)
        if handler is not None:
            data.update(handler(system, user))
        return schema.model_validate(data)

    def text(self, *, system: str, user: str, max_tokens: int | None = None) -> str:
        self.calls.append(RecordedCall("text", None, system, user))
        if self._text_script is not None:
            return self._text_script(system, user)
        return (
            "[offline mode] A drafted letter would appear here. "
            "The structured facts and citations above are unaffected."
        )

    # ---- introspection helpers used by tests ------------------------------
    def calls_for(self, schema: type[BaseModel] | str) -> list[RecordedCall]:
        key = schema if isinstance(schema, str) else schema.__name__
        return [c for c in self.calls if c.schema == key]

    def reset(self) -> None:
        self.calls.clear()

    # ---- default scripts --------------------------------------------------
    def _install_defaults(self) -> None:
        """Sensible offline behaviour for the agents' known schemas.

        Registered by *name* so this module does not import the agents and
        create a cycle.
        """

        def explanation(_system: str, user: str) -> dict[str, Any]:
            return {
                "explanation": (
                    "Offline explanation: the verdict above was produced by the "
                    "rule engine. Each criterion and its citation is listed "
                    "verbatim so the decision can be checked by hand."
                ),
                "plain_language_summary": "See the criteria list for the reasoning.",
            }

        def appeal_assessment(_system: str, user: str) -> dict[str, Any]:
            # Offline heuristic: a rejection citing a criterion the household
            # actually satisfies is treated as wrongful. The graph re-checks
            # this against the rule engine regardless, so a wrong guess here
            # cannot produce a wrongful-denial claim on its own.
            wrongful = "contradicted_by_rules" in user
            return {
                "assessed_as_wrongful": wrongful,
                "grounds": (
                    "The stated ground for rejection is contradicted by the "
                    "household's recorded particulars and the scheme's own "
                    "eligibility clause."
                    if wrongful
                    else "The rejection appears consistent with the scheme rules."
                ),
                "confidence": 0.6,
            }

        self.script("ExplanationOutput", explanation)
        self.script("AppealAssessmentOutput", appeal_assessment)
