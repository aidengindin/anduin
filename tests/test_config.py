"""Config loading: .env support + dev-friendly state-dir override."""

from __future__ import annotations

from anduin.config import Secrets, load


def test_secrets_reads_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://from-dotenv\nGOOGLE_HEALTH_CLIENT_ID=abc123\n"
    )
    s = Secrets()
    assert s.database_url == "postgresql://from-dotenv"
    assert s.google_health_client_id == "abc123"


def test_real_env_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://from-dotenv\n")
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env")
    s = Secrets()
    assert s.database_url == "postgresql://from-env"


def test_load_honors_anduin_state_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env in this dir
    monkeypatch.delenv("ANDUIN_CONFIG", raising=False)
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("ANDUIN_STATE_DIR", str(tmp_path / "state"))
    app = load()
    assert app.file.state_dir == tmp_path / "state"


def test_anduin_state_dir_beats_systemd_state_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANDUIN_CONFIG", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("STATE_DIRECTORY", "/var/lib/anduin/state")
    monkeypatch.setenv("ANDUIN_STATE_DIR", str(tmp_path / "dev-state"))
    app = load()
    assert app.file.state_dir == tmp_path / "dev-state"
