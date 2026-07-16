from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands

from . import __version__
from .config import Config
from .formatting import chunks, quote, reasons, removal_notice, validation_notice
from .models import ModerationResult
from .moderator import ModerationContext, ModerationServiceError, Moderator
from .state import StateStore

LOG = logging.getLogger(__name__)
AUDIT_CHANNEL_NAME = "bot-moderation-audit"


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

    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self._backfill_complete.wait()
        await self._process_thread_title(thread, source="thread_create")

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        if before.name == after.name:
            return
        await self._backfill_complete.wait()
        await self._process_thread_title(after, source="thread_rename")

    async def _process_thread_title(self, thread: discord.Thread, *, source: str) -> bool:
        if thread.guild.id != self.config.guild_id:
            return False
        async with self._channel_locks[thread.id]:
            if await self.state.thread_title_seen(thread.id, thread.name):
                return False

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
            context = ModerationContext(
                channel_type="forum_post" if is_forum else "general_chat",
                channel_name=parent_name,
                channel_description=getattr(parent, "topic", None) or "",
                forum_rules_version="forum-rules.md (integrated policy)" if is_forum else None,
                thread_title=thread.name,
                thread_root=body or None,
                requested_action=self._requested_action(body),
                proposed_title=thread.name,
                attachments=(
                    [
                        {
                            "filename": attachment.filename,
                            "content_type": attachment.content_type,
                            "size": attachment.size,
                            "source_url": attachment.url,
                            "extracted_text": None,
                        }
                        for attachment in starter.attachments
                    ]
                    if starter is not None
                    else []
                ),
            )
            original_draft = f"Title: {thread.name}" + (f"\n\n{body}" if body else "")
            received = (
                f"**Thread title received** ({source})\n"
                f"Thread: `{thread.id}` · Parent: `#{parent_name}`\n"
                f"Author: `{author}` (`{getattr(author, 'id', thread.owner_id)}`)\n\n"
                f"**Draft**\n{quote(original_draft)}"
            )
            if not await self._audit(received):
                return False

            try:
                result = await self.moderator.moderate(body, context=context)
            except ModerationServiceError:
                LOG.exception("Thread-title moderation unavailable (thread=%s)", thread.id)
                if await self._audit(
                    f"**Thread-title judgement** — thread `{thread.id}`\n"
                    "ERROR — classifier unavailable; failed open."
                ):
                    await self.state.mark_thread_title(thread.id, thread.name)
                return False

            if result.allowed:
                if await self._audit(
                    f"**Thread-title judgement** — thread `{thread.id}`\n"
                    "ALLOWED — no rules violated.\nBot response: None."
                ):
                    await self.state.mark_thread_title(thread.id, thread.name)
                return False

            notice = removal_notice(parent_name, original_draft, result)
            if not await self._audit(
                f"**Thread-title judgement** — thread `{thread.id}`\n"
                f"**BLOCKED**\n\n**Reasons**\n{reasons(result)}\n\n"
                f"**Bot response (removal DM)**\n{quote(notice)}"
            ):
                return False

            dm_status = "not sent; author unavailable"
            if author is not None:
                dm_status = "sent"
                try:
                    for part in chunks(notice):
                        await author.send(part, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    dm_status = "failed"

            delete_status = "thread deleted"
            deleted = False
            try:
                await thread.delete(reason="CoronetBot moderation decision")
                deleted = True
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

            if await self._audit(
                f"**Thread-title actions** — thread `{thread.id}`\n"
                f"DM delivery: **{dm_status}**\nDeletion: **{delete_status}**"
            ):
                await self.state.mark_thread_title(thread.id, thread.name)
                if starter is not None and starter.id == thread.id:
                    await self.state.mark_processed(thread.id, starter.id)
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
            return

        context = await self._moderation_context(message)
        if not message.content.strip() and context.proposed_title is None:
            if await self._audit(
                self._judgement_audit(
                    message,
                    "ALLOWED — no textual content; attachments were logged but not analysed.",
                )
            ):
                await self.state.mark_processed(message.channel.id, message.id)
            return

        try:
            result = await self.moderator.moderate(message.content, context=context)
        except ModerationServiceError:
            LOG.exception(
                "Moderation unavailable; message left intact (guild=%s channel=%s message=%s)",
                message.guild.id,
                message.channel.id,
                message.id,
            )
            if await self._audit(
                self._judgement_audit(message, "ERROR — classifier unavailable; failed open.")
            ):
                await self.state.mark_processed(message.channel.id, message.id)
            return

        if result.allowed:
            if await self._audit(self._judgement_audit(message, "ALLOWED — no rules violated.")):
                await self.state.mark_processed(message.channel.id, message.id)
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
            return

        dm_status = "sent"
        try:
            for part in chunks(notice):
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
        try:
            if deletes_thread:
                await message.channel.delete(reason="CoronetBot moderation decision")
            else:
                await message.delete()
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

        if await self._audit(
            f"**Moderation actions** — message `{message.id}`\n"
            f"DM delivery: **{dm_status}**\nDeletion: **{delete_status}**"
        ):
            await self.state.mark_processed(message.channel.id, message.id)

    def _message_in_scope(self, message: discord.Message) -> bool:
        return not (
            message.guild is None
            or message.guild.id != self.config.guild_id
            or message.author.bot
            or message.webhook_id is not None
            or (self.audit_channel is not None and message.channel.id == self.audit_channel.id)
        )

    async def _moderation_context(self, message: discord.Message) -> ModerationContext:
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
        attachments = [
            {
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "source_url": attachment.url,
                "extracted_text": None,
            }
            for attachment in message.attachments
        ]

        return ModerationContext(
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
            attachments=attachments,
            cited_material_accessible=True if attachments else None,
        )

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
    def _original_draft(message: discord.Message, context: ModerationContext) -> str:
        if context.proposed_title is not None:
            return f"Title: {context.proposed_title}\n\n{message.content}"
        return message.content

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
            if self.audit_channel is not None and channel.id == self.audit_channel.id:
                continue
            permissions = channel.permissions_for(guild.me)
            if permissions.view_channel and permissions.read_message_history:
                yield channel

        # Active public/private threads are enough for normal restart gaps. Older archived
        # threads require channel-specific archive scans and can be added if needed.
        for thread in guild.threads:
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

    def _received_audit(self, message: discord.Message, *, source: str) -> str:
        channel_name = getattr(message.channel, "name", "unknown")
        body = message.content if message.content else "_(no text content)_"
        attachment_lines = [
            f"- `{attachment.filename}` ({attachment.size} bytes, "
            f"{attachment.content_type or 'unknown type'}): {attachment.url}"
            for attachment in message.attachments
        ]
        attachments = "\n".join(attachment_lines) if attachment_lines else "None"
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
    def _blocked_audit(message: discord.Message, result: ModerationResult, notice: str) -> str:
        return (
            f"**Moderation judgement** — message `{message.id}`\n"
            f"**BLOCKED**\n\n**Reasons**\n{reasons(result)}\n\n"
            f"**Bot response (removal DM)**\n{quote(notice)}"
        )

    def _interaction_context(self, interaction: discord.Interaction) -> str:
        if interaction.guild is None:
            return "DM"
        channel_id = interaction.channel_id
        return f"guild `{interaction.guild.id}`, channel `{channel_id}`"

    async def _validation_context(self, interaction: discord.Interaction) -> ModerationContext:
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
        )

    def _register_commands(self) -> None:
        @self.tree.command(name="validate", description="Validate and refine a draft message")
        @app_commands.describe(text="The draft to validate before posting")
        async def validate(interaction: discord.Interaction, text: str) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            request_audit = (
                "**Validation request**\n"
                f"User: `{interaction.user}` (`{interaction.user.id}`)\n"
                f"Context: {self._interaction_context(interaction)}\n\n"
                f"**Draft**\n{quote(text)}"
            )
            if not await self._audit(request_audit):
                await _send_followups(
                    interaction,
                    "⚠️ Validation is unavailable because audit logging is unavailable.",
                )
                return

            try:
                context = await self._validation_context(interaction)
                result = await self.moderator.moderate(text, context=context)
                output = validation_notice(text, result)
                judgement = "ALLOWED" if result.allowed else f"BLOCKED\n\n{reasons(result)}"
            except ModerationServiceError:
                LOG.exception("Validation unavailable (user=%s)", interaction.user.id)
                output = "⚠️ Validation is temporarily unavailable. Your draft was not assessed."
                judgement = "ERROR — classifier unavailable."

            response_audit = (
                "**Validation judgement and bot response**\n"
                f"User: `{interaction.user.id}`\nJudgement: **{judgement}**\n\n"
                f"**Bot response**\n{quote(output)}"
            )
            if not await self._audit(response_audit):
                output = (
                    "⚠️ Validation completed, but the response could not be audited. "
                    "Try again later."
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
                "`/validate text` — validate and refine a draft before posting\n"
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


async def _send_followups(interaction: discord.Interaction, text: str) -> None:
    for part in chunks(text):
        await interaction.followup.send(
            part, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


def run() -> None:
    config = Config.from_env()
    rules = Path(config.rules_path).read_text(encoding="utf-8")
    client = CoronetClient(config, rules)
    client.run(config.discord_token, log_handler=None)
