from __future__ import annotations

from pathlib import Path

from autosign.__main__ import initialize_master_key


def test_initialize_master_key_fills_empty_example_value(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("AUTOSIGN_PORT=8000\nAUTOSIGN_MASTER_KEY=\n", encoding="utf-8")

    assert initialize_master_key(env_path) == 0

    contents = env_path.read_text(encoding="utf-8")
    assert "AUTOSIGN_PORT=8000" in contents
    assert "AUTOSIGN_MASTER_KEY=\n" not in contents
    assert len(contents.partition("AUTOSIGN_MASTER_KEY=")[2].strip()) > 20


def test_initialize_master_key_does_not_replace_existing_value(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("AUTOSIGN_MASTER_KEY=already-set\n", encoding="utf-8")

    assert initialize_master_key(env_path) == 0
    assert env_path.read_text(encoding="utf-8") == "AUTOSIGN_MASTER_KEY=already-set\n"
