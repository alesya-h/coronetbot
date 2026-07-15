from __future__ import annotations

from collections.abc import Iterable

from .models import ModerationResult

DISCORD_LIMIT = 2000


def quote(text: str) -> str:
    # Discord renders a bare `>` on an otherwise-empty line as a visible greater-than
    # sign rather than as an empty quoted line. A zero-width space keeps the blockquote
    # shape without adding visible junk between quoted paragraphs.
    return "\n".join(f"> {line}" if line else "> \u200b" for line in text.split("\n"))


def reasons(result: ModerationResult) -> str:
    entries = []
    for violation in result.violations:
        entries.append(f"• **{violation.rule}:** “{violation.quote}”\n  {violation.explanation}")
    return "\n\n".join(entries)


def removal_notice(channel: str, original: str, result: ModerationResult) -> str:
    assert not result.allowed and result.suggested_revision is not None
    return (
        f"Your message in **#{channel}** was removed from public view. A moderation "
        "audit containing the draft and decision is retained in **#bot-spam**.\n\n"
        f"**Original draft:**\n{quote(original)}\n\n"
        f"**Reasons:**\n{reasons(result)}\n\n"
        f"**Suggested revision:**\n{quote(result.suggested_revision)}\n\n"
        "You can copy and revise your original draft above. You may use `/validate` here "
        "to validate/refine your message before trying to send it again — that should help "
        "you avoid triggering slow-mode only for your message to get deleted seconds later. "
        "Use `/rules` to see the set of rules used for moderation."
    )


def validation_notice(original: str, result: ModerationResult) -> str:
    audit_disclosure = "\n\n_This validation and response were logged to #bot-spam._"
    if result.allowed:
        return "✅ This draft passes the current moderation rules." + audit_disclosure
    assert result.suggested_revision is not None
    return (
        f"❌ This draft would be removed.\n\n**Reasons:**\n{reasons(result)}\n\n"
        f"**Suggested revision:**\n{quote(result.suggested_revision)}\n\n"
        f"**Original draft:**\n{quote(original)}{audit_disclosure}"
    )


def chunks(text: str, limit: int = DISCORD_LIMIT) -> Iterable[str]:
    """Split text without dropping any characters, preferring newline boundaries."""
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        yield remaining[:split_at]
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    if remaining:
        yield remaining
