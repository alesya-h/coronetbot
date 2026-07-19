from __future__ import annotations

from collections.abc import Iterable, Sequence

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
        source = (
            f" in image `{violation.attachment_filename}`" if violation.attachment_filename else ""
        )
        entries.append(
            f"• **{violation.rule}:**{source} “{violation.quote}”\n  {violation.explanation}"
        )
    return "\n\n".join(entries)


def response_for_audit(parts: Sequence[str]) -> str:
    return "\n\n".join(
        f"**Message {index}:**\n{quote(part)}" for index, part in enumerate(parts, start=1)
    )


def removal_notice(channel: str, original: str, result: ModerationResult) -> tuple[str, ...]:
    assert not result.allowed and result.suggested_revision is not None
    return (
        f"Your message in **#{channel}** was removed.\n\n**Reasons:**\n{reasons(result)}",
        "**Suggested revision — copy the next message:**",
        result.suggested_revision,
        "**Original draft — copy the next message:**",
        original,
        "You can revise either version above. Return to the server and use `/validate` in "
        "the channel or thread where you intend to post; that gives the bot the relevant "
        "surrounding context and helps avoid triggering slow-mode before a message is "
        "removed. Use `/rules` in the server to see the moderation policy.",
    )


def title_prefix_notice(title: str, recommended_prefix: str | None) -> tuple[str, ...]:
    if recommended_prefix is None:
        instruction = "Please begin it with `C: ` for a claim or `Q: ` for a question."
    else:
        instruction = f"Please edit it to begin with `{recommended_prefix}`."
    return (
        "Your forum post has been left in place, but its title is missing or uses the wrong "
        f"claim/question prefix. {instruction} The prefix is a forum-organising convention, "
        "not a moderation violation.\n\n**Current title — copy the next message:**",
        title,
    )


def edited_message_public_notice(author: str, approved_version: str) -> tuple[str, ...]:
    return (
        f"A post from **{author}** was deleted because its author edited it and the edited "
        "version did not comply with the community standards.\n\n"
        "**Original post before the edit — copy the next message:**",
        approved_version,
    )


def thread_deletion_participant_notice(messages: list[str]) -> tuple[str, ...]:
    label = "message" if len(messages) == 1 else "messages"
    verb = "has" if len(messages) == 1 else "have"
    parts = [
        "The original author of a thread you posted in edited its title into a version that "
        "does not comply with the community guidelines, regrettably triggering deletion "
        f"of the thread you posted in. Your {label} {verb} been preserved below."
    ]
    for index, message in enumerate(messages, start=1):
        heading = "Your message" if len(messages) == 1 else f"Your message {index}"
        parts.extend((f"**{heading} — copy the next message:**", message))
    return tuple(parts)


def validation_notice(original: str, result: ModerationResult) -> tuple[str, ...]:
    if result.allowed:
        return ("✅ This draft passes the current moderation rules.",)
    assert result.suggested_revision is not None
    return (
        f"❌ This draft would be removed.\n\n**Reasons:**\n{reasons(result)}",
        "**Suggested revision — copy the next message:**",
        result.suggested_revision,
        "**Original draft — copy the next message:**",
        original,
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
