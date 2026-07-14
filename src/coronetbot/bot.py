from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands

from . import __version__
from .config import Config
from .formatting import chunks, removal_notice, validation_notice
from .moderator import ModerationServiceError, Moderator

LOG = logging.getLogger(__name__)


class CoronetClient(discord.Client):
    def __init__(self, config: Config, rules: str) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.rules = rules
        self.tree = app_commands.CommandTree(self)
        self.moderator = Moderator(
            api_url=config.llm_base_url,
            api_key=config.llm_api_key,
            model=config.llm_model,
            rules=rules,
            max_concurrency=config.max_concurrency,
            timeout_seconds=config.llm_timeout_seconds,
            retries=config.llm_retries,
        )
        self._register_commands()

    async def setup_hook(self) -> None:
        await self.moderator.__aenter__()
        if self.config.guild_id is not None:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            LOG.info("Synced commands to development guild %s", self.config.guild_id)
        else:
            await self.tree.sync()
            LOG.info("Synced global commands")

    async def close(self) -> None:
        await self.moderator.__aexit__()
        await super().close()

    async def on_ready(self) -> None:
        assert self.user is not None
        LOG.info("Logged in as %s (%s), model=%s", self.user, self.user.id, self.config.llm_model)

    async def on_message(self, message: discord.Message) -> None:
        # Server messages only. Ignore bots/webhooks to prevent loops and bot-on-bot moderation.
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        if not message.content.strip():
            return

        try:
            result = await self.moderator.moderate(message.content)
        except ModerationServiceError:
            # Fail open: uncertain or unavailable moderation must never delete user content.
            LOG.exception(
                "Moderation unavailable; message left intact (guild=%s channel=%s message=%s)",
                message.guild.id,
                message.channel.id,
                message.id,
            )
            return

        if result.allowed:
            return

        channel_name = getattr(message.channel, "name", "unknown")
        notice = removal_notice(channel_name, message.content, result)
        try:
            for part in chunks(notice):
                await message.author.send(part, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            LOG.warning(
                "Could not DM moderated user (guild=%s channel=%s message=%s user=%s)",
                message.guild.id,
                message.channel.id,
                message.id,
                message.author.id,
            )

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
            LOG.exception(
                "Could not delete rule-breaking message (guild=%s channel=%s message=%s)",
                message.guild.id,
                message.channel.id,
                message.id,
            )

    def _register_commands(self) -> None:
        @self.tree.command(name="validate", description="Validate and refine a draft message")
        @app_commands.describe(text="The draft to validate before posting")
        async def validate(interaction: discord.Interaction, text: str) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await self.moderator.moderate(text)
                output = validation_notice(text, result)
            except ModerationServiceError:
                LOG.exception("Validation unavailable (user=%s)", interaction.user.id)
                output = "⚠️ Validation is temporarily unavailable. Your draft was not assessed."
            await _send_followups(interaction, output)

        @self.tree.command(name="rules", description="Show the current moderation rules")
        async def rules_command(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            await _send_followups(interaction, self.rules)

        @self.tree.command(name="help", description="Show CoronetBot commands and configuration")
        async def help_command(interaction: discord.Interaction) -> None:
            output = (
                f"**CoronetBot {__version__}**\n\n"
                "`/validate text` — validate and refine a draft before posting\n"
                "`/rules` — show the moderation rules\n"
                "`/help` — show this message\n\n"
                f"LLM model: `{self.config.llm_model}`\n"
                f"Rules file: `{self.config.rules_path}`\n"
                "Moderation failures fail open: messages are not deleted unless the classifier "
                "returns a valid rule violation."
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
