from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APPROVED_MESSAGE_LIMIT = 5000


@dataclass(frozen=True, slots=True)
class ApprovedMessage:
    message_id: int
    channel_id: int
    author_id: int
    content: str
    attachment_ids: tuple[int, ...] = ()
    attachment_names: tuple[str, ...] = ()


@dataclass(slots=True)
class StateStore:
    path: Path
    cursors: dict[int, int] = field(default_factory=dict)
    thread_titles: dict[int, str] = field(default_factory=dict)
    approved_messages: dict[int, ApprovedMessage] = field(default_factory=dict)
    pending_messages: dict[int, set[int]] = field(default_factory=dict)
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
        raw_approved = data.get("approved_messages", {}) if isinstance(data, dict) else {}
        approved_messages: dict[int, ApprovedMessage] = {}
        if isinstance(raw_approved, dict):
            for message_id, item in raw_approved.items():
                if not isinstance(item, dict):
                    continue
                try:
                    parsed_id = int(message_id)
                    content = item["content"]
                    attachment_ids = item.get("attachment_ids", [])
                    attachment_names = item.get("attachment_names", [])
                    if not isinstance(content, str):
                        continue
                    if not isinstance(attachment_ids, list) or not isinstance(
                        attachment_names, list
                    ):
                        continue
                    approved_messages[parsed_id] = ApprovedMessage(
                        message_id=parsed_id,
                        channel_id=int(item["channel_id"]),
                        author_id=int(item["author_id"]),
                        content=content,
                        attachment_ids=tuple(int(value) for value in attachment_ids),
                        attachment_names=tuple(str(value) for value in attachment_names),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        raw_pending = data.get("pending_messages", {}) if isinstance(data, dict) else {}
        pending_messages: dict[int, set[int]] = {}
        if isinstance(raw_pending, dict):
            for channel_id, message_ids in raw_pending.items():
                if not isinstance(message_ids, list):
                    continue
                try:
                    pending_messages[int(channel_id)] = {int(value) for value in message_ids}
                except (TypeError, ValueError):
                    continue
        store = cls(
            path=path,
            cursors=cursors,
            thread_titles=thread_titles,
            approved_messages=approved_messages,
            pending_messages=pending_messages,
        )
        if migrated_titles:
            store._write_locked()
        return store

    async def seen(self, channel_id: int, message_id: int) -> bool:
        async with self._lock:
            if message_id in self.pending_messages.get(channel_id, set()):
                return False
            return message_id <= self.cursors.get(channel_id, 0)

    async def pending(self, channel_id: int) -> tuple[int, ...]:
        async with self._lock:
            return tuple(sorted(self.pending_messages.get(channel_id, set())))

    async def mark_pending(self, channel_id: int, message_id: int) -> None:
        async with self._lock:
            pending = self.pending_messages.setdefault(channel_id, set())
            if message_id not in pending:
                pending.add(message_id)
                self._write_locked()

    async def clear_pending(self, channel_id: int, message_id: int) -> None:
        async with self._lock:
            pending = self.pending_messages.get(channel_id)
            if pending is None or message_id not in pending:
                return
            pending.remove(message_id)
            if not pending:
                del self.pending_messages[channel_id]
            self._write_locked()

    async def cursor(self, channel_id: int) -> int | None:
        async with self._lock:
            return self.cursors.get(channel_id)

    async def thread_title_seen(self, thread_id: int, title: str) -> bool:
        fingerprint = self._title_fingerprint(title)
        async with self._lock:
            return self.thread_titles.get(thread_id) == fingerprint

    async def has_thread_title(self, thread_id: int) -> bool:
        async with self._lock:
            return thread_id in self.thread_titles

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

    async def approved_message(self, message_id: int) -> ApprovedMessage | None:
        async with self._lock:
            return self.approved_messages.get(message_id)

    async def mark_approved(self, message: ApprovedMessage) -> None:
        async with self._lock:
            self.approved_messages[message.message_id] = message
            if len(self.approved_messages) > APPROVED_MESSAGE_LIMIT:
                excess = len(self.approved_messages) - APPROVED_MESSAGE_LIMIT
                for message_id in sorted(self.approved_messages)[:excess]:
                    del self.approved_messages[message_id]
            self._write_locked()

    async def remove_approved(self, message_id: int) -> None:
        async with self._lock:
            if self.approved_messages.pop(message_id, None) is not None:
                self._write_locked()

    async def remove_channel_approved(self, channel_id: int) -> None:
        async with self._lock:
            message_ids = [
                message_id
                for message_id, message in self.approved_messages.items()
                if message.channel_id == channel_id
            ]
            if message_ids:
                for message_id in message_ids:
                    del self.approved_messages[message_id]
                self._write_locked()

    async def mark_processed(
        self,
        channel_id: int,
        message_id: int,
        *,
        approved: ApprovedMessage | None = None,
    ) -> None:
        async with self._lock:
            changed = False
            if approved is not None and self.approved_messages.get(message_id) != approved:
                self.approved_messages[message_id] = approved
                if len(self.approved_messages) > APPROVED_MESSAGE_LIMIT:
                    excess = len(self.approved_messages) - APPROVED_MESSAGE_LIMIT
                    for old_message_id in sorted(self.approved_messages)[:excess]:
                        del self.approved_messages[old_message_id]
                changed = True
            pending = self.pending_messages.get(channel_id)
            if pending is not None and message_id in pending:
                pending.remove(message_id)
                if not pending:
                    del self.pending_messages[channel_id]
                changed = True
            current = self.cursors.get(channel_id, 0)
            if message_id > current:
                self.cursors[channel_id] = message_id
                changed = True
            if changed:
                self._write_locked()

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 3,
            "channel_cursors": {
                str(channel_id): str(message_id)
                for channel_id, message_id in sorted(self.cursors.items())
            },
            "thread_titles": {
                str(thread_id): title for thread_id, title in sorted(self.thread_titles.items())
            },
            "pending_messages": {
                str(channel_id): [str(message_id) for message_id in sorted(message_ids)]
                for channel_id, message_ids in sorted(self.pending_messages.items())
            },
            "approved_messages": {
                str(message_id): {
                    "channel_id": str(message.channel_id),
                    "author_id": str(message.author_id),
                    "content": message.content,
                    "attachment_ids": [str(value) for value in message.attachment_ids],
                    "attachment_names": list(message.attachment_names),
                }
                for message_id, message in sorted(self.approved_messages.items())
            },
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        with suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)
