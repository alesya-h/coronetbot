from coronetbot.moderator import Moderator


def test_rules_are_inserted_without_interpreting_json_braces() -> None:
    moderator = Moderator(
        api_url="http://localhost/v1/chat/completions",
        api_key=None,
        model="test-model",
        rules="Do not call people idiots.",
    )
    assert "Do not call people idiots." in moderator.system_prompt
    assert '"allowed": true | false' in moderator.system_prompt
