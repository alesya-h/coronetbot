from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coronetbot.bot import CoronetClient, PreparedAttachments
from coronetbot.config import Config
from coronetbot.models import ModerationResult, Violation
from coronetbot.moderator import ModerationContext
from coronetbot.state import ApprovedMessage


def _client(tmp_path: Path) -> CoronetClient:
    config = Config(
        discord_token="token",
        mode="dev",
        guild_id=1526764377171296296,
        guild_name="test",
        llm_model="gpt-5.6-sol",
        llm_thinking="high",
        rules_path=Path("RULES.md"),
        max_concurrency=2,
        llm_timeout_seconds=120,
        llm_retries=2,
        state_path=tmp_path / "state.json",
        backfill_lookback_seconds=0,
        max_images_per_message=4,
        max_image_bytes=8_000_000,
    )
    return CoronetClient(config, "Be civil")


@pytest.mark.parametrize("is_latest", [True, False])
async def test_blocked_edit_only_posts_continuity_notice_when_not_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_latest: bool,
) -> None:
    client = _client(tmp_path)
    author = SimpleNamespace(
        id=20,
        display_name="Alesya",
        send=AsyncMock(),
    )
    channel = SimpleNamespace(id=10, name="general")
    message = SimpleNamespace(
        id=30,
        channel=channel,
        author=author,
        guild=SimpleNamespace(id=client.config.guild_id),
        content="bad edit",
        attachments=[],
        jump_url="https://discord.test/message",
        delete=AsyncMock(),
    )
    approved = ApprovedMessage(
        message_id=30,
        channel_id=10,
        author_id=20,
        content="approved original",
    )
    result = ModerationResult(
        allowed=False,
        violations=(Violation("U1", "bad", "Not allowed."),),
        suggested_revision="acceptable edit",
    )
    client.moderator = SimpleNamespace(moderate=AsyncMock(return_value=result))
    client.state = SimpleNamespace(
        remove_approved=AsyncMock(),
        mark_approved=AsyncMock(),
    )
    public_message = SimpleNamespace(delete=AsyncMock())
    send_public = AsyncMock(return_value=[public_message])

    async def context(_message: object) -> tuple[ModerationContext, PreparedAttachments]:
        return ModerationContext(), PreparedAttachments([], (), ())

    monkeypatch.setattr(client, "_moderation_context", context)
    monkeypatch.setattr(client, "_audit", AsyncMock(return_value=True))
    monkeypatch.setattr(client, "_message_is_latest", AsyncMock(return_value=is_latest))
    monkeypatch.setattr(client, "_send_public_notice", send_public)

    await client._process_message_edit(message, approved)

    message.delete.assert_awaited_once()
    client.state.remove_approved.assert_awaited_once_with(30)
    if is_latest:
        send_public.assert_not_awaited()
    else:
        send_public.assert_awaited_once()
        assert "approved original" in send_public.await_args.args[1]
        assert "Alesya" in send_public.await_args.args[1]


async def test_thread_deletion_notice_groups_and_preserves_participant_messages(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    owner = SimpleNamespace(id=20, bot=False)
    participant = SimpleNamespace(id=21, bot=False)
    bot = SimpleNamespace(id=22, bot=True)
    messages = [
        SimpleNamespace(
            id=101,
            author=owner,
            webhook_id=None,
            content="owner reply",
            attachments=[],
        ),
        SimpleNamespace(
            id=102,
            author=participant,
            webhook_id=None,
            content="first participant reply",
            attachments=[],
        ),
        SimpleNamespace(
            id=103,
            author=participant,
            webhook_id=None,
            content="second participant reply",
            attachments=[],
        ),
        SimpleNamespace(
            id=104,
            author=bot,
            webhook_id=None,
            content="bot reply",
            attachments=[],
        ),
    ]

    class Thread:
        id = 100
        owner_id = 20
        name = "C: Original title"

        def history(self, **_kwargs: object):
            async def iterator():
                for message in messages:
                    yield message

            return iterator()

    notices = await client._thread_participant_notices(Thread())

    assert notices is not None
    assert len(notices) == 1
    assert notices[0][0] is participant
    assert "first participant reply" in notices[0][1]
    assert "second participant reply" in notices[0][1]
    assert "owner reply" not in notices[0][1]
    assert "bot reply" not in notices[0][1]
