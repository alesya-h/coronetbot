from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from . import __version__
from .config import Config
from .formatting import (
    chunks,
    edited_message_public_notice,
    quote,
    reasons,
    removal_notice,
    response_for_audit,
    thread_deletion_participant_notice,
    title_prefix_notice,
    validation_notice,
)
from .models import ModerationResult
from .moderator import ModerationContext, ModerationImage, ModerationServiceError, Moderator
from .state import ApprovedMessage, StateStore

LOG = logging.getLogger(__name__)
AUDIT_CHANNEL_NAME = "bot-moderation-audit"
IGNORED_CATEGORY_IDS = {1491596963647324180}  # Committee internal
IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


@dataclass(frozen=True, slots=True)
class PreparedAttachments:
    metadata: list[dict[str, Any]]
    images: tuple[ModerationImage, ...]
    unavailable_images: tuple[str, ...]


def detected_image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def attachment_looks_like_image(filename: str, content_type: str | None) -> bool:
    return bool(
        (content_type and content_type.casefold().startswith("image/"))
        or Path(filename).suffix.casefold() in IMAGE_EXTENSIONS
    )


class CoronetClient(discord.Client):
    def __init__(self, config: Config, rules: str) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.rules = rules
        self.tree = app_commands.CommandTree(self)
        self.audit_channel: discord.TextChannel | None = None
        self.state = StateStore.load(config.state_path)
        self._ready_once = False
        self._backfill_complete = asyncio.Event()
        self._channel_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.moderator = Moderator(
            model=config.llm_model,
            thinking=config.llm_thinking,
            rules=rules,
            max_concurrency=config.max_concurrency,
            timeout_seconds=config.llm_timeout_seconds,
            retries=config.llm_retries,
        )
        self._register_commands()

    async def setup_hook(self) -> None:
        await self.moderator.__aenter__()
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        LOG.info("Synced commands to %s (%s)", self.config.guild_name, self.config.guild_id)

    async def close(self) -> None:
        await self.moderator.__aexit__()
        await super().close()

    async def on_ready(self) -> None:
        assert self.user is not None
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            LOG.critical(
                "Bot is not installed in configured %s server %s",
                self.config.guild_name,
                self.config.guild_id,
            )
            await self.close()
            return

        matches = [channel for channel in guild.text_channels if channel.name == AUDIT_CHANNEL_NAME]
        if len(matches) != 1:
            LOG.critical(
                "Expected exactly one #%s in guild %s; found %s",
                AUDIT_CHANNEL_NAME,
                guild.id,
                len(matches),
            )
            await self.close()
            return
        self.audit_channel = matches[0]

        member = guild.me
        permissions = self.audit_channel.permissions_for(member) if member else None
        if permissions is None or not permissions.view_channel or not permissions.send_messages:
            LOG.critical(
                "Bot cannot view and send messages in audit channel %s",
                self.audit_channel.id,
            )
            await self.close()
            return

        LOG.info(
            "Logged in as %s (%s), mode=%s guild=%s audit_channel=%s model=%s thinking=%s",
            self.user,
            self.user.id,
            self.config.mode,
            guild.id,
            self.audit_channel.id,
            self.config.llm_model,
            self.config.llm_thinking,
        )
        if not self._ready_once:
            self._ready_once = True
            try:
                await self._backfill_missed_messages(guild)
            finally:
                self._backfill_complete.set()

    async def on_message(self, message: discord.Message) -> None:
        # Do not let a newer live message advance a channel cursor past older messages
        # while startup history is still being replayed.
        await self._backfill_complete.wait()
        await self._process_message(message, source="live")

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if "content" not in payload.data and "attachments" not in payload.data:
            return
        if payload.guild_id != self.config.guild_id:
            return
        await self._backfill_complete.wait()

        channel = self.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if self._channel_is_ignored(channel):
            return
        if self.audit_channel is not None and channel.id == self.audit_channel.id:
            return

        after = await self._fetch_message(channel, payload.message_id)
        if after is None or not self._message_in_scope(after):
            return

        approved = await self.state.approved_message(after.id)
        cached = payload.cached_message
        if approved is None and cached is not None and cached.author.id == after.author.id:
            approved = self._approved_from_message(cached)
        async with self._channel_locks[after.channel.id]:
            await self._process_message_edit(after, approved)

    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self._backfill_complete.wait()
        await self._process_thread_title(thread, source="thread_create")

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        if before.name == after.name:
            return
        await self._backfill_complete.wait()
        await self._process_thread_title(after, source="thread_rename")

    async def _process_thread_title(self, thread: discord.Thread, *, source: str) -> bool:
        if thread.guild.id != self.config.guild_id or self._channel_is_ignored(thread):
            return False
        async with self._channel_locks[thread.id]:
            if await self.state.thread_title_seen(thread.id, thread.name):
                return False
            is_title_edit = source == "thread_rename" or await self.state.has_thread_title(
                thread.id
            )

            starter = await self._fetch_message(thread, thread.id)
            if starter is None:
                try:
                    starter = await anext(thread.history(limit=1, oldest_first=True), None)
                except discord.HTTPException:
                    starter = None
            body = starter.content if starter is not None else ""
            author = starter.author if starter is not None else thread.owner
            parent = thread.parent
            is_forum = isinstance(parent, discord.ForumChannel)
            parent_name = getattr(parent, "name", "unknown")
            prepared = await self._prepare_attachments(
                starter.attachments if starter is not None else []
            )
            context = ModerationContext(
                channel_type="forum_post" if is_forum else "general_chat",
                channel_name=parent_name,
                channel_description=getattr(parent, "topic", None) or "",
                forum_rules_version="forum-rules.md (integrated policy)" if is_forum else None,
                thread_title=thread.name,
                thread_root=body or None,
                requested_action=self._requested_action(body),
                proposed_title=thread.name,
                attachments=prepared.metadata,
                cited_material_accessible=(
                    True if prepared.images else False if prepared.metadata else None
                ),
            )
            draft_text = f"Title: {thread.name}" + (f"\n\n{body}" if body else "")
            starter_attachments = starter.attachments if starter is not None else []
            original_draft = self._draft_with_attachments(draft_text, starter_attachments)
            received = (
                f"**Thread title received** ({source})\n"
                f"Thread: `{thread.id}` · Parent: `#{parent_name}`\n"
                f"Author: `{author}` (`{getattr(author, 'id', thread.owner_id)}`)\n\n"
                f"**Draft**\n{quote(draft_text)}\n\n"
                f"**Attachments**\n{self._attachments_audit_listing(starter_attachments)}"
            )
            if not await self._audit(received):
                if starter is not None:
                    await self.state.mark_pending(thread.id, starter.id)
                return False
            if prepared.metadata and not await self._audit(
                self._attachment_analysis_audit("thread", thread.id, prepared)
            ):
                if starter is not None:
                    await self.state.mark_pending(thread.id, starter.id)
                return False
            if prepared.unavailable_images:
                if await self._audit(
                    f"**Thread-title judgement** — thread `{thread.id}`\n"
                    "ERROR — one or more image attachments could not be analysed; failed open."
                ):
                    await self.state.mark_thread_title(thread.id, thread.name)
                return False

            try:
                result = await self.moderator.moderate(
                    body, context=context, images=prepared.images
                )
            except ModerationServiceError:
                LOG.exception("Thread-title moderation unavailable (thread=%s)", thread.id)
                await self._audit(
                    f"**Thread-title judgement** — thread `{thread.id}`\n"
                    "ERROR — classifier unavailable; failed open."
                )
                if starter is not None:
                    await self.state.mark_pending(thread.id, starter.id)
                return False

            if result.allowed:
                prefix_reminder = self._title_prefix_reminder(
                    thread.name,
                    is_forum=is_forum,
                    recommended_prefix=result.title_prefix_advisory,
                )
                bot_response = (
                    f"\n{response_for_audit(prefix_reminder)}" if prefix_reminder else " None."
                )
                if not await self._audit(
                    f"**Thread-title judgement** — thread `{thread.id}`\n"
                    f"ALLOWED — no rules violated.\nBot response: {bot_response}"
                ):
                    if starter is not None:
                        await self.state.mark_pending(thread.id, starter.id)
                    return False

                prefix_dm_status = "not needed"
                if prefix_reminder is not None:
                    prefix_dm_status = "not sent; author unavailable"
                    if author is not None:
                        prefix_dm_status = "sent"
                        try:
                            for response_part in prefix_reminder:
                                for part in chunks(response_part):
                                    await author.send(
                                        part, allowed_mentions=discord.AllowedMentions.none()
                                    )
                        except discord.HTTPException:
                            prefix_dm_status = "failed"
                    await self._audit(
                        f"**Thread-title prefix reminder** — thread `{thread.id}`\n"
                        f"DM delivery: **{prefix_dm_status}**\nThread: **left in place**"
                    )

                await self.state.mark_thread_title(thread.id, thread.name)
                if starter is not None:
                    await self.state.mark_processed(
                        thread.id,
                        starter.id,
                        approved=self._approved_from_message(starter),
                    )
                return False

            notice = removal_notice(parent_name, original_draft, result)
            participant_notices: list[tuple[discord.abc.User, tuple[str, ...]]] = []
            if is_title_edit:
                collected = await self._thread_participant_notices(thread)
                if collected is None:
                    await self._audit(
                        f"**Thread-title judgement** — thread `{thread.id}`\n"
                        "ERROR — blocked title edit, but participant messages could not be "
                        "preserved; failed open."
                    )
                    if starter is not None:
                        await self.state.mark_pending(thread.id, starter.id)
                    return False
                participant_notices = collected

            if not await self._audit(
                f"**Thread-title judgement** — thread `{thread.id}`\n"
                f"**BLOCKED**\n\n**Reasons**\n{reasons(result)}\n\n"
                f"**Bot response (removal DM)**\n{response_for_audit(notice)}"
            ):
                if starter is not None:
                    await self.state.mark_pending(thread.id, starter.id)
                return False
            for participant, participant_notice in participant_notices:
                if not await self._audit(
                    f"**Thread-deletion participant notification** — thread `{thread.id}`\n"
                    f"Recipient: `{participant}` (`{participant.id}`)\n\n"
                    f"**Bot response (DM)**\n{response_for_audit(participant_notice)}"
                ):
                    if starter is not None:
                        await self.state.mark_pending(thread.id, starter.id)
                    return False

            dm_status = "not sent; author unavailable"
            if author is not None:
                dm_status = "sent"
                try:
                    for response_part in notice:
                        for part in chunks(response_part):
                            await author.send(part, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    dm_status = "failed"

            participant_dm_sent = 0
            for participant, participant_notice in participant_notices:
                try:
                    for response_part in participant_notice:
                        for part in chunks(response_part):
                            await participant.send(
                                part, allowed_mentions=discord.AllowedMentions.none()
                            )
                    participant_dm_sent += 1
                except discord.HTTPException:
                    LOG.warning(
                        "Could not preserve messages for thread participant (thread=%s user=%s)",
                        thread.id,
                        participant.id,
                    )

            delete_status = "thread deleted"
            deleted = False
            try:
                await thread.delete(reason="CoronetBot moderation decision")
                deleted = True
                await self.state.remove_channel_approved(thread.id)
                LOG.info(
                    "Deleted rule-breaking thread title (guild=%s thread=%s owner=%s rules=%s)",
                    thread.guild.id,
                    thread.id,
                    thread.owner_id,
                    ", ".join(violation.rule for violation in result.violations),
                )
            except discord.HTTPException:
                delete_status = "thread deletion failed"
                LOG.exception("Could not delete rule-breaking thread %s", thread.id)

            actions_audited = await self._audit(
                f"**Thread-title actions** — thread `{thread.id}`\n"
                f"Owner DM delivery: **{dm_status}**\n"
                f"Participant preservation DMs: **{participant_dm_sent}/"
                f"{len(participant_notices)} sent**\n"
                f"Deletion: **{delete_status}**"
            )
            if deleted:
                if actions_audited:
                    await self.state.mark_thread_title(thread.id, thread.name)
                    if starter is not None and starter.id == thread.id:
                        await self.state.mark_processed(thread.id, starter.id)
            elif starter is not None:
                await self.state.mark_pending(thread.id, starter.id)
            return deleted

    async def _process_message(self, message: discord.Message, *, source: str) -> None:
        if not self._message_in_scope(message):
            return
        if isinstance(message.channel, discord.Thread) and message.id == message.channel.id:
            await self._process_thread_title(message.channel, source=source)
            return
        async with self._channel_locks[message.channel.id]:
            await self._process_message_serial(message, source=source)

    async def _process_message_serial(self, message: discord.Message, *, source: str) -> None:
        if await self.state.seen(message.channel.id, message.id):
            return

        if not await self._audit(self._received_audit(message, source=source)):
            LOG.error("Audit unavailable; message left intact (message=%s)", message.id)
            await self.state.mark_pending(message.channel.id, message.id)
            return

        context, prepared = await self._moderation_context(message)
        if prepared.metadata and not await self._audit(
            self._attachment_analysis_audit("message", message.id, prepared)
        ):
            LOG.error(
                "Attachment analysis could not be audited; message left intact (%s)", message.id
            )
            await self.state.mark_pending(message.channel.id, message.id)
            return
        if prepared.unavailable_images:
            if await self._audit(
                self._judgement_audit(
                    message,
                    "ERROR — one or more image attachments could not be analysed; failed open.",
                )
            ):
                await self.state.mark_processed(message.channel.id, message.id)
            return
        if not message.content.strip() and context.proposed_title is None and not prepared.images:
            judgement_audited = await self._audit(
                self._judgement_audit(
                    message,
                    "ALLOWED — no textual or supported image content; other attachments "
                    "were logged but not analysed.",
                )
            )
            if judgement_audited:
                await self.state.mark_processed(
                    message.channel.id,
                    message.id,
                    approved=self._approved_from_message(message),
                )
            else:
                await self.state.mark_pending(message.channel.id, message.id)
            return

        try:
            result = await self.moderator.moderate(
                message.content, context=context, images=prepared.images
            )
        except ModerationServiceError:
            LOG.exception(
                "Moderation unavailable; message left intact (guild=%s channel=%s message=%s)",
                message.guild.id,
                message.channel.id,
                message.id,
            )
            await self._audit(
                self._judgement_audit(message, "ERROR — classifier unavailable; failed open.")
            )
            await self.state.mark_pending(message.channel.id, message.id)
            return

        if result.allowed:
            judgement_audited = await self._audit(
                self._judgement_audit(message, "ALLOWED — no rules violated.")
            )
            if judgement_audited:
                await self.state.mark_processed(
                    message.channel.id,
                    message.id,
                    approved=self._approved_from_message(message),
                )
            else:
                await self.state.mark_pending(message.channel.id, message.id)
            return

        channel_name = getattr(message.channel, "name", "unknown")
        original_draft = self._original_draft(message, context)
        notice = removal_notice(channel_name, original_draft, result)
        decision_audit = self._blocked_audit(message, result, notice)
        if not await self._audit(decision_audit):
            LOG.error(
                "Could not audit blocked decision; message left intact (message=%s)",
                message.id,
            )
            await self.state.mark_pending(message.channel.id, message.id)
            return

        dm_status = "sent"
        try:
            for response_part in notice:
                for part in chunks(response_part):
                    await message.author.send(part, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            dm_status = "failed"
            LOG.warning(
                "Could not DM moderated user (guild=%s channel=%s message=%s user=%s)",
                message.guild.id,
                message.channel.id,
                message.id,
                message.author.id,
            )

        deletes_thread = context.proposed_title is not None and isinstance(
            message.channel, discord.Thread
        )
        delete_status = "thread deleted" if deletes_thread else "message deleted"
        deleted = False
        try:
            if deletes_thread:
                await message.channel.delete(reason="CoronetBot moderation decision")
            else:
                await message.delete()
            deleted = True
            LOG.info(
                "Deleted rule-breaking %s (guild=%s channel=%s message=%s user=%s rules=%s)",
                "thread" if deletes_thread else "message",
                message.guild.id,
                message.channel.id,
                message.id,
                message.author.id,
                ", ".join(v.rule for v in result.violations),
            )
        except discord.HTTPException:
            delete_status = (
                "thread deletion failed" if deletes_thread else "message deletion failed"
            )
            LOG.exception(
                "Could not delete rule-breaking %s (guild=%s channel=%s message=%s)",
                "thread" if deletes_thread else "message",
                message.guild.id,
                message.channel.id,
                message.id,
            )

        action_audited = await self._audit(
            f"**Moderation actions** — message `{message.id}`\n"
            f"DM delivery: **{dm_status}**\nDeletion: **{delete_status}**"
        )
        if deleted:
            if action_audited:
                await self.state.mark_processed(message.channel.id, message.id)
        else:
            await self.state.mark_pending(message.channel.id, message.id)

    async def _process_message_edit(
        self,
        message: discord.Message,
        approved: ApprovedMessage | None,
    ) -> None:
        current = self._approved_from_message(message)
        if approved == current:
            return
        if approved is not None and (
            approved.channel_id != message.channel.id or approved.author_id != message.author.id
        ):
            approved = None

        if not await self._audit(self._edit_received_audit(message, approved)):
            LOG.error("Audit unavailable; edited message left intact (message=%s)", message.id)
            return

        context, prepared = await self._moderation_context(message)
        if prepared.metadata and not await self._audit(
            self._attachment_analysis_audit("edited message", message.id, prepared)
        ):
            return
        if prepared.unavailable_images:
            await self._audit(
                self._judgement_audit(
                    message,
                    "ERROR — an edited image attachment could not be analysed; failed open.",
                )
            )
            return

        try:
            result = await self.moderator.moderate(
                message.content, context=context, images=prepared.images
            )
        except ModerationServiceError:
            LOG.exception("Edit moderation unavailable; message left intact (%s)", message.id)
            await self._audit(
                self._judgement_audit(message, "ERROR — classifier unavailable; failed open.")
            )
            return

        if result.allowed:
            if await self._audit(
                self._judgement_audit(message, "ALLOWED EDIT — no rules violated.")
            ):
                await self.state.mark_approved(current)
            return

        if approved is None:
            await self._audit(
                self._judgement_audit(
                    message,
                    "ERROR — edited version was blocked, but the approved pre-edit version "
                    "was unavailable; failed open.",
                )
            )
            return

        is_latest = await self._message_is_latest(message)
        if is_latest is None:
            await self._audit(
                self._judgement_audit(
                    message,
                    "ERROR — edited version was blocked, but message position could not be "
                    "determined; failed open.",
                )
            )
            return

        channel_name = getattr(message.channel, "name", "unknown")
        edited_draft = self._original_draft(message, context)
        approved_draft = self._approved_draft(approved, message.channel)
        removal = removal_notice(channel_name, edited_draft, result)
        public_notice: tuple[str, ...] | None = None
        if not is_latest:
            author_name = discord.utils.escape_markdown(message.author.display_name)
            public_notice = edited_message_public_notice(author_name, approved_draft)
        decision = (
            f"**Edit moderation judgement** — message `{message.id}`\n"
            f"**BLOCKED**\n\n**Reasons**\n{reasons(result)}\n\n"
            f"**Bot response (removal DM)**\n{response_for_audit(removal)}"
        )
        if public_notice is not None:
            decision += (
                "\n\n**Bot response (public continuity notice)**\n"
                f"{response_for_audit(public_notice)}"
            )
        if not await self._audit(decision):
            return

        public_messages: list[discord.Message] = []
        if public_notice is not None:
            sent = await self._send_public_notice(message.channel, public_notice)
            if sent is None:
                await self._audit(
                    f"**Edit moderation actions** — message `{message.id}`\n"
                    "Public continuity notice: **failed**\nDeletion: **not attempted; failed open**"
                )
                return
            public_messages = sent

        dm_status = "sent"
        try:
            for response_part in removal:
                for part in chunks(response_part):
                    await message.author.send(part, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            dm_status = "failed"

        delete_status = "message deleted"
        public_status = "sent" if public_notice else "not needed"
        try:
            # A body edit to a starter deletes only the starter message. Title edits are
            # handled separately by on_thread_update and delete the whole container.
            await message.delete()
            await self.state.remove_approved(message.id)
            LOG.info(
                "Deleted rule-breaking edited message (guild=%s channel=%s message=%s user=%s)",
                message.guild.id,
                message.channel.id,
                message.id,
                message.author.id,
            )
        except discord.HTTPException:
            delete_status = "message deletion failed; message retained"
            public_status = "rolled back" if public_notice else "not needed"
            for sent_message in public_messages:
                with suppress(discord.HTTPException):
                    await sent_message.delete()

        await self._audit(
            f"**Edit moderation actions** — message `{message.id}`\n"
            f"DM delivery: **{dm_status}**\n"
            f"Public continuity notice: **{public_status}**\n"
            f"Deletion: **{delete_status}**"
        )

    async def _message_is_latest(self, message: discord.Message) -> bool | None:
        channel = message.channel
        if not hasattr(channel, "history"):
            return None
        try:
            latest = await anext(channel.history(limit=1, oldest_first=False), None)
        except discord.HTTPException:
            return None
        if latest is None and isinstance(channel, discord.Thread) and message.id == channel.id:
            return True
        return latest is not None and latest.id == message.id

    async def _send_public_notice(
        self,
        channel: discord.abc.Messageable,
        response_parts: tuple[str, ...],
    ) -> list[discord.Message] | None:
        sent: list[discord.Message] = []
        try:
            for response_part in response_parts:
                for part in chunks(response_part):
                    sent.append(
                        await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
                    )
            return sent
        except discord.HTTPException:
            for message in sent:
                with suppress(discord.HTTPException):
                    await message.delete()
            return None

    def _message_in_scope(self, message: discord.Message) -> bool:
        return not (
            message.guild is None
            or message.guild.id != self.config.guild_id
            or message.author.bot
            or message.webhook_id is not None
            or self._channel_is_ignored(message.channel)
            or (self.audit_channel is not None and message.channel.id == self.audit_channel.id)
        )

    @staticmethod
    def _title_prefix_reminder(
        title: str,
        *,
        is_forum: bool,
        recommended_prefix: str | None,
    ) -> tuple[str, ...] | None:
        if not is_forum:
            return None
        has_conventional_prefix = re.match(r"^[CQ]: ", title) is not None
        if recommended_prefix is None and has_conventional_prefix:
            return None
        return title_prefix_notice(title, recommended_prefix)

    @staticmethod
    def _channel_category_id(channel: object) -> int | None:
        category_id = getattr(channel, "category_id", None)
        if category_id is not None:
            return category_id
        parent = getattr(channel, "parent", None)
        return getattr(parent, "category_id", None)

    @classmethod
    def _channel_is_ignored(cls, channel: object) -> bool:
        return cls._channel_category_id(channel) in IGNORED_CATEGORY_IDS

    async def _moderation_context(
        self, message: discord.Message
    ) -> tuple[ModerationContext, PreparedAttachments]:
        channel = message.channel
        channel_name = getattr(channel, "name", "unknown")
        channel_description = getattr(channel, "topic", None) or ""
        channel_type = "general_chat"
        thread_title: str | None = None
        thread_root: str | None = None
        proposed_title: str | None = None
        forum_rules_version: str | None = None

        if isinstance(channel, discord.Thread):
            thread_title = channel.name
            if message.id == channel.id:
                proposed_title = channel.name
                thread_root = message.content
            if isinstance(channel.parent, discord.ForumChannel):
                channel_description = channel.parent.topic or ""
                forum_rules_version = "forum-rules.md (integrated policy)"
                if message.id == channel.id:
                    channel_type = "forum_post"
                else:
                    channel_type = "forum_reply"
                    starter = await self._fetch_message(channel, channel.id)
                    thread_root = starter.content if starter is not None else None

        reply_target: str | None = None
        reference = message.reference
        if reference is not None and reference.message_id is not None:
            resolved = reference.resolved
            if isinstance(resolved, discord.Message):
                reply_target = resolved.content
            else:
                target = await self._fetch_message(channel, reference.message_id)
                reply_target = target.content if target is not None else None

        recent_messages: list[discord.Message] = []
        if hasattr(channel, "history"):
            try:
                async for previous in channel.history(
                    limit=15,
                    before=message,
                    oldest_first=False,
                ):
                    if not previous.author.bot and previous.webhook_id is None:
                        recent_messages.append(previous)
            except discord.HTTPException:
                LOG.warning("Could not fetch context for message %s", message.id)
        recent_messages.reverse()
        recent_context = [
            {
                "author_id": str(previous.author.id),
                "message": previous.content,
            }
            for previous in recent_messages
        ]
        recent_same_author = [
            previous.content
            for previous in recent_messages
            if previous.author.id == message.author.id
        ]
        prepared = await self._prepare_attachments(message.attachments)

        context = ModerationContext(
            channel_type=channel_type,
            channel_name=channel_name,
            channel_description=channel_description,
            forum_rules_version=forum_rules_version,
            thread_title=thread_title,
            thread_root=thread_root,
            requested_action=self._requested_action(thread_root),
            reply_target=reply_target,
            recent_context=recent_context,
            recent_same_author=recent_same_author,
            proposed_title=proposed_title,
            attachments=prepared.metadata,
            cited_material_accessible=(
                True if prepared.images else False if prepared.metadata else None
            ),
        )
        return context, prepared

    async def _prepare_attachments(
        self, attachments: Sequence[discord.Attachment]
    ) -> PreparedAttachments:
        metadata: list[dict[str, Any]] = []
        images: list[ModerationImage] = []
        unavailable: list[str] = []

        for attachment in attachments:
            item: dict[str, Any] = {
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "source_url": attachment.url,
                "extracted_text": None,
            }
            if not attachment_looks_like_image(attachment.filename, attachment.content_type):
                item["image_analysis"] = "not an image; content not analysed"
                metadata.append(item)
                continue
            if len(images) >= self.config.max_images_per_message:
                item["image_analysis"] = "not analysed; image-count limit exceeded"
                unavailable.append(attachment.filename)
                metadata.append(item)
                continue
            if attachment.size > self.config.max_image_bytes:
                item["image_analysis"] = "not analysed; image-size limit exceeded"
                unavailable.append(attachment.filename)
                metadata.append(item)
                continue

            try:
                data = await attachment.read(use_cached=True)
            except (discord.HTTPException, OSError):
                item["image_analysis"] = "not analysed; download failed"
                unavailable.append(attachment.filename)
                metadata.append(item)
                continue

            if len(data) > self.config.max_image_bytes:
                item["image_analysis"] = "not analysed; downloaded image exceeded size limit"
                unavailable.append(attachment.filename)
                metadata.append(item)
                continue
            media_type = detected_image_media_type(data)
            if media_type is None:
                item["image_analysis"] = "not analysed; unsupported or invalid image data"
                unavailable.append(attachment.filename)
                metadata.append(item)
                continue

            item["image_analysis"] = f"included in moderation request as {media_type}"
            metadata.append(item)
            images.append(
                ModerationImage(
                    filename=attachment.filename,
                    media_type=media_type,
                    data=data,
                )
            )

        return PreparedAttachments(metadata, tuple(images), tuple(unavailable))

    @staticmethod
    def _attachment_analysis_audit(
        source_type: str,
        source_id: int,
        prepared: PreparedAttachments,
    ) -> str:
        lines = [f"- `{item['filename']}`: {item['image_analysis']}" for item in prepared.metadata]
        return f"**Attachment analysis** — {source_type} `{source_id}`\n" + "\n".join(lines)

    @staticmethod
    def _requested_action(thread_root: str | None) -> str | None:
        if not thread_root:
            return None
        lines = thread_root.splitlines()
        for index, line in enumerate(lines):
            if "requested action" not in line.casefold():
                continue
            inline = line.split(":", 1)[1].strip() if ":" in line else ""
            following: list[str] = []
            for candidate in lines[index + 1 :]:
                if re.match(r"^\s*(?:#{1,6}\s+|\d+\.\s+)", candidate):
                    break
                if candidate.strip():
                    following.append(candidate.strip())
            value = " ".join(part for part in [inline, *following] if part)
            return value or None
        return None

    async def _thread_participant_notices(
        self,
        thread: discord.Thread,
    ) -> list[tuple[discord.abc.User, tuple[str, ...]]] | None:
        by_author: dict[int, tuple[discord.abc.User, list[str]]] = {}
        try:
            async for message in thread.history(limit=None, oldest_first=True):
                if (
                    message.id == thread.id
                    or message.author.bot
                    or message.webhook_id is not None
                    or message.author.id == thread.owner_id
                ):
                    continue
                entry = by_author.setdefault(message.author.id, (message.author, []))
                entry[1].append(self._draft_with_attachments(message.content, message.attachments))
        except (discord.Forbidden, discord.HTTPException):
            LOG.exception("Could not preserve participant messages for thread %s", thread.id)
            return None

        return [
            (author, thread_deletion_participant_notice(messages))
            for author, messages in by_author.values()
        ]

    @staticmethod
    async def _fetch_message(
        channel: discord.abc.Messageable,
        message_id: int,
    ) -> discord.Message | None:
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    @staticmethod
    def _approved_from_message(message: discord.Message) -> ApprovedMessage:
        return ApprovedMessage(
            message_id=message.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            content=message.content,
            attachment_ids=tuple(attachment.id for attachment in message.attachments),
            attachment_names=tuple(attachment.filename for attachment in message.attachments),
        )

    @classmethod
    def _approved_draft(
        cls,
        approved: ApprovedMessage,
        channel: discord.abc.Messageable,
    ) -> str:
        text = approved.content
        if isinstance(channel, discord.Thread) and approved.message_id == channel.id:
            text = f"Title: {channel.name}" + (f"\n\n{text}" if text else "")
        if approved.attachment_names:
            names = "\n".join(f"- {name}" for name in approved.attachment_names)
            prefix = f"{text}\n\n" if text else ""
            text = f"{prefix}Attachments:\n{names}"
        return text

    def _edit_received_audit(
        self,
        message: discord.Message,
        approved: ApprovedMessage | None,
    ) -> str:
        approved_draft = (
            self._approved_draft(approved, message.channel)
            if approved is not None
            else "_(approved pre-edit version unavailable)_"
        )
        current = self._original_draft(
            message,
            ModerationContext(
                proposed_title=(
                    message.channel.name
                    if isinstance(message.channel, discord.Thread)
                    and message.id == message.channel.id
                    else None
                )
            ),
        )
        return (
            f"**Message edit received**\n"
            f"Message: `{message.id}` · [jump]({message.jump_url})\n"
            f"Author: `{message.author}` (`{message.author.id}`)\n"
            f"Channel: `{message.channel.id}`\n\n"
            f"**Approved pre-edit version**\n{quote(approved_draft)}\n\n"
            f"**Edited version**\n{quote(current)}\n\n"
            f"**Current attachments**\n"
            f"{self._attachments_audit_listing(message.attachments)}"
        )

    @staticmethod
    def _draft_with_attachments(text: str, attachments: Sequence[discord.Attachment]) -> str:
        if not attachments:
            return text
        names = "\n".join(f"- {attachment.filename}" for attachment in attachments)
        prefix = f"{text}\n\n" if text else ""
        return f"{prefix}Attachments:\n{names}"

    @classmethod
    def _original_draft(cls, message: discord.Message, context: ModerationContext) -> str:
        text = message.content
        if context.proposed_title is not None:
            text = f"Title: {context.proposed_title}\n\n{text}"
        return cls._draft_with_attachments(text, message.attachments)

    async def _backfill_missed_messages(self, guild: discord.Guild) -> None:
        processed = 0
        scanned_channels = 0
        cutoff = datetime.now(UTC) - timedelta(seconds=self.config.backfill_lookback_seconds)
        await self._audit(
            "**Backfill started**\n"
            f"State file: `{self.config.state_path}`\n"
            f"Initial no-cursor lookback: `{self.config.backfill_lookback_seconds}` seconds"
        )

        async for channel in self._history_channels(guild):
            scanned_channels += 1
            cursor = await self.state.cursor(channel.id)
            after = discord.Object(id=cursor) if cursor is not None else cutoff
            try:
                for message_id in await self.state.pending(channel.id):
                    message = await self._fetch_message(channel, message_id)
                    if message is None or not self._message_in_scope(message):
                        await self.state.clear_pending(channel.id, message_id)
                        continue
                    await self._process_message(message, source="backfill_retry")
                    processed += 1

                # Message-created chat threads and forum posts expose their title only
                # through the thread container. Fetch the starter explicitly because
                # thread.history() does not consistently include it.
                if isinstance(channel, discord.Thread):
                    deleted = await self._process_thread_title(channel, source="backfill_thread")
                    processed += 1
                    if deleted:
                        continue
                async for message in channel.history(
                    limit=None,
                    after=after,
                    oldest_first=True,
                ):
                    if not self._message_in_scope(message):
                        continue
                    await self._process_message(message, source="backfill")
                    processed += 1
            except discord.Forbidden:
                LOG.warning("Cannot read history for channel %s", channel.id)
            except discord.HTTPException:
                LOG.exception("Failed reading history for channel %s", channel.id)

        await self._audit(
            "**Backfill finished**\n"
            f"Channels scanned: `{scanned_channels}`\n"
            f"Candidate messages processed/skipped: `{processed}`"
        )

    async def _history_channels(
        self, guild: discord.Guild
    ) -> AsyncIterator[discord.abc.Messageable]:
        for channel in guild.text_channels:
            if self._channel_is_ignored(channel):
                continue
            if self.audit_channel is not None and channel.id == self.audit_channel.id:
                continue
            permissions = channel.permissions_for(guild.me)
            if permissions.view_channel and permissions.read_message_history:
                yield channel

        # Active public/private threads are enough for normal restart gaps. Older archived
        # threads require channel-specific archive scans and can be added if needed.
        for thread in guild.threads:
            if self._channel_is_ignored(thread):
                continue
            if self.audit_channel is not None and thread.id == self.audit_channel.id:
                continue
            permissions = thread.permissions_for(guild.me)
            if permissions.view_channel and permissions.read_message_history:
                yield thread

    async def _audit(self, text: str) -> bool:
        channel = self.audit_channel
        if channel is None:
            LOG.error("Audit channel is not ready")
            return False
        try:
            for part in chunks(text):
                await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException:
            LOG.exception("Could not write to audit channel %s", channel.id)
            return False

    @staticmethod
    def _attachments_audit_listing(attachments: Sequence[discord.Attachment]) -> str:
        lines = [
            f"- `{attachment.filename}` ({attachment.size} bytes, "
            f"{attachment.content_type or 'unknown type'}): {attachment.url}"
            for attachment in attachments
        ]
        return "\n".join(lines) if lines else "None"

    def _received_audit(self, message: discord.Message, *, source: str) -> str:
        channel_name = getattr(message.channel, "name", "unknown")
        body = message.content if message.content else "_(no text content)_"
        attachments = self._attachments_audit_listing(message.attachments)
        return (
            f"**Message received** ({source})\n"
            f"Message: `{message.id}` · [jump]({message.jump_url})\n"
            f"Author: `{message.author}` (`{message.author.id}`)\n"
            f"Channel: `#{channel_name}` (`{message.channel.id}`)\n\n"
            f"**Content**\n{quote(body)}\n\n"
            f"**Attachments**\n{attachments}"
        )

    @staticmethod
    def _judgement_audit(message: discord.Message, judgement: str) -> str:
        return (
            f"**Moderation judgement** — message `{message.id}`\n{judgement}\nBot response: None."
        )

    @staticmethod
    def _blocked_audit(
        message: discord.Message,
        result: ModerationResult,
        notice: tuple[str, ...],
    ) -> str:
        return (
            f"**Moderation judgement** — message `{message.id}`\n"
            f"**BLOCKED**\n\n**Reasons**\n{reasons(result)}\n\n"
            f"**Bot response (removal DM)**\n{response_for_audit(notice)}"
        )

    def _interaction_context(self, interaction: discord.Interaction) -> str:
        if interaction.guild is None:
            return "DM"
        channel_id = interaction.channel_id
        return f"guild `{interaction.guild.id}`, channel `{channel_id}`"

    async def _validation_context(
        self,
        interaction: discord.Interaction,
        prepared: PreparedAttachments | None = None,
    ) -> ModerationContext:
        channel = interaction.channel
        if channel is None:
            return ModerationContext(channel_name="DM")

        channel_name = getattr(channel, "name", "DM") or "DM"
        channel_description = getattr(channel, "topic", None) or ""
        channel_type = "general_chat"
        thread_title: str | None = None
        thread_root: str | None = None
        forum_rules_version: str | None = None
        if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
            channel_type = "forum_reply"
            channel_description = channel.parent.topic or ""
            thread_title = channel.name
            forum_rules_version = "forum-rules.md (integrated policy)"
            starter = await self._fetch_message(channel, channel.id)
            thread_root = starter.content if starter is not None else None

        recent_context: list[dict[str, str]] = []
        recent_same_author: list[str] = []
        if hasattr(channel, "history"):
            try:
                history = [message async for message in channel.history(limit=15)]
                history.reverse()
                for message in history:
                    if message.author.bot or message.webhook_id is not None:
                        continue
                    recent_context.append(
                        {"author_id": str(message.author.id), "message": message.content}
                    )
                    if message.author.id == interaction.user.id:
                        recent_same_author.append(message.content)
            except discord.HTTPException:
                LOG.warning("Could not fetch context for validation by %s", interaction.user.id)

        return ModerationContext(
            channel_type=channel_type,
            channel_name=channel_name,
            channel_description=channel_description,
            forum_rules_version=forum_rules_version,
            thread_title=thread_title,
            thread_root=thread_root,
            requested_action=self._requested_action(thread_root),
            recent_context=recent_context,
            recent_same_author=recent_same_author,
            attachments=prepared.metadata if prepared else [],
            cited_material_accessible=(
                True
                if prepared and prepared.images
                else False
                if prepared and prepared.metadata
                else None
            ),
        )

    def _register_commands(self) -> None:
        @self.tree.command(name="validate", description="Validate a draft and optional image")
        @app_commands.describe(
            text="The draft text to validate before posting",
            image="An optional draft image attachment to validate",
        )
        async def validate(
            interaction: discord.Interaction,
            text: str | None = None,
            image: discord.Attachment | None = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            text = text or ""
            attachment_list = [image] if image is not None else []
            prepared = await self._prepare_attachments(attachment_list)
            displayed_draft = self._draft_with_attachments(text, attachment_list)
            attachment_audit = (
                f"\n\n**Attachment**\n`{image.filename}` ({image.size} bytes, "
                f"{image.content_type or 'unknown type'}): {image.url}"
                if image is not None
                else ""
            )
            request_audit = (
                "**Validation request**\n"
                f"User: `{interaction.user}` (`{interaction.user.id}`)\n"
                f"Context: {self._interaction_context(interaction)}\n\n"
                f"**Draft**\n{quote(displayed_draft or '_(empty draft)_')}"
                f"{attachment_audit}"
            )
            if not await self._audit(request_audit):
                await _send_followups(
                    interaction,
                    "⚠️ Validation is unavailable because audit logging is unavailable.",
                )
                return
            if prepared.metadata and not await self._audit(
                self._attachment_analysis_audit("validation", interaction.id, prepared)
            ):
                await _send_followups(
                    interaction,
                    "⚠️ Validation is unavailable because attachment audit logging failed.",
                )
                return

            try:
                if not text.strip() and image is None:
                    output = ("⚠️ Provide draft text, an image, or both.",)
                    judgement = "NOT ASSESSED — empty draft."
                elif prepared.unavailable_images:
                    output = ("⚠️ The image could not be analysed, so the draft was not assessed.",)
                    judgement = "ERROR — image attachment unavailable; failed open."
                else:
                    context = await self._validation_context(interaction, prepared)
                    result = await self.moderator.moderate(
                        text, context=context, images=prepared.images
                    )
                    output = validation_notice(displayed_draft, result)
                    judgement = "ALLOWED" if result.allowed else f"BLOCKED\n\n{reasons(result)}"
            except ModerationServiceError:
                LOG.exception("Validation unavailable (user=%s)", interaction.user.id)
                output = ("⚠️ Validation is temporarily unavailable. Your draft was not assessed.",)
                judgement = "ERROR — classifier unavailable."

            response_audit = (
                "**Validation judgement and bot response**\n"
                f"User: `{interaction.user.id}`\nJudgement: **{judgement}**\n\n"
                f"**Bot response**\n{response_for_audit(output)}"
            )
            if not await self._audit(response_audit):
                output = (
                    "⚠️ Validation completed, but the response could not be audited. "
                    "Try again later.",
                )
            await _send_followups(interaction, output)

        @self.tree.command(name="rules", description="Show the current moderation rules")
        async def rules_command(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            await self._audit(
                "**Command and bot response**\n"
                f"User: `{interaction.user}` (`{interaction.user.id}`)\nCommand: `/rules`\n\n"
                f"**Bot response**\n{quote(self.rules)}"
            )
            await _send_followups(interaction, self.rules)

        @self.tree.command(name="help", description="Show CoronetBot commands and configuration")
        async def help_command(interaction: discord.Interaction) -> None:
            output = (
                f"**CoronetBot {__version__}**\n\n"
                "`/validate [text] [image]` — validate and refine a draft before posting\n"
                "`/rules` — show the moderation rules\n"
                "`/help` — show this message\n\n"
                f"Mode: `{self.config.mode}` (`{self.config.guild_name}`)\n"
                f"LLM: `codex/{self.config.llm_model}` "
                f"(`{self.config.llm_thinking}` reasoning)\n"
                f"Rules file: `{self.config.rules_path}`\n"
                "If moderation or audit logging fails, the bot leaves the message in place."
            )
            await self._audit(
                "**Command and bot response**\n"
                f"User: `{interaction.user}` (`{interaction.user.id}`)\nCommand: `/help`\n\n"
                f"**Bot response**\n{quote(output)}"
            )
            await interaction.response.send_message(output, ephemeral=True)


async def _send_followups(
    interaction: discord.Interaction,
    response: str | Sequence[str],
) -> None:
    response_parts = (response,) if isinstance(response, str) else response
    for response_part in response_parts:
        for part in chunks(response_part):
            await interaction.followup.send(
                part, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )


def run() -> None:
    config = Config.from_env()
    rules = Path(config.rules_path).read_text(encoding="utf-8")
    client = CoronetClient(config, rules)
    client.run(config.discord_token, log_handler=None)
