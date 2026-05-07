"""Tests for PortalAuth and LocalAuth.

Uses a hand-rolled fake aiohttp ClientSession that records POST calls and
returns scripted responses. Avoids aresponses/aioresponses to keep the
test suite zero-dep beyond pytest itself, mirroring phase 1/2 fixtures.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import aiohttp
import pytest

from pyisyox.auth import Auth, AuthError, LocalAuth, PortalAuth, TokenPair, _jwt_exp

# --- helpers --------------------------------------------------------------


def _make_jwt(exp: float, kind: str = "at+jwt") -> str:
    """Forge a minimal ES256-shaped JWT with the given ``exp`` claim.

    Signature is bogus — we never verify, so it doesn't matter for tests.
    """
    header = {"alg": "ES256", "typ": kind}
    payload = {"exp": exp, "iss": "eisy"}

    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64(header)}.{b64(payload)}.signature"


class _FakeResponse:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self._body = body or {}

    async def json(self) -> dict[str, Any]:
        # Yield once so concurrent coroutines actually interleave —
        # without this, asyncio.gather runs each task to completion
        # before the next starts, masking concurrency-correctness bugs.
        await asyncio.sleep(0)
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Records POST calls and returns scripted responses in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses: list[_FakeResponse] = []

    def queue(self, status: int, body: dict[str, Any] | None = None) -> None:
        self._responses.append(_FakeResponse(status, body))

    def post(self, url: str, *, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.calls.append((url, json or {}))
        if not self._responses:
            raise RuntimeError(f"no scripted response for POST {url}")
        return self._responses.pop(0)


# --- TokenPair / _jwt_exp ------------------------------------------------


def test_jwt_exp_extracts_claim() -> None:
    in_one_hour = time.time() + 3600
    token = _make_jwt(in_one_hour)
    assert _jwt_exp(token) == pytest.approx(in_one_hour, abs=1)


def test_jwt_exp_returns_zero_on_garbage() -> None:
    assert _jwt_exp("not-a-jwt") == 0.0
    assert _jwt_exp("a.b.c") == 0.0


def test_jwt_exp_warns_on_undecodable_token(caplog: pytest.LogCaptureFixture) -> None:
    """Returning 0 silently would let proactive refresh fail forever; we
    must emit a warning so operators see when the eisy's token format
    drifts away from what we expect."""
    with caplog.at_level(logging.WARNING, logger="pyisyox.auth"):
        _jwt_exp("not-a-jwt")
    assert any("three dot-separated segments" in rec.message for rec in caplog.records)


def test_jwt_exp_warns_on_payload_without_exp(caplog: pytest.LogCaptureFixture) -> None:
    """A well-formed JWT lacking the 'exp' claim is still 'expiry unknown'.
    Warn and keep returning 0 (skip-proactive sentinel)."""

    def _b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    no_exp_token = f"{_b64({'alg': 'ES256'})}.{_b64({'iss': 'eisy'})}.sig"
    with caplog.at_level(logging.WARNING, logger="pyisyox.auth"):
        result = _jwt_exp(no_exp_token)
    assert result == 0.0
    assert any("missing numeric 'exp'" in rec.message for rec in caplog.records)


def test_token_pair_from_response_decodes_expiry() -> None:
    in_one_hour = time.time() + 3600
    in_thirty_days = time.time() + 30 * 86400
    pair = TokenPair.from_response(
        {
            "accessToken": _make_jwt(in_one_hour),
            "refreshToken": _make_jwt(in_thirty_days, kind="rt+jwt"),
        }
    )
    assert pair.access_expires_at == pytest.approx(in_one_hour, abs=1)
    assert pair.refresh_expires_at == pytest.approx(in_thirty_days, abs=1)


def test_access_expires_within() -> None:
    in_one_hour = time.time() + 3600
    pair = TokenPair(
        access_token="x",
        refresh_token="y",
        access_expires_at=in_one_hour,
    )
    assert not pair.access_expires_within(60)
    assert pair.access_expires_within(3600 + 1)


# --- LocalAuth ------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_auth_attaches_basic() -> None:
    auth = LocalAuth("admin", "hunter2")
    sess = FakeSession()
    await auth.authenticate(sess, "https://eisy.local:8443")  # no-op
    kwargs = await auth.request_kwargs(sess, "https://eisy.local:8443")
    assert isinstance(kwargs["auth"], aiohttp.BasicAuth)
    assert kwargs["auth"].login == "admin"
    assert kwargs["auth"].password == "hunter2"


@pytest.mark.asyncio
async def test_local_auth_cannot_recover_from_401() -> None:
    auth = LocalAuth("admin", "wrong")
    assert (await auth.handle_unauthorized(FakeSession(), "https://x")) is False


def test_local_auth_implements_protocol() -> None:
    assert isinstance(LocalAuth("u", "p"), Auth)


# --- PortalAuth -----------------------------------------------------------


def _login_body(access_exp: float, refresh_exp: float, with_ssl: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "accessToken": _make_jwt(access_exp),
        "refreshToken": _make_jwt(refresh_exp, kind="rt+jwt"),
    }
    if with_ssl:
        data["ssl"] = {"key": "PRIVATE", "cert": "CERT", "ca": "CA"}
    return {"successful": True, "data": data}


@pytest.mark.asyncio
async def test_portal_auth_login_stores_tokens() -> None:
    auth = PortalAuth("user@example.com", "pass")
    sess = FakeSession()
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))

    await auth.authenticate(sess, "https://eisy.local")

    assert auth.tokens is not None
    assert auth.tokens.access_token.count(".") == 2
    url, body = sess.calls[0]
    assert url == "https://eisy.local/api/login"
    assert body == {"username": "user@example.com", "password": "pass"}


@pytest.mark.asyncio
async def test_portal_auth_request_kwargs_attaches_bearer() -> None:
    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    await auth.authenticate(sess, "https://eisy")

    kwargs = await auth.request_kwargs(sess, "https://eisy")

    auth_header = kwargs["headers"]["Authorization"]
    assert auth_header.startswith("Bearer ")
    assert auth.tokens is not None
    assert auth_header[len("Bearer ") :] == auth.tokens.access_token


@pytest.mark.asyncio
async def test_portal_auth_request_kwargs_before_login_raises() -> None:
    auth = PortalAuth("u@x", "p")
    with pytest.raises(AuthError, match="before authenticate"):
        await auth.request_kwargs(FakeSession(), "https://eisy")


@pytest.mark.asyncio
async def test_portal_auth_proactive_refresh_within_leeway() -> None:
    """If the access token expires within PROACTIVE_REFRESH_LEEWAY seconds,
    request_kwargs triggers a refresh before returning headers."""
    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    # First login: access token expires in 30 s (under the 60 s leeway).
    sess.queue(200, _login_body(time.time() + 30, time.time() + 30 * 86400))
    # Refresh: fresh tokens with comfortable expiry.
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))

    await auth.authenticate(sess, "https://eisy")
    kwargs = await auth.request_kwargs(sess, "https://eisy")

    assert len(sess.calls) == 2  # login + refresh
    assert sess.calls[1][0] == "https://eisy/api/jwt/refresh"
    # The bearer header reflects the post-refresh access token.
    assert auth.tokens is not None
    assert kwargs["headers"]["Authorization"].endswith(auth.tokens.access_token)


@pytest.mark.asyncio
async def test_portal_auth_handle_unauthorized_refreshes() -> None:
    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    await auth.authenticate(sess, "https://eisy")

    retry = await auth.handle_unauthorized(sess, "https://eisy")
    assert retry is True
    assert sess.calls[-1][0] == "https://eisy/api/jwt/refresh"


@pytest.mark.asyncio
async def test_portal_auth_handle_unauthorized_falls_back_to_relogin() -> None:
    """If the refresh token is rejected (e.g., expired), PortalAuth falls back
    to a fresh /api/login."""
    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    sess.queue(401)  # refresh rejected
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    await auth.authenticate(sess, "https://eisy")

    retry = await auth.handle_unauthorized(sess, "https://eisy")
    assert retry is True
    paths = [url.rsplit("/", 1)[-1] for url, _ in sess.calls]
    assert paths == ["login", "refresh", "login"]


@pytest.mark.asyncio
async def test_portal_auth_handle_unauthorized_returns_false_when_relogin_fails() -> None:
    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    sess.queue(401)  # refresh rejected
    sess.queue(401)  # re-login also rejected
    await auth.authenticate(sess, "https://eisy")

    assert (await auth.handle_unauthorized(sess, "https://eisy")) is False


@pytest.mark.asyncio
async def test_portal_auth_login_response_envelope_validation() -> None:
    """Reject responses lacking ``successful: true`` or the token fields."""
    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(200, {"successful": False, "data": {}})
    with pytest.raises(AuthError, match="not successful"):
        await auth.authenticate(sess, "https://eisy")

    sess.queue(200, {"successful": True, "data": {"accessToken": "x"}})  # missing refresh
    with pytest.raises(AuthError, match="missing accessToken"):
        await auth.authenticate(sess, "https://eisy")


@pytest.mark.asyncio
async def test_portal_auth_login_http_failure() -> None:
    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(401)
    with pytest.raises(AuthError, match="login failed"):
        await auth.authenticate(sess, "https://eisy")


@pytest.mark.asyncio
async def test_portal_auth_close_clears_tokens() -> None:
    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    await auth.authenticate(sess, "https://eisy")
    assert auth.tokens is not None
    await auth.close()
    assert auth.tokens is None


def test_portal_auth_implements_protocol() -> None:
    assert isinstance(PortalAuth("u@x", "p"), Auth)


# --- concurrency ----------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_auth_concurrent_authenticate_collapses_to_one_login() -> None:
    """Two coroutines both call authenticate() before either completes;
    the lock + post-acquire re-check ensures only one POST /api/login fires."""

    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))

    await asyncio.gather(
        auth.authenticate(sess, "https://eisy"),
        auth.authenticate(sess, "https://eisy"),
        auth.authenticate(sess, "https://eisy"),
    )

    login_calls = [c for c in sess.calls if c[0].endswith("/api/login")]
    assert len(login_calls) == 1, "concurrent authenticate() must collapse to one login"


@pytest.mark.asyncio
async def test_portal_auth_concurrent_proactive_refresh_collapses() -> None:
    """N coroutines all see an expiring token in request_kwargs at the same
    time. Only one /api/jwt/refresh round-trip should fire; the runners-up
    re-check inside the lock and skip."""

    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    # Initial login: token expires in 30 s (under 60 s leeway).
    sess.queue(200, _login_body(time.time() + 30, time.time() + 30 * 86400))
    # Single refresh queued — if more than one fires the test fails with
    # "no scripted response".
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    await auth.authenticate(sess, "https://eisy")

    results = await asyncio.gather(
        auth.request_kwargs(sess, "https://eisy"),
        auth.request_kwargs(sess, "https://eisy"),
        auth.request_kwargs(sess, "https://eisy"),
    )

    refresh_calls = [c for c in sess.calls if c[0].endswith("/api/jwt/refresh")]
    assert len(refresh_calls) == 1, "concurrent proactive refresh must collapse to one POST"
    # All callers see the same fresh access token in their headers.
    headers = [r["headers"]["Authorization"] for r in results]
    assert len(set(headers)) == 1


@pytest.mark.asyncio
async def test_portal_auth_concurrent_handle_unauthorized_collapses() -> None:
    """Two parallel requests both get 401 and call handle_unauthorized.
    The first refreshes; the second observes a different access token at
    lock-acquire time and no-ops."""

    auth = PortalAuth("u@x", "p")
    sess = FakeSession()
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    sess.queue(200, _login_body(time.time() + 3600, time.time() + 30 * 86400))
    await auth.authenticate(sess, "https://eisy")

    results = await asyncio.gather(
        auth.handle_unauthorized(sess, "https://eisy"),
        auth.handle_unauthorized(sess, "https://eisy"),
        auth.handle_unauthorized(sess, "https://eisy"),
    )
    assert all(results), "all callers should report retry-OK"
    refresh_calls = [c for c in sess.calls if c[0].endswith("/api/jwt/refresh")]
    assert len(refresh_calls) == 1, "concurrent 401s must share a single refresh"
