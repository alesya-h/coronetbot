import json
from types import SimpleNamespace

import pytest

from coronetbot.moderator import (
    ModerationContext,
    ModerationImage,
    ModerationServiceError,
    Moderator,
    _ModerationOutput,
)


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
    assert "no more than 100 characters" in moderator.system_prompt


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
                title_prefix_advisory=None,
                advisory=None,
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


@pytest.mark.asyncio
async def test_image_is_sent_as_ephemeral_multimodal_input(
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
                title_prefix_advisory=None,
                advisory=None,
            )
            return SimpleNamespace(output_parsed=parsed)

    moderator = Moderator(model="gpt-5.6-sol", rules="Be civil")

    async def new_client() -> object:
        return SimpleNamespace(responses=Responses())

    monkeypatch.setattr(moderator, "_new_client", new_client)
    result = await moderator.moderate(
        "See attached",
        images=(
            ModerationImage("proof.png", "image/png", b"png bytes"),
            ModerationImage(
                "thread root 10: root.png",
                "image/png",
                b"root bytes",
                authored=False,
            ),
        ),
    )

    assert result.allowed
    request_input = captured["input"]
    assert isinstance(request_input, list)
    content = request_input[0]["content"]
    assert json.loads(content[0]["text"])["proposed_message"] == "See attached"
    assert content[1]["text"].endswith("proof.png")
    assert content[2]["type"] == "input_image"
    assert content[2]["image_url"].startswith("data:image/png;base64,")
    assert content[3]["text"].startswith("Contextual evidence image")
    assert content[4]["type"] == "input_image"
    assert captured["store"] is False


@pytest.mark.asyncio
async def test_invalid_response_is_retried_with_a_fresh_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients_created = 0

    class Responses:
        def __init__(self, valid: bool) -> None:
            self.valid = valid

        def parse(self, **_kwargs: object) -> object:
            parsed = _ModerationOutput(
                allowed=self.valid,
                violations=[],
                suggested_revision=None,
                title_prefix_advisory=None,
                advisory=None,
            )
            return SimpleNamespace(output_parsed=parsed)

    moderator = Moderator(model="gpt-5.6-sol", rules="Be civil")

    async def new_client() -> object:
        nonlocal clients_created
        clients_created += 1
        return SimpleNamespace(responses=Responses(valid=clients_created == 2))

    monkeypatch.setattr(moderator, "_new_client", new_client)
    result = await moderator.moderate("A draft")

    assert result.allowed
    assert clients_created == 2


@pytest.mark.asyncio
async def test_exhausted_application_retry_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients_created = 0

    class Responses:
        def parse(self, **_kwargs: object) -> object:
            raise RuntimeError("provider output that must not escape")

    moderator = Moderator(model="gpt-5.6-sol", rules="Be civil")

    async def new_client() -> object:
        nonlocal clients_created
        clients_created += 1
        return SimpleNamespace(responses=Responses())

    monkeypatch.setattr(moderator, "_new_client", new_client)
    with pytest.raises(ModerationServiceError, match="Codex moderation request failed") as error:
        await moderator.moderate("A private draft")

    assert clients_created == 2
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_title_only_thread_is_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Responses:
        def parse(self, **_kwargs: object) -> object:
            parsed = _ModerationOutput(
                allowed=True,
                violations=[],
                suggested_revision=None,
                title_prefix_advisory=None,
                advisory=None,
            )
            return SimpleNamespace(output_parsed=parsed)

    moderator = Moderator(model="gpt-5.6-sol", rules="Be civil")

    async def new_client() -> object:
        return SimpleNamespace(responses=Responses())

    monkeypatch.setattr(moderator, "_new_client", new_client)
    result = await moderator.moderate(
        "",
        context=ModerationContext(proposed_title="A thread title"),
    )
    assert result.allowed
