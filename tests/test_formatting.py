from coronetbot.formatting import (
    chunks,
    edited_message_public_notice,
    quote,
    reasons,
    removal_notice,
    thread_deletion_participant_notice,
    title_prefix_notice,
    validation_notice,
)
from coronetbot.models import ModerationResult, Violation


def test_quote_preserves_text() -> None:
    assert quote("one\n\nthree") == "> one\n> \u200b\n> three"


def test_chunks_preserve_text_except_split_newlines() -> None:
    text = "a" * 25
    parts = list(chunks(text, limit=10))
    assert "".join(parts) == text
    assert all(len(part) <= 10 for part in parts)


def test_removal_notice_contains_required_sections() -> None:
    result = ModerationResult(
        allowed=False,
        violations=(Violation("Personal attack", "idiot", "Attacks the person."),),
        suggested_revision="I disagree with the decision.",
    )
    notice = removal_notice("general", "You idiot", result)
    combined = "\n".join(notice)
    assert "#general" in combined
    assert "Original draft" in combined
    assert "Reasons" in combined
    assert "Suggested revision" in combined
    assert "/validate" in combined
    assert "/rules" in combined
    assert "Return to the server" in combined
    assert "`/validate` here" not in combined
    assert "bot-moderation-audit" not in combined
    assert "we don't take appeals" in combined
    assert "feedback on the rules used for moderation" in combined
    assert "#discord-server-feedback" in combined
    assert notice[2] == "I disagree with the decision."
    assert notice[4] == "You idiot"
    assert not notice[2].startswith(">")
    assert not notice[4].startswith(">")


def test_image_reason_names_the_attachment() -> None:
    result = ModerationResult(
        allowed=False,
        violations=(
            Violation(
                "Personal attack",
                "dishonest idiot",
                "Attacks the person.",
                attachment_filename="caption.png",
            ),
        ),
        suggested_revision="Remove the personal attack.",
    )
    assert "in image `caption.png`" in reasons(result)


def test_title_prefix_notice_leaves_post_in_place() -> None:
    notice = title_prefix_notice("A question without a prefix", "Q: ")
    assert "left in place" in notice[0]
    assert "`Q: `" in notice[0]
    assert "not a moderation violation" in notice[0]
    assert notice[1] == "A question without a prefix"


def test_edit_public_notice_preserves_approved_version() -> None:
    notice = edited_message_public_notice("Alesya", "Previously approved\n\npost")
    assert "from **Alesya**" in notice[0]
    assert "edited version did not comply" in notice[0]
    assert notice[1] == "Previously approved\n\npost"


def test_thread_deletion_notice_preserves_participant_messages() -> None:
    notice = thread_deletion_participant_notice(["first", "second"])
    assert "original author" in notice[0]
    assert "triggering deletion" in notice[0]
    assert notice[1] == "**Your message 1 — copy the next message:**"
    assert notice[2] == "first"
    assert notice[3] == "**Your message 2 — copy the next message:**"
    assert notice[4] == "second"


def test_validation_notice_does_not_expose_internal_audit_channel() -> None:
    allowed = ModerationResult(allowed=True)
    assert "bot-moderation-audit" not in "\n".join(validation_notice("draft", allowed))
