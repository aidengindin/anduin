"""Interactive OAuth2 seeding flows.

`anduin auth <source>` runs a tiny loopback HTTP listener, prints the auth URL,
waits for the redirect, exchanges the code, and returns an OAuthToken. The CLI
caller persists it via state.save_token.

Intended to be run once per source from an interactive shell (SSH into osgiliath
with a port-forward back to your laptop, or directly on console). Not invoked
by systemd.
"""

from __future__ import annotations

import http.server
import logging
import secrets
import time
import urllib.parse
import webbrowser

from anduin.http import make_client, post_json
from anduin.state import OAuthToken

logger = logging.getLogger(__name__)


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_HEALTH_SCOPES = [
    # Google Health API v4 scopes (health.googleapis.com/v4). The user must
    # enable these on their OAuth client. sleep -> the sleep data type;
    # activity_and_fitness -> steps/distance/active-energy-burned;
    # health_metrics_and_measurements -> heart-rate, spo2, hrv, resting HR,
    # respiratory rate (both the sample and daily variants).
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]

WITHINGS_AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_SCOPES = ["user.metrics"]


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    received: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        qs = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(qs))
        _CodeCatcher.received = params
        body = b"<html><body>OK, you can close this tab.</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kwargs) -> None:  # silence
        pass


def _await_code(port: int, expected_state: str) -> str:
    server = http.server.HTTPServer(("127.0.0.1", port), _CodeCatcher)
    logger.info("waiting for OAuth redirect on http://127.0.0.1:%d/", port)
    try:
        while True:
            server.handle_request()
            if "code" in _CodeCatcher.received and _CodeCatcher.received.get("state") == expected_state:
                return _CodeCatcher.received["code"]
            if "error" in _CodeCatcher.received:
                raise RuntimeError(f"oauth error: {_CodeCatcher.received}")
    finally:
        server.server_close()


def google_health_seed(*, client_id: str, client_secret: str, redirect_port: int) -> OAuthToken:
    redirect_uri = f"http://127.0.0.1:{redirect_port}/"
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_HEALTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    logger.info("open this URL in a browser:\n  %s", url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass

    code = _await_code(redirect_port, state)
    with make_client() as client:
        resp = post_json(
            client,
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    return OAuthToken(
        access_token=resp["access_token"],
        refresh_token=resp["refresh_token"],
        expires_at=time.time() + float(resp["expires_in"]),
    )


def withings_seed(*, client_id: str, client_secret: str, redirect_port: int) -> OAuthToken:
    redirect_uri = f"http://127.0.0.1:{redirect_port}/"
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(WITHINGS_SCOPES),
        "state": state,
    }
    url = f"{WITHINGS_AUTH_URL}?{urllib.parse.urlencode(params)}"
    logger.info("open this URL in a browser:\n  %s", url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass

    code = _await_code(redirect_port, state)
    with make_client() as client:
        resp = post_json(
            client,
            WITHINGS_TOKEN_URL,
            data={
                "action": "requesttoken",
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    body = resp.get("body") if isinstance(resp, dict) else None
    if not body or "access_token" not in body:
        raise RuntimeError(f"withings token exchange failed: {resp!r}")
    return OAuthToken(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=time.time() + float(body["expires_in"]),
        extra={"userid": body.get("userid")},
    )
