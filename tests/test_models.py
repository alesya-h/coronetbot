import pytest

from coronetbot.models import InvalidModerationResponse, ModerationResult


def test_allowed_result() -> None:
    result = ModerationResult.from_json(
        {"allowed": True, "violations": [], "suggested_revision": None}, "hello"
    )
    assert result.allowed


def test_allowed_result_can_include_non_blocking_advisory() -> None:
    result = ModerationResult.from_json(
        {
            "allowed": True,
            "violations": [],
            "suggested_revision": None,
            "advisory": "A pinpoint source link would help readers.",
        },
        "question",
    )
    assert result.allowed
    assert result.advisory == "A pinpoint source link would help readers."


def test_allowed_result_can_include_title_prefix_advisory() -> None:
    result = ModerationResult.from_json(
        {
            "allowed": True,
            "violations": [],
            "suggested_revision": None,
            "title_prefix_advisory": "Q: ",
        },
        "question",
    )
    assert result.allowed
    assert result.title_prefix_advisory == "Q: "


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


def test_image_violation_must_cite_a_supplied_image() -> None:
    payload = {
        "allowed": False,
        "violations": [
            {
                "rule": "Personal attack",
                "quote": "visible insulting caption",
                "explanation": "The image attacks a person.",
                "attachment_filename": "caption.png",
            }
        ],
        "suggested_revision": "Remove the insulting caption.",
    }
    result = ModerationResult.from_json(
        payload,
        "",
        image_filenames={"caption.png"},
    )
    assert result.violations[0].attachment_filename == "caption.png"

    with pytest.raises(InvalidModerationResponse, match="unknown image"):
        ModerationResult.from_json(payload, "", image_filenames={"other.png"})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"allowed": "yes", "violations": []},
        {
            "allowed": True,
            "violations": [],
            "suggested_revision": None,
            "title_prefix_advisory": "X: ",
        },
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
        {
            "allowed": False,
            "violations": [{"rule": "Attack", "quote": "hello", "explanation": "bad"}],
            "suggested_revision": "rewrite",
            "advisory": "optional note",
        },
        {
            "allowed": True,
            "violations": [],
            "suggested_revision": "unnecessary rewrite",
        },
    ],
)
def test_rejects_invalid_results(payload: object) -> None:
    with pytest.raises(InvalidModerationResponse):
        ModerationResult.from_json(payload, "hello")
