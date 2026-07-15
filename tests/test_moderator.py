import json
from types import SimpleNamespace

import pytest

from coronetbot.moderator import Moderator, _ModerationOutput


def test_rules_are_inserted_without_interpreting_json_braces() -> None:
    moderator = Moderator(model="gpt-5.6-sol", rules="Do not call people idiots.")
    assert "Do not call people idiots." in moderator.system_prompt
    assert '"allowed": true | false' in moderator.system_prompt


@pytest.mark.asyncio
async def test_codex_backend_uses_structured_ephemeral_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Responses:
        def parse(self, **kwargs: object) -> object:
            captured.update(kwargs)
            parsed = _ModerationOutput(
                allowed=True,
                violations=[],
                suggested_revision=None,
            )
            return SimpleNamespace(output_parsed=parsed)

    client = SimpleNamespace(responses=Responses())
    moderator = Moderator(model="gpt-5.6-sol", thinking="high", rules="Be civil")

    async def new_client() -> object:
        return client

    monkeypatch.setattr(moderator, "_new_client", new_client)
    result = await moderator.moderate("A private draft")

    assert result.allowed
    assert json.loads(captured["input"]) == {"message": "A private draft"}
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["reasoning"] == {"effort": "high"}
    assert captured["store"] is False
    assert captured["text_format"] is _ModerationOutput
