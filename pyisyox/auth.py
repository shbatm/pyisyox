"""Authentication strategies for IoX 6 controllers.

Two modes share a single :class:`Auth` protocol so the HTTP client can be
auth-agnostic:

* :class:`LocalAuth` — HTTP basic against ``:8443/rest/*`` with the local
  admin account. Offline by construction. Feature-degraded: no
  ``/api/triggers`` AST, no ``/api/variables`` names; must use the legacy
  XML ``/rest/nodes`` for structure. Recommended only when the user
  refuses to use a portal account.
* :class:`PortalAuth` — JWT bearer obtained from ``POST :443/api/login``.
  Auto-refreshes via ``POST :443/api/jwt/refresh``. Verified offline-safe
  on 2026-05-07: the eisy validates portal credentials and signs the JWT
  locally; no my.isy.io round-trip during steady-state. **Recommended
  default** — unlocks the modern ``/api/*`` JSON surface.

The :class:`PortalAuth` flow leaks the PG3 MQTT TLS keypair under
``data.ssl`` in the login response. :func:`redact_sensitive` (see
``pyisyox.redactor``) MUST be applied to any debug-level logging of the
response body.

Endpoint discovery and shape verification: ``POST /api/login`` body
``{"username": "<email>", "password": "<password>"}``; response
``{"successful": true, "data": {"accessToken": "<es256-jwt>",
"refreshToken": "<es256-jwt>", "ssl": {...}, ...}}``. Refresh body
``{"refreshToken": "<rt>"}`` returns the same data shape.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import aiohttp

if TYPE_CHECKING:
    pass


class AuthError(Exception):
    """Authentication failure (login rejected, refresh failed, etc.)."""


@dataclass(slots=True)
class TokenPair:
    """JWT access + refresh tokens with decoded expiry for proactive refresh.

    Attributes:
        access_token: Short-lived bearer token (default 1 h TTL).
        refresh_token: Long-lived token used to mint a new access token
            (default 30 d TTL).
        access_expires_at: Unix timestamp at which ``access_token``
            expires. Decoded from the JWT ``exp`` claim. ``0`` if the
            token couldn't be decoded.
        refresh_expires_at: Unix timestamp at which ``refresh_token``
            expires. ``0`` if undecoded.
    """

    access_token: str
    refresh_token: str
    access_expires_at: float = 0.0
    refresh_expires_at: float = 0.0

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> TokenPair:
        """Build a :class:`TokenPair` from a ``/api/login`` or
        ``/api/jwt/refresh`` response ``data`` dict."""
        access = data["accessToken"]
        refresh = data["refreshToken"]
        return cls(
            access_token=access,
            refresh_token=refresh,
            access_expires_at=_jwt_exp(access),
            refresh_expires_at=_jwt_exp(refresh),
        )

    def access_expires_within(self, seconds: float, now: float | None = None) -> bool:
        """True if the access token expires within ``seconds`` of ``now``.

        Used to trigger a proactive refresh before a request, avoiding the
        cost of one round-trip + one 401 + one refresh + one retry.
        """
        if self.access_expires_at <= 0:
            return False
        current = now if now is not None else time.time()
        return current + seconds >= self.access_expires_at


def _jwt_exp(token: str) -> float:
    """Extract the ``exp`` claim from a JWT, returning 0 if not parseable.

    No signature verification — we trust whatever the eisy issued. The exp
    claim is used solely for client-side proactive-refresh scheduling.
    """
    try:
        _, payload_b64, _ = token.split(".", 2)
    except ValueError:
        return 0.0
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return 0.0
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else 0.0


@runtime_checkable
class Auth(Protocol):
    """Auth strategy protocol shared by :class:`LocalAuth` and :class:`PortalAuth`.

    The HTTP client calls :meth:`authenticate` once during connect, then
    :meth:`request_kwargs` before every request to obtain the kwargs to
    splat into ``session.get(...)``/``session.post(...)``. On a 401
    response, the client calls :meth:`handle_unauthorized`; if it returns
    True, the client retries the original request once.
    """

    async def authenticate(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Perform any one-time authentication setup (e.g., login POST)."""
        ...

    async def request_kwargs(self, session: aiohttp.ClientSession, base_url: str) -> dict[str, Any]:
        """Return kwargs for ``session.request()`` (auth, headers, etc.)."""
        ...

    async def handle_unauthorized(self, session: aiohttp.ClientSession, base_url: str) -> bool:
        """Handle a 401 response. Return True if re-auth succeeded and the
        original request should be retried; False if the auth state cannot
        recover (caller should propagate the 401 as a permanent error)."""
        ...

    async def close(self) -> None:
        """Release any auth-held resources (e.g., explicit logout)."""
        ...


