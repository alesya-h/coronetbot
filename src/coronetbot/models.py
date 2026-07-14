from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class InvalidModerationResponse(ValueError):
    """Raised when the LLM returns an unsafe or malformed decision."""


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    quote: str
    explanation: str


@dataclass(frozen=True, slots=True)
class ModerationResult:
    allowed: bool
    violations: tuple[Violation, ...] = ()
    suggested_revision: str | None = None

    @classmethod
    def from_json(cls, value: Any, original: str) -> ModerationResult:
        if not isinstance(value, dict) or type(value.get("allowed")) is not bool:
            raise InvalidModerationResponse("response must contain boolean 'allowed'")

        raw_violations = value.get("violations", [])
        if not isinstance(raw_violations, list):
            raise InvalidModerationResponse("'violations' must be an array")

        violations: list[Violation] = []
        for item in raw_violations:
            if not isinstance(item, dict):
                raise InvalidModerationResponse("each violation must be an object")
            fields = (item.get("rule"), item.get("quote"), item.get("explanation"))
            if not all(isinstance(field, str) and field.strip() for field in fields):
                raise InvalidModerationResponse("violation fields must be non-empty strings")
            rule, quote, explanation = (field.strip() for field in fields)
            if quote not in original:
                raise InvalidModerationResponse("violation quote is not in the original message")
            violations.append(Violation(rule=rule, quote=quote, explanation=explanation))

        revision = value.get("suggested_revision")
        if revision is not None and not isinstance(revision, str):
            raise InvalidModerationResponse("'suggested_revision' must be a string or null")
        if isinstance(revision, str):
            revision = revision.strip() or None

        if value["allowed"]:
            if violations:
                raise InvalidModerationResponse("allowed response contains violations")
            return cls(allowed=True)
        if not violations:
            raise InvalidModerationResponse("blocked response contains no violations")
        if revision is None:
            raise InvalidModerationResponse("blocked response contains no suggested revision")
        return cls(False, tuple(violations), revision)
