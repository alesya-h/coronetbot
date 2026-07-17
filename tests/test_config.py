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
    assert config.state_path == Path(".coronetbot-state.json")
    assert config.backfill_lookback_seconds == 3600
    assert config.max_images_per_message == 4
    assert config.max_image_bytes == 8_000_000


def test_production_server_and_codex_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text("Be civil")
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CB_DISCORD_TOKEN", "token")
    monkeypatch.setenv("CB_RULES_PATH", str(rules))
    monkeypatch.setenv("CB_MODE", "production")
    monkeypatch.setenv("CB_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CB_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("CB_BACKFILL_LOOKBACK_SECONDS", "0")
    monkeypatch.setenv("CODEX_HOME", "previous-test-value")
    config = Config.from_env()
    assert config.guild_id == 1439793454153601066
    assert config.guild_name == "Coronet"
    assert os.environ["CODEX_HOME"] == str(codex_home)
    assert config.state_path == tmp_path / "state.json"
    assert config.backfill_lookback_seconds == 0


def test_discord_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_DISCORD_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="CB_DISCORD_TOKEN"):
        Config.from_env()


def test_mode_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_DISCORD_TOKEN", "token")
    monkeypatch.setenv("CB_MODE", "staging")
    with pytest.raises(ConfigurationError, match="CB_MODE"):
        Config.from_env()
