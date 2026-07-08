from __future__ import annotations

import os
import stat
from pathlib import Path

from anduin.state import OAuthToken, load_token, save_token


def test_save_then_load_roundtrip(tmp_path: Path):
    tok = OAuthToken(
        access_token="a",
        refresh_token="r",
        expires_at=1700000000.0,
        extra={"userid": "u1"},
    )
    save_token(tmp_path, "withings", tok)
    p = tmp_path / "withings" / "token.json"
    assert p.exists()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600

    loaded = load_token(tmp_path, "withings")
    assert loaded is not None
    assert loaded.access_token == "a"
    assert loaded.refresh_token == "r"
    assert loaded.expires_at == 1700000000.0
    assert loaded.extra == {"userid": "u1"}


def test_load_missing_returns_none(tmp_path: Path):
    assert load_token(tmp_path, "nope") is None
