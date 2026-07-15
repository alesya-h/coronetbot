from __future__ import annotations

import asyncio
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
        return cls(path=path, cursors=cursors)

    async def seen(self, channel_id: int, message_id: int) -> bool:
        async with self._lock:
            return message_id <= self.cursors.get(channel_id, 0)

    async def cursor(self, channel_id: int) -> int | None:
        async with self._lock:
            return self.cursors.get(channel_id)

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
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)
