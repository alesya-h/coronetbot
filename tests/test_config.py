from pathlib import Path

import pytest

from coronetbot.config import Config, ConfigurationError


def test_config_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text("Be civil")
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("RULES_PATH", str(rules))
    config = Config.from_env()
    assert config.discord_token == "token"
    assert config.llm_model == "gpt-4.1-mini"


def test_discord_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="DISCORD_TOKEN"):
        Config.from_env()
