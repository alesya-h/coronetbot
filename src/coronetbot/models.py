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
    attachment_filename: str | None = None


@dataclass(frozen=True, slots=True)
class ModerationResult:
    allowed: bool
    violations: tuple[Violation, ...] = ()
    suggested_revision: str | None = None

    @classmethod
    def from_json(
        cls,
        value: Any,
        original: str,
        *,
        image_filenames: set[str] | None = None,
    ) -> ModerationResult:
        if not isinstance(value, dict) or type(value.get("allowed")) is not bool:
            raise InvalidModerationResponse("response must contain boolean 'allowed'")

        raw_violations = value.get("violations", [])
        if not isinstance(raw_violations, list):
            raise InvalidModerationResponse("'violations' must be an array")

        violations: list[Violation] = []
        image_filenames = image_filenames or set()
        for item in raw_violations:
            if not isinstance(item, dict):
                raise InvalidModerationResponse("each violation must be an object")
            fields = (item.get("rule"), item.get("quote"), item.get("explanation"))
            if not all(isinstance(field, str) and field.strip() for field in fields):
                raise InvalidModerationResponse("violation fields must be non-empty strings")
            rule, quote, explanation = (field.strip() for field in fields)
            attachment_filename = item.get("attachment_filename")
            if attachment_filename is not None and not isinstance(attachment_filename, str):
                raise InvalidModerationResponse("attachment filename must be a string or null")
            if isinstance(attachment_filename, str):
                attachment_filename = attachment_filename.strip()
                if attachment_filename not in image_filenames:
                    raise InvalidModerationResponse("violation cites an unknown image attachment")
            elif quote not in original:
                raise InvalidModerationResponse("violation quote is not in the original message")
            violations.append(
                Violation(
                    rule=rule,
                    quote=quote,
                    explanation=explanation,
                    attachment_filename=attachment_filename,
                )
            )

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
