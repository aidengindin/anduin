from __future__ import annotations

import logging

import httpx
import pytest

from anduin.http import get_json


class _StubResponse:
    def __init__(self, status_code, *, url="https://example.test/api", json_data=None, text=""):
        self.status_code = status_code
        self.headers = {"retry-after": "0"}
        self.request = httpx.Request("GET", url)
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.content = b"" if status_code == 204 else b"{}"

    def json(self):
        if not self.content:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
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


class _NoContentClient:
    def get(self, *args, **kwargs):
        return _StubResponse(204)


def test_204_no_content_returns_none():
    # Garmin's HRV endpoint answers 204 for a window with no readings; that must
    # not surface as a JSONDecodeError and fail the whole extractor run.
    assert get_json(_NoContentClient(), "https://example.test/api") is None


def test_empty_200_body_returns_none():
    response = httpx.Response(
        200, content=b"", request=httpx.Request("GET", "https://example.test/api")
    )

    class _Client:
        def get(self, *args, **kwargs):
            return response

    assert get_json(_Client(), "https://example.test/api") is None


def test_retry_then_success_returns_json():
    payload = {"ok": True, "value": 42}
    client = _RateLimitedThenOkClient(fail_times=2, payload=payload)
    result = get_json(client, "https://example.test/api", retries=3, backoff=2.0)
    assert result == payload


class _CountingClient:
    def __init__(self, status, text=""):
        self.calls = 0
        self._status = status
        self._text = text

    def get(self, *args, **kwargs):
        self.calls += 1
        return _StubResponse(self._status, text=self._text)


def test_client_error_fails_fast_without_retry():
    # A 400 is deterministic; retrying it wastes time. Must raise on the first try.
    client = _CountingClient(400, text='{"error": "bad"}')
    with pytest.raises(httpx.HTTPStatusError):
        get_json(client, "https://example.test/api", retries=3, backoff=2.0)
    assert client.calls == 1


def test_client_error_surfaces_response_body(caplog):
    client = _CountingClient(400, text="ACCOUNT_NOT_LINKED detail here")
    with caplog.at_level(logging.WARNING), pytest.raises(httpx.HTTPStatusError):
        get_json(client, "https://example.test/api", retries=3, backoff=2.0)
    assert "ACCOUNT_NOT_LINKED detail here" in caplog.text


def test_server_error_still_retries():
    # 5xx is transient; keep retrying up to the limit.
    client = _CountingClient(503)
    with pytest.raises(httpx.HTTPStatusError):
        get_json(client, "https://example.test/api", retries=3, backoff=2.0)
    assert client.calls == 3
