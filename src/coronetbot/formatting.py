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
        source = (
            f" in image `{violation.attachment_filename}`" if violation.attachment_filename else ""
        )
        entries.append(
            f"• **{violation.rule}:**{source} “{violation.quote}”\n  {violation.explanation}"
        )
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


def title_prefix_notice(title: str, recommended_prefix: str | None) -> str:
    if recommended_prefix is None:
        instruction = "Please begin it with `C: ` for a claim or `Q: ` for a question."
    else:
        instruction = f"Please edit it to begin with `{recommended_prefix}`."
    return (
        "Your forum post has been left in place, but its title is missing or uses the wrong "
        f"claim/question prefix.\n\n**Current title:**\n{quote(title)}\n\n{instruction} "
        "The prefix is a forum-organising convention, not a moderation violation."
    )


def edited_message_public_notice(author: str, approved_version: str) -> str:
    return (
        f"A post from **{author}** was deleted because its author edited it and the edited "
        "version did not comply with the community standards.\n\n"
        f"**Original post before the edit:**\n{quote(approved_version)}"
    )


def thread_deletion_participant_notice(messages: list[str]) -> str:
    label = "message" if len(messages) == 1 else "messages"
    verb = "has" if len(messages) == 1 else "have"
    preserved = "\n\n".join(
        f"**Your {label if len(messages) == 1 else f'message {index}'}:**\n{quote(message)}"
        for index, message in enumerate(messages, start=1)
    )
    return (
        "The original author of a thread you posted in edited its title into a version that "
        "does not comply with the community guidelines, regrettably triggering deletion "
        f"of the thread you posted in. Your {label} {verb} been preserved below.\n\n"
        f"{preserved}"
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
