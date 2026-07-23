"""Withings extractor — body composition + blood pressure.

Endpoint: POST https://wbsapi.withings.net/measure?action=getmeas with
meastypes=<comma list> (category=1, real measurements). OAuth2 via the helper
in anduin.oauth.

Withings returns each measure as (value, unit) where final value = value *
10**unit, tagged with an integer `type`. One measuregrp carries several measures
(a scale group: weight + fat + muscle + ...; a BP-monitor group: systolic +
diastolic + pulse); each maps to its own raw.samples row via _MEASURE_TYPES.

Natural key = grpid (Withings measurement-group ID, stable across re-pulls). The
raw.samples conflict key is (source, metric, natural_key, valid_from), so the
shared grpid never collides across the distinct metrics in one group.
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

# Withings meastype -> (metric, unit, recording_method). Only these types are
# requested and mapped; anything else in the response is ignored. Values are all
# confirmed present in live getmeas data.
_MEASURE_TYPES: dict[int, tuple[str, str, str]] = {
    1:   ("body_weight",               "kg",    "scale"),
    5:   ("fat_free_mass",             "kg",    "scale"),
    6:   ("body_fat_ratio",            "%",     "scale"),
    8:   ("fat_mass",                  "kg",    "scale"),
    76:  ("muscle_mass",               "kg",    "scale"),
    77:  ("hydration",                 "kg",    "scale"),
    88:  ("bone_mass",                 "kg",    "scale"),
    170: ("visceral_fat",              "index", "scale"),
    9:   ("blood_pressure_diastolic",  "mmHg",  "bp_monitor"),
    10:  ("blood_pressure_systolic",   "mmHg",  "bp_monitor"),
}


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


def _measure_rows(body: dict | None) -> list[dict]:
    """Map a getmeas response body into raw.samples rows.

    Emits one row per mapped measure (see _MEASURE_TYPES). Skips groups without
    a stable grpid/date, measures of unmapped types, and measures missing their
    value/unit (malformed).
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
            mapping = _MEASURE_TYPES.get(m.get("type"))
            if mapping is None:
                continue
            metric, unit, recording_method = mapping
            value = _measure_value(m)
            if value is None:
                logger.warning("withings: skipping malformed measure in grp %s: %r", grpid, m)
                continue
            rows.append({
                "source": "withings",
                "device": str(device) if device is not None else None,
                "recording_method": recording_method,
                "metric": metric,
                "value": value,
                "unit": unit,
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
    user_id = app.file.user_id
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
                "meastypes": ",".join(str(t) for t in sorted(_MEASURE_TYPES)),
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

    rows = _measure_rows(resp.get("body") or {})

    if dry_run:
        logger.info("withings: would upsert %d measure rows", len(rows))
        return result

    try:
        n = upsert_samples(conn, user_id, rows)
        result.add("raw.samples", n)
    except Exception as e:  # noqa: BLE001
        result.error(f"withings upsert: {e!r}")
    return result
