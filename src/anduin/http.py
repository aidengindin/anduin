"""Shared httpx client + retrying request helpers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def make_client(**kwargs: Any) -> httpx.Client:
    transport = httpx.HTTPTransport(retries=2)
    return httpx.Client(timeout=DEFAULT_TIMEOUT, transport=transport, **kwargs)


def _safe_text(r: Any) -> str:
    """Best-effort response body for error logging, length-capped."""
    try:
        return (r.text or "")[:1000]
    except Exception:  # noqa: BLE001
        return "<no body>"


def _retry_request(
    fn,
    url: str,
    *,
    retries: int,
    backoff: float,
) -> Any:
    last_exc: Exception | None = None
    last_429: Any = None
    for attempt in range(retries):
        try:
            r = fn()
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt < retries - 1:
                sleep_for = backoff**attempt
                logger.warning(
                    "request failed (%s), retry %d/%d in %.1fs",
                    e.__class__.__name__, attempt + 1, retries, sleep_for,
                )
                time.sleep(sleep_for)
            continue

        if r.status_code == 429:
            last_429 = r
            wait = float(r.headers.get("retry-after", backoff**attempt))
            logger.warning("rate limited (429) on %s, sleeping %.1fs", url, wait)
            time.sleep(wait)
            continue

        if 400 <= r.status_code < 500:
            # Client errors are deterministic: retrying only wastes time. Surface
            # the response body (APIs put the real reason there, e.g. Google's
            # ACCOUNT_NOT_LINKED) and fail fast.
            logger.warning("client error %d on %s: %s", r.status_code, url, _safe_text(r))
            r.raise_for_status()

        if 500 <= r.status_code < 600:
            last_exc = httpx.HTTPStatusError(
                f"server {r.status_code}", request=r.request, response=r
            )
            if attempt < retries - 1:
                sleep_for = backoff**attempt
                logger.warning(
                    "server error %d on %s, retry %d/%d in %.1fs",
                    r.status_code, url, attempt + 1, retries, sleep_for,
                )
                time.sleep(sleep_for)
            continue

        r.raise_for_status()
        return r.json()
    if last_exc is not None:
        raise last_exc
    if last_429 is not None:
        raise httpx.HTTPStatusError(
            f"rate limited (429) after {retries} retries",
            request=last_429.request,
            response=last_429,
        )
    # Should be unreachable, but never surface a bare AssertionError.
    raise httpx.HTTPError(f"request to {url} failed after {retries} retries")


def get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    auth: Any = None,
    retries: int = 3,
    backoff: float = 2.0,
) -> Any:
    return _retry_request(
        lambda: client.get(url, params=params, headers=headers, auth=auth),
        url,
        retries=retries,
        backoff=backoff,
    )


def post_json(
    client: httpx.Client,
    url: str,
    *,
    data: dict | None = None,
    json: dict | None = None,
    headers: dict | None = None,
    auth: Any = None,
    retries: int = 3,
    backoff: float = 2.0,
) -> Any:
    return _retry_request(
        lambda: client.post(url, data=data, json=json, headers=headers, auth=auth),
        url,
        retries=retries,
        backoff=backoff,
    )
