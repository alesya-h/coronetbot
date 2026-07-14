from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


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
    llm_api_key: str | None
    llm_base_url: str
    llm_model: str
    rules_path: Path
    guild_id: int | None
    max_concurrency: int
    llm_timeout_seconds: int
    llm_retries: int

    @classmethod
    def from_env(cls) -> Config:
        discord_token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not discord_token:
            raise ConfigurationError("DISCORD_TOKEN is required")

        raw_guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        try:
            guild_id = int(raw_guild_id) if raw_guild_id else None
        except ValueError as exc:
            raise ConfigurationError("DISCORD_GUILD_ID must be an integer") from exc

        rules_path = Path(os.environ.get("RULES_PATH", "RULES.md"))
        if not rules_path.is_file():
            raise ConfigurationError(f"rules file does not exist: {rules_path}")

        base_url = os.environ.get(
            "LLM_BASE_URL", "https://api.openai.com/v1/chat/completions"
        ).strip()
        if not base_url.startswith(("http://", "https://")):
            raise ConfigurationError("LLM_BASE_URL must be an HTTP(S) URL")

        return cls(
            discord_token=discord_token,
            llm_api_key=os.environ.get("LLM_API_KEY") or None,
            llm_base_url=base_url,
            llm_model=os.environ.get("LLM_MODEL", "gpt-4.1-mini").strip(),
            rules_path=rules_path,
            guild_id=guild_id,
            max_concurrency=_integer("MAX_CONCURRENCY", 8),
            llm_timeout_seconds=_integer("LLM_TIMEOUT_SECONDS", 30),
            llm_retries=_integer("LLM_RETRIES", 2, minimum=0),
        )
