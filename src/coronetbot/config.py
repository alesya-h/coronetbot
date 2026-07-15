from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


THINKING_LEVELS = {"minimal", "low", "medium", "high", "xhigh"}
SERVERS = {
    "production": (1439793454153601066, "Coronet"),
    "dev": (1526764377171296296, "Alesya's test server"),
}


def _integer(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    mode: str
    guild_id: int
    guild_name: str
    llm_model: str
    llm_thinking: str
    rules_path: Path
    max_concurrency: int
    llm_timeout_seconds: int
    llm_retries: int

    @classmethod
    def from_env(cls) -> Config:
        discord_token = os.environ.get("CB_DISCORD_TOKEN", "").strip()
        if not discord_token:
            raise ConfigurationError("CB_DISCORD_TOKEN is required")

        mode = os.environ.get("CB_MODE", "dev").strip()
        if mode not in SERVERS:
            raise ConfigurationError("CB_MODE must be 'dev' or 'production'")
        guild_id, guild_name = SERVERS[mode]

        rules_path = Path(os.environ.get("CB_RULES_PATH", "RULES.md"))
        if not rules_path.is_file():
            raise ConfigurationError(f"rules file does not exist: {rules_path}")

        thinking = os.environ.get("CB_LLM_THINKING", "high").strip()
        if thinking not in THINKING_LEVELS:
            levels = ", ".join(sorted(THINKING_LEVELS))
            raise ConfigurationError(f"CB_LLM_THINKING must be one of: {levels}")

        model = os.environ.get("CB_LLM_MODEL", "gpt-5.6-sol").strip()
        if not model:
            raise ConfigurationError("CB_LLM_MODEL must not be empty")

        codex_home = os.environ.get("CB_CODEX_HOME", "").strip()
        if codex_home:
            # codex-backend-sdk reads CODEX_HOME directly. Keep the public bot
            # configuration consistently CB_-prefixed and bridge it here.
            os.environ["CODEX_HOME"] = codex_home

        return cls(
            discord_token=discord_token,
            mode=mode,
            guild_id=guild_id,
            guild_name=guild_name,
            llm_model=model,
            llm_thinking=thinking,
            rules_path=rules_path,
            max_concurrency=_integer("CB_MAX_CONCURRENCY", 2),
            llm_timeout_seconds=_integer("CB_LLM_TIMEOUT_SECONDS", 120),
            llm_retries=_integer("CB_LLM_RETRIES", 2, minimum=0),
        )
