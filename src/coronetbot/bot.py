from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands

from . import __version__
from .config import Config
from .formatting import chunks, quote, reasons, removal_notice, validation_notice
from .models import ModerationResult
from .moderator import ModerationServiceError, Moderator
from .state import StateStore

LOG = logging.getLogger(__name__)
AUDIT_CHANNEL_NAME = "bot-spam"


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
            await self._backfill_missed_messages(guild)

    async def on_message(self, message: discord.Message) -> None:
        await self._process_message(message, source="live")

    async def _process_message(self, message: discord.Message, *, source: str) -> None:
        if not self._message_in_scope(message):
            return
        if await self.state.seen(message.channel.id, message.id):
            return

        if not await self._audit(self._received_audit(message, source=source)):
            LOG.error("Audit unavailable; message left intact (message=%s)", message.id)
            return

        if not message.content.strip():
            if await self._audit(
                self._judgement_audit(
                    message,
                    "ALLOWED — no textual content; attachments were logged but not analysed.",
                )
            ):
                await self.state.mark_processed(message.channel.id, message.id)
            return

        try:
            result = await self.moderator.moderate(message.content)
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
        notice = removal_notice(channel_name, message.content, result)
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

        delete_status = "deleted"
        try:
            await message.delete()
            LOG.info(
                "Deleted rule-breaking message (guild=%s channel=%s message=%s user=%s rules=%s)",
                message.guild.id,
                message.channel.id,
                message.id,
                message.author.id,
                ", ".join(v.rule for v in result.violations),
            )
        except discord.HTTPException:
            delete_status = "failed"
            LOG.exception(
                "Could not delete rule-breaking message (guild=%s channel=%s message=%s)",
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
                result = await self.moderator.moderate(text)
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
                "Messages, judgements, and bot responses are retained in `#bot-spam`.\n"
                "Moderation failures fail open: messages are not deleted unless the classifier "
                "returns a valid rule violation."
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
