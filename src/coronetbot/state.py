from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StateStore:
    path: Path
    cursors: dict[int, int] = field(default_factory=dict)
    thread_titles: dict[int, str] = field(default_factory=dict)
    _lock: asyncio.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    @classmethod
    def load(cls, path: Path) -> StateStore:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path=path)
        except (json.JSONDecodeError, OSError, TypeError):
            return cls(path=path)

        raw_cursors = data.get("channel_cursors", {}) if isinstance(data, dict) else {}
        cursors: dict[int, int] = {}
        if isinstance(raw_cursors, dict):
            for channel_id, message_id in raw_cursors.items():
                try:
                    cursors[int(channel_id)] = int(message_id)
                except (TypeError, ValueError):
                    continue
        raw_titles = data.get("thread_titles", {}) if isinstance(data, dict) else {}
        thread_titles: dict[int, str] = {}
        migrated_titles = False
        if isinstance(raw_titles, dict):
            for thread_id, title in raw_titles.items():
                try:
                    value = str(title)
                    if not value.startswith("sha256:"):
                        value = cls._title_fingerprint(value)
                        migrated_titles = True
                    thread_titles[int(thread_id)] = value
                except (TypeError, ValueError):
                    continue
        store = cls(path=path, cursors=cursors, thread_titles=thread_titles)
        if migrated_titles:
            store._write_locked()
        return store

    async def seen(self, channel_id: int, message_id: int) -> bool:
        async with self._lock:
            return message_id <= self.cursors.get(channel_id, 0)

    async def cursor(self, channel_id: int) -> int | None:
        async with self._lock:
            return self.cursors.get(channel_id)

    async def thread_title_seen(self, thread_id: int, title: str) -> bool:
        fingerprint = self._title_fingerprint(title)
        async with self._lock:
            return self.thread_titles.get(thread_id) == fingerprint

    async def mark_thread_title(self, thread_id: int, title: str) -> None:
        fingerprint = self._title_fingerprint(title)
        async with self._lock:
            if self.thread_titles.get(thread_id) == fingerprint:
                return
            self.thread_titles[thread_id] = fingerprint
            self._write_locked()

    @staticmethod
    def _title_fingerprint(title: str) -> str:
        return "sha256:" + hashlib.sha256(title.encode("utf-8")).hexdigest()

    async def mark_processed(self, channel_id: int, message_id: int) -> None:
        async with self._lock:
            current = self.cursors.get(channel_id, 0)
            if message_id <= current:
                return
            self.cursors[channel_id] = message_id
            self._write_locked()

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 1,
            "channel_cursors": {
                str(channel_id): str(message_id)
                for channel_id, message_id in sorted(self.cursors.items())
            },
            "thread_titles": {
                str(thread_id): title for thread_id, title in sorted(self.thread_titles.items())
            },
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)
