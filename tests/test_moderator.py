import json
from types import SimpleNamespace

import pytest

from coronetbot.moderator import ModerationContext, Moderator, _ModerationOutput


def test_context_quotation_corpus_includes_proposed_title_only() -> None:
    context = ModerationContext(
        proposed_title="C: A disputed title",
        thread_root="context must not count as authored text",
    )
    corpus = context.quotation_corpus("Body text")
    assert corpus == "C: A disputed title\nBody text"
    assert "context must not count" not in corpus


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
    context = ModerationContext(channel_type="forum_reply", thread_title="Q: Test")
    result = await moderator.moderate("A private draft", context=context)

    assert result.allowed
    payload = json.loads(captured["input"])
    assert payload["proposed_message"] == "A private draft"
    assert payload["channel_type"] == "forum_reply"
    assert payload["thread_title"] == "Q: Test"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["reasoning"] == {"effort": "high"}
    assert captured["store"] is False
    assert captured["text_format"] is _ModerationOutput
