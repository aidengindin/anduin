"""Headache check-in reminders over ntfy.

Run by a systemd timer at a few fixed local times (``anduin remind headache``).
Each run asks "did the owner check in recently?" and, if not, publishes one
notification with two action buttons:

* **No headache** -- an ``http`` action the phone's ntfy client performs
  itself: a form POST of ``intensity=0`` to anduin over the tailnet. One tap,
  the app never opens, and the notification clears.
* **Log** -- a ``view`` action that opens the Log tab for anything above zero.

Two buttons stay inside iOS's three-action cap. The body carries no health
data, only the prompt text and the anduin URL -- on public ntfy.sh the topic
name is the only access control, so it is a secret (``NTFY_TOPIC``) and the
notification is written as if a stranger could read it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from anduin import journal
from anduin.config import AppConfig, HeadacheConfig
from anduin.http import post_json

logger = logging.getLogger(__name__)


def should_remind(latest_at: datetime | None, now: datetime, skip_minutes: int) -> bool:
    """False while the most recent check-in is inside the skip window."""
    if latest_at is None:
        return True
    return now - latest_at > timedelta(minutes=skip_minutes)


def build_notification(cfg: HeadacheConfig, topic: str) -> dict[str, Any]:
    """ntfy JSON-publish payload (POST to the server root, topic in the body)."""
    base = cfg.app_url.rstrip("/")
    return {
        "topic": topic,
        "title": "Headache check-in",
        "message": "How's your head right now?",
        "click": f"{base}/log",
        "tags": ["brain"],
        "actions": [
            {
                "action": "http",
                "label": "No headache",
                "url": f"{base}/api/log/headache",
                "method": "POST",
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "body": "intensity=0&source=ntfy",
                "clear": True,
            },
            {"action": "view", "label": "Log", "url": f"{base}/log"},
        ],
    }


def send(
    client: httpx.Client, cfg: HeadacheConfig, topic: str, token: str, payload: dict[str, Any]
) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    post_json(client, cfg.ntfy_url.rstrip("/") + "/", json=payload, headers=headers)


def run(
    client: httpx.Client, conn: Any, app: AppConfig, *,
    now: datetime | None = None, dry_run: bool = False,
) -> int:
    """One reminder pass. ``conn`` is a dict-row connection, or None on a dry
    run, which logs the payload and neither reads the DB nor publishes."""
    cfg = app.file.headache
    topic = app.secrets.ntfy_topic
    if not topic:
        logger.error("NTFY_TOPIC is not set")
        return 2
    if not cfg.app_url:
        logger.error("headache.app_url is not configured (the URL the phone reaches anduin on)")
        return 2
    now = now or datetime.now(timezone.utc)
    payload = build_notification(cfg, topic)
    if dry_run:
        logger.info("dry-run: would publish to %s: %s", cfg.ntfy_url, json.dumps(payload))
        return 0
    latest = journal.latest_checkin_at(conn, app.file.user_id)
    if not should_remind(latest, now, cfg.remind_skip_within_minutes):
        logger.info("skipping: last check-in at %s is within %d min",
                    latest.isoformat(), cfg.remind_skip_within_minutes)
        return 0
    try:
        send(client, cfg, topic, app.secrets.ntfy_token, payload)
    except httpx.HTTPError as e:
        logger.error("ntfy publish failed: %s", e)
        return 1
    logger.info("published headache reminder (last check-in: %s)",
                latest.isoformat() if latest else "never")
    return 0
