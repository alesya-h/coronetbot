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
        f"Your message in **#{channel}** was removed.\n\n"
        f"**Original draft:**\n{quote(original)}\n\n"
        f"**Reasons:**\n{reasons(result)}\n\n"
        f"**Suggested revision:**\n{quote(result.suggested_revision)}\n\n"
        "You can copy and revise your original draft above. Return to the server and use "
        "`/validate` in the channel or thread where you intend to post; that gives the bot "
        "the relevant surrounding context and helps avoid triggering slow-mode before a "
        "message is removed. Use `/rules` in the server to see the moderation policy."
    )


def validation_notice(original: str, result: ModerationResult) -> str:
    if result.allowed:
        return "✅ This draft passes the current moderation rules."
    assert result.suggested_revision is not None
    return (
        f"❌ This draft would be removed.\n\n**Reasons:**\n{reasons(result)}\n\n"
        f"**Suggested revision:**\n{quote(result.suggested_revision)}\n\n"
        f"**Original draft:**\n{quote(original)}"
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
