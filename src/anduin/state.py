"""OAuth token persistence under STATE_DIRECTORY.

systemd's `StateDirectory=anduin/state/<source>` gives us a writable dir owned
by the service user. We store one JSON file per source, 0600.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    extra: dict | None = None

    def to_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "extra": self.extra or {},
        }

    @classmethod
    def from_json(cls, d: dict) -> "OAuthToken":
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_at=float(d["expires_at"]),
            extra=d.get("extra"),
        )


def _path(state_dir: Path, source: str) -> Path:
    return state_dir / source / "token.json"


def load_token(state_dir: Path, source: str) -> OAuthToken | None:
    p = _path(state_dir, source)
    if not p.exists():
        return None
    return OAuthToken.from_json(json.loads(p.read_text()))


def save_token(state_dir: Path, source: str, token: OAuthToken) -> None:
    p = _path(state_dir, source)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(token.to_json(), indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)
