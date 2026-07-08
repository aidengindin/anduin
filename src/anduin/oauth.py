"""Minimal OAuth2 refresh-token helper.

Loads the saved token from state, refreshes it if expired, persists the new
token, returns the access token. No interactive flow here — that lives in
oauth_flow.py and is only invoked by `anduin auth <source>`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from anduin import state as state_mod
from anduin.http import post_json

logger = logging.getLogger(__name__)

EXPIRY_SKEW_SEC = 60


@dataclass
class Provider:
    name: str
    token_url: str
    client_auth: str  # 'basic' or 'body'


GOOGLE_HEALTH = Provider(
    name="google-health",
    token_url="https://oauth2.googleapis.com/token",
    client_auth="body",
)

WITHINGS = Provider(
    name="withings",
    # Withings uses a non-standard endpoint that wraps OAuth2 in their custom
    # response envelope. We POST with action=requesttoken.
    token_url="https://wbsapi.withings.net/v2/oauth2",
    client_auth="body",
)


def access_token(
    http_client: httpx.Client,
    provider: Provider,
    state_dir: Path,
    client_id: str,
    client_secret: str,
) -> str:
    """Return a valid access token, refreshing on disk if expired."""
    tok = state_mod.load_token(state_dir, provider.name)
    if tok is None:
        raise RuntimeError(
            f"no saved token for {provider.name}; run `anduin auth {provider.name}` first"
        )
    if tok.expires_at - EXPIRY_SKEW_SEC > time.time():
        return tok.access_token

    logger.info("refreshing %s access token", provider.name)
    if provider is WITHINGS:
        data = {
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tok.refresh_token,
        }
        resp = post_json(http_client, provider.token_url, data=data)
        body = resp.get("body") if isinstance(resp, dict) else None
        if not body or "access_token" not in body:
            raise RuntimeError(f"withings refresh failed: {resp!r}")
        new = state_mod.OAuthToken(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", tok.refresh_token),
            expires_at=time.time() + float(body["expires_in"]),
            extra={"userid": body.get("userid")},
        )
    else:
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tok.refresh_token,
        }
        resp = post_json(http_client, provider.token_url, data=data)
        if "access_token" not in resp:
            raise RuntimeError(f"{provider.name} refresh failed: {resp!r}")
        new = state_mod.OAuthToken(
            access_token=resp["access_token"],
            refresh_token=resp.get("refresh_token", tok.refresh_token),
            expires_at=time.time() + float(resp["expires_in"]),
        )
    state_mod.save_token(state_dir, provider.name, new)
    return new.access_token
