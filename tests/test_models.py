import pytest

from coronetbot.models import InvalidModerationResponse, ModerationResult


def test_allowed_result() -> None:
    result = ModerationResult.from_json(
        {"allowed": True, "violations": [], "suggested_revision": None}, "hello"
    )
    assert result.allowed


def test_blocked_result() -> None:
    result = ModerationResult.from_json(
        {
            "allowed": False,
            "violations": [
                {"rule": "Personal attack", "quote": "idiot", "explanation": "Attacks a person."}
            ],
            "suggested_revision": "I disagree with that decision.",
        },
        "You are an idiot",
    )
    assert not result.allowed
    assert result.violations[0].quote == "idiot"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"allowed": "yes", "violations": []},
        {"allowed": False, "violations": [], "suggested_revision": "rewrite"},
        {
            "allowed": False,
            "violations": [{"rule": "Attack", "quote": "invented", "explanation": "bad"}],
            "suggested_revision": "rewrite",
        },
        {
            "allowed": True,
            "violations": [{"rule": "Attack", "quote": "hello", "explanation": "bad"}],
            "suggested_revision": None,
        },
    ],
)
def test_rejects_invalid_results(payload: object) -> None:
    with pytest.raises(InvalidModerationResponse):
        ModerationResult.from_json(payload, "hello")
