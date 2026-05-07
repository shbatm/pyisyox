"""Tests for PortalAuth and LocalAuth.

Uses a hand-rolled fake aiohttp ClientSession that records POST calls and
returns scripted responses. Avoids aresponses/aioresponses to keep the
test suite zero-dep beyond pytest itself, mirroring phase 1/2 fixtures.
"""

from __future__ import annotations

import base64
import json
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
        return self._body

    async def __aenter__(self) -> _FakeResponse:
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
