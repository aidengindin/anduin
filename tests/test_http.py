from __future__ import annotations

import httpx
import pytest

from anduin.http import get_json


class _StubResponse:
    def __init__(self, status_code, *, url="https://example.test/api", json_data=None):
        self.status_code = status_code
        self.headers = {"retry-after": "0"}
        self.request = httpx.Request("GET", url)
        self._json_data = json_data if json_data is not None else {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class _AlwaysRateLimitedClient:
    def get(self, *args, **kwargs):
        return _StubResponse(429)


class _RateLimitedThenOkClient:
    def __init__(self, fail_times, payload):
        self._remaining_failures = fail_times
        self._payload = payload

    def get(self, *args, **kwargs):
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            return _StubResponse(429)
        return _StubResponse(200, json_data=self._payload)


def test_sustained_429_raises_http_status_error_not_assertion():
    client = _AlwaysRateLimitedClient()
    with pytest.raises(httpx.HTTPStatusError):
        get_json(client, "https://example.test/api", retries=3, backoff=2.0)


def test_retry_then_success_returns_json():
    payload = {"ok": True, "value": 42}
    client = _RateLimitedThenOkClient(fail_times=2, payload=payload)
    result = get_json(client, "https://example.test/api", retries=3, backoff=2.0)
    assert result == payload
