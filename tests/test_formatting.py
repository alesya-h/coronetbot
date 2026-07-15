from coronetbot.formatting import chunks, quote, removal_notice
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
