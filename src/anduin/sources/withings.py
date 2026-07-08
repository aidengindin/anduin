"""Withings extractor — body weight only.

Endpoint: POST https://wbsapi.withings.net/measure?action=getmeas with
meastype=1 (weight, kg). OAuth2 via the helper in anduin.oauth.

Withings returns weight as (value, unit) where final value = value * 10**unit.
Natural key = grpid (Withings measurement-group ID, stable across re-pulls).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx
from psycopg import Connection

from anduin.config import AppConfig
from anduin.http import post_json
from anduin.oauth import WITHINGS, access_token
from anduin.sources.base import SourceResult
from anduin.upsert import upsert_samples

logger = logging.getLogger(__name__)

MEAS_URL = "https://wbsapi.withings.net/measure"
MEASTYPE_WEIGHT = 1


def _measure_value(m: dict) -> float | None:
    """Compute value * 10**unit for a Withings measure.

    Returns None when 'value' or 'unit' is missing/None (partial/malformed
    response), so the caller can skip that measure instead of crashing.
    """
    raw_value = m.get("value")
    raw_unit = m.get("unit")
    if raw_value is None or raw_unit is None:
        return None
    return float(raw_value) * (10 ** int(raw_unit))


def _weight_rows(body: dict | None) -> list[dict]:
    """Map a getmeas response body into raw.samples rows for body weight.

    Skips groups without a stable grpid/date and measures that are not
    weight or that are missing their value/unit (malformed).
    """
    rows: list[dict] = []
    for grp in (body or {}).get("measuregrps", []) or []:
        grpid = str(grp.get("grpid"))
        ts = grp.get("date")
        if not grpid or grpid == "None" or ts is None:
            continue
        valid_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        device = grp.get("model") or grp.get("deviceid")
        for m in grp.get("measures", []) or []:
            if m.get("type") != MEASTYPE_WEIGHT:
                continue
            value = _measure_value(m)
            if value is None:
                logger.warning("withings: skipping malformed measure in grp %s: %r", grpid, m)
                continue
            rows.append({
                "source": "withings",
                "device": str(device) if device is not None else None,
                "recording_method": "scale",
                "metric": "body_weight",
                "value": value,
                "unit": "kg",
                "valid_from": valid_at,
                "valid_to": valid_at,
                "natural_key": grpid,
                "raw": grp,
            })
    return rows


def extract(
    http: httpx.Client,
    conn: Connection,
    app: AppConfig,
    since: date,
    until: date,
    *,
    dry_run: bool = False,
) -> SourceResult:
    result = SourceResult(source="withings")
    if not app.secrets.withings_client_id or not app.secrets.withings_client_secret:
        result.error("withings: missing WITHINGS_CLIENT_ID or WITHINGS_CLIENT_SECRET")
        return result

    try:
        token = access_token(
            http,
            WITHINGS,
            app.file.state_dir,
            app.secrets.withings_client_id,
            app.secrets.withings_client_secret,
        )
    except Exception as e:  # noqa: BLE001
        result.error(f"withings auth: {e!r}")
        return result

    start_ts = int(datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(until, datetime.max.time(), tzinfo=timezone.utc).timestamp())

    try:
        resp = post_json(
            http,
            MEAS_URL,
            data={
                "action": "getmeas",
                "meastype": MEASTYPE_WEIGHT,
                "category": 1,
                "startdate": start_ts,
                "enddate": end_ts,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as e:  # noqa: BLE001
        result.error(f"withings getmeas: {e!r}")
        return result

    if not isinstance(resp, dict) or resp.get("status") != 0:
        result.error(f"withings getmeas non-zero status: {resp!r}")
        return result

    rows = _weight_rows(resp.get("body") or {})

    if dry_run:
        logger.info("withings: would upsert %d weight rows", len(rows))
        return result

    try:
        n = upsert_samples(conn, rows)
        result.add("raw.samples", n)
    except Exception as e:  # noqa: BLE001
        result.error(f"withings upsert: {e!r}")
    return result
