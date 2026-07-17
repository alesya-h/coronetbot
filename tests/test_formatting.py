from coronetbot.formatting import chunks, quote, reasons, removal_notice, validation_notice
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
    assert "#general" in notice
    assert "Original draft" in notice
    assert "Reasons" in notice
    assert "Suggested revision" in notice
    assert "/validate" in notice
    assert "/rules" in notice
    assert "Return to the server" in notice
    assert "`/validate` here" not in notice
    assert "bot-moderation-audit" not in notice


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


def test_validation_notice_does_not_expose_internal_audit_channel() -> None:
    allowed = ModerationResult(allowed=True)
    assert "bot-moderation-audit" not in validation_notice("draft", allowed)
