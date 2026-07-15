import asyncio
import json
from pathlib import Path

from coronetbot.state import StateStore


def test_state_store_marks_and_loads_cursors(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cursors.json"
    store = StateStore.load(path)

    asyncio.run(store.mark_processed(10, 100))
    asyncio.run(store.mark_processed(10, 90))
    asyncio.run(store.mark_processed(11, 50))

    assert json.loads(path.read_text())["channel_cursors"] == {"10": "100", "11": "50"}
    loaded = StateStore.load(path)
    assert asyncio.run(loaded.seen(10, 100))
    assert asyncio.run(loaded.seen(10, 99))
    assert not asyncio.run(loaded.seen(10, 101))


def test_state_store_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "cursors.json"
    path.write_text("not json")
    store = StateStore.load(path)

    assert not asyncio.run(store.seen(1, 1))