class LocalAuth:
    """HTTP basic auth against ``:8443/rest/*`` with the local admin account.

    No login round-trip is needed — credentials are passed on every request.
    A 401 on this path means the credentials are wrong, so re-auth cannot
    recover.
    """

    def __init__(self, username: str, password: str) -> None:
        """Store the local admin credentials.

        Args:
            username: Local admin username (typically ``"admin"``).
            password: Local admin password.
        """
        self._auth = aiohttp.BasicAuth(username, password)

    async def authenticate(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """No-op — basic auth attaches per request."""

    async def request_kwargs(self, session: aiohttp.ClientSession, base_url: str) -> dict[str, Any]:
        """Return kwargs that attach HTTP basic auth."""
        return {"auth": self._auth}

    async def handle_unauthorized(self, session: aiohttp.ClientSession, base_url: str) -> bool:
        """Cannot recover from 401 with basic auth — credentials are wrong."""
        return False

    async def close(self) -> None:
        """No-op — no server-side session to tear down."""


class PortalAuth:
    """JWT bearer auth from ``POST :443/api/login``.

    Maintains an in-memory :class:`TokenPair` with proactive refresh. On
    401, attempts one refresh; if refresh fails (or has expired), falls
    back to a fresh login.

    Login URL: ``{base_url}/api/login``. Refresh URL:
    ``{base_url}/api/jwt/refresh``. Logout URL (optional, on
    :meth:`close`): ``{base_url}/api/logout``.
    """

    LOGIN_PATH = "/api/login"
    REFRESH_PATH = "/api/jwt/refresh"
    LOGOUT_PATH = "/api/logout"

    #: Number of seconds before access-token expiry at which we proactively refresh.
    PROACTIVE_REFRESH_LEEWAY = 60.0

    def __init__(self, email: str, password: str) -> None:
        """Store the portal credentials. No network calls happen here.

        Args:
            email: Portal email address. ``:443/api/login`` rejects
                non-email usernames at the request-validation layer
                (verified 2026-05-07).
            password: Portal password.
        """
        self._email = email
        self._password = password
        self._tokens: TokenPair | None = None

    @property
    def tokens(self) -> TokenPair | None:
        """Currently held tokens, or ``None`` if not yet authenticated.

        Tests use this to assert state without forcing a real network round-trip.
        """
        return self._tokens

    async def authenticate(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Perform ``POST /api/login`` and store the returned token pair.

        Raises:
            AuthError: When the login response is not ``successful: true``
                or lacks tokens.
        """
        await self._login(session, base_url)

    async def request_kwargs(self, session: aiohttp.ClientSession, base_url: str) -> dict[str, Any]:
        """Return ``Authorization: Bearer <accessToken>`` headers.

        Refreshes the token proactively if it expires within
        :attr:`PROACTIVE_REFRESH_LEEWAY` seconds, avoiding the cost of an
        in-flight 401 + refresh + retry round.
        """
        if self._tokens is None:
            raise AuthError("PortalAuth.request_kwargs called before authenticate()")
        if self._tokens.access_expires_within(self.PROACTIVE_REFRESH_LEEWAY):
            await self._refresh_or_relogin(session, base_url)
        return {"headers": {"Authorization": f"Bearer {self._tokens.access_token}"}}

    async def handle_unauthorized(self, session: aiohttp.ClientSession, base_url: str) -> bool:
        """Handle 401: try refresh, then re-login.

        Returns True if re-auth succeeded and the caller should retry the
        original request; False if both refresh and login failed.
        """
        try:
            await self._refresh_or_relogin(session, base_url)
        except AuthError:
            return False
        return True

    async def close(self) -> None:
        """Forget the in-memory tokens. Does not call ``/api/logout`` —
        a stale refresh token expiring naturally is harmless, and an
        explicit logout would require the session+base_url which the
        protocol doesn't pass to ``close()``.
        """
        self._tokens = None

    async def _refresh_or_relogin(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Try refresh first; on failure (refresh expired/rejected), re-login."""
        refreshed = False
        if self._tokens is not None:
            try:
                await self._refresh(session, base_url)
            except AuthError:
                # Refresh failed — fall through to a full login.
                self._tokens = None
            else:
                refreshed = True
        if not refreshed:
            await self._login(session, base_url)

    async def _login(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """``POST /api/login`` — exchange credentials for a token pair."""
        body = {"username": self._email, "password": self._password}
        url = f"{base_url.rstrip('/')}{self.LOGIN_PATH}"
        async with session.post(url, json=body) as resp:
            if resp.status != 200:
                raise AuthError(f"login failed: HTTP {resp.status}")
            data = await resp.json()
        self._tokens = self._tokens_from_response(data, "login")

    async def _refresh(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """``POST /api/jwt/refresh`` — mint a fresh access+refresh pair."""
        if self._tokens is None:
            raise AuthError("cannot refresh without an existing refresh token")
        body = {"refreshToken": self._tokens.refresh_token}
        url = f"{base_url.rstrip('/')}{self.REFRESH_PATH}"
        async with session.post(url, json=body) as resp:
            if resp.status != 200:
                raise AuthError(f"token refresh failed: HTTP {resp.status}")
            data = await resp.json()
        self._tokens = self._tokens_from_response(data, "refresh")

    @staticmethod
    def _tokens_from_response(payload: dict[str, Any], op: str) -> TokenPair:
        """Validate and unwrap the ``data`` envelope from login/refresh responses."""
        if not payload.get("successful"):
            raise AuthError(f"{op} response was not successful")
        data = payload.get("data")
        if not isinstance(data, dict) or "accessToken" not in data or "refreshToken" not in data:
            raise AuthError(f"{op} response missing accessToken/refreshToken")
        return TokenPair.from_response(data)
