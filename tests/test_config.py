import os
from pathlib import Path

import pytest

from coronetbot.config import Config, ConfigurationError


def test_dev_config_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text("Be civil")
    monkeypatch.setenv("CB_DISCORD_TOKEN", "token")
    monkeypatch.setenv("CB_RULES_PATH", str(rules))
    monkeypatch.setenv("CB_MODE", "dev")
    config = Config.from_env()
    assert config.discord_token == "token"
    assert config.guild_id == 1526764377171296296
    assert config.guild_name == "Alesya's test server"
    assert config.llm_model == "gpt-5.6-sol"
    assert config.llm_thinking == "high"


def test_production_server_and_codex_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text("Be civil")
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CB_DISCORD_TOKEN", "token")
    monkeypatch.setenv("CB_RULES_PATH", str(rules))
    monkeypatch.setenv("CB_MODE", "production")
    monkeypatch.setenv("CB_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_HOME", "previous-test-value")
    config = Config.from_env()
    assert config.guild_id == 1439793454153601066
    assert config.guild_name == "Coronet"
    assert os.environ["CODEX_HOME"] == str(codex_home)


def test_discord_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_DISCORD_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="CB_DISCORD_TOKEN"):
        Config.from_env()


def test_mode_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_DISCORD_TOKEN", "token")
    monkeypatch.setenv("CB_MODE", "staging")
    with pytest.raises(ConfigurationError, match="CB_MODE"):
        Config.from_env()
