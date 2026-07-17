import asyncio
import json
from pathlib import Path

from coronetbot.state import ApprovedMessage, StateStore


def test_state_store_marks_and_loads_cursors(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cursors.json"
    store = StateStore.load(path)

    asyncio.run(store.mark_processed(10, 100))
    asyncio.run(store.mark_processed(10, 90))
    asyncio.run(store.mark_processed(11, 50))
    asyncio.run(store.mark_thread_title(20, "A thread"))
    asyncio.run(
        store.mark_approved(
            ApprovedMessage(
                message_id=100,
                channel_id=10,
                author_id=30,
                content="Approved content",
                attachment_ids=(40,),
                attachment_names=("evidence.png",),
            )
        )
    )

    assert json.loads(path.read_text())["channel_cursors"] == {"10": "100", "11": "50"}
    loaded = StateStore.load(path)
    assert asyncio.run(loaded.seen(10, 100))
    assert asyncio.run(loaded.seen(10, 99))
    assert not asyncio.run(loaded.seen(10, 101))
    assert asyncio.run(loaded.has_thread_title(20))
    assert asyncio.run(loaded.thread_title_seen(20, "A thread"))
    assert not asyncio.run(loaded.thread_title_seen(20, "Renamed thread"))
    approved = asyncio.run(loaded.approved_message(100))
    assert approved is not None
    assert approved.content == "Approved content"
    assert approved.attachment_ids == (40,)
    assert approved.attachment_names == ("evidence.png",)
    asyncio.run(loaded.remove_channel_approved(10))
    assert asyncio.run(loaded.approved_message(100)) is None


def test_state_store_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "cursors.json"
    path.write_text("not json")
    store = StateStore.load(path)

    assert not asyncio.run(store.seen(1, 1))
