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

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

_LOGGER = logging.getLogger(__name__)


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

    A return value of ``0`` is a sentinel meaning "expiry unknown";
    :meth:`TokenPair.access_expires_within` treats this as
    "skip proactive refresh", so reactive 401-handling is the only
    recovery path until the operator notices the warning logged here
    and updates the parser. Forcing immediate refresh on undecodable
    tokens would loop endlessly if the eisy persisted in returning
    malformed JWTs.
    """
    try:
        _, payload_b64, _ = token.split(".", 2)
    except ValueError:
        _LOGGER.warning(
            "JWT does not have three dot-separated segments — skipping proactive "
            "refresh; reactive 401 handling will still recover from server-side expiry."
        )
        return 0.0
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError) as exc:
        _LOGGER.warning(
            "JWT payload not decodable as base64-url JSON (%s) — skipping proactive refresh",
            exc,
        )
        return 0.0
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        _LOGGER.warning(
            "JWT payload missing numeric 'exp' claim (got %r) — skipping proactive refresh",
            exp,
        )
        return 0.0
    return float(exp)


class Auth(Protocol):
    """Auth strategy protocol shared by :class:`LocalAuth` and :class:`PortalAuth`.

    Not ``@runtime_checkable`` — ``isinstance(x, Auth)`` against an
    unrelated class that happens to share these attribute names would
    pass without verifying coroutine signatures, which masks bugs.
    Tests construct the concrete classes directly.

    The HTTP client calls :meth:`authenticate` once during connect, then
    :meth:`request_kwargs` before every request to obtain the kwargs to
    splat into ``session.get(...)``/``session.post(...)``. On a 401
    response, the client calls :meth:`handle_unauthorized`; if it returns
    True, the client retries the original request once.
    """

    async def authenticate(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Perform any one-time authentication setup (e.g., login POST)."""

    async def request_kwargs(self, session: aiohttp.ClientSession, base_url: str) -> dict[str, Any]:
        """Return kwargs for ``session.request()`` (auth, headers, etc.)."""
        return {}

    async def handle_unauthorized(self, session: aiohttp.ClientSession, base_url: str) -> bool:
        """Handle a 401 response. Return True if re-auth succeeded and the
        original request should be retried; False if the auth state cannot
        recover (caller should propagate the 401 as a permanent error)."""
        return False

    async def close(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Release any auth-held resources (e.g., explicit logout).

        ``session`` and ``base_url`` are passed so implementations can
        make a final logout call to invalidate server-side state
        (PortalAuth posts ``/api/logout``); LocalAuth ignores them
        since basic-auth has no server-side session.
        """


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

    async def close(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """No-op — basic auth has no server-side session to tear down."""


class PortalAuth:
    """JWT bearer auth from ``POST :443/api/login``.

    Maintains an in-memory :class:`TokenPair` with proactive refresh. On
    401, attempts one refresh; if refresh fails (or has expired), falls
    back to a fresh login.

    Login URL: ``{base_url}/api/login``. Refresh URL:
    ``{base_url}/api/jwt/refresh``. Logout URL (optional, on
    :meth:`close`): ``{base_url}/api/jwt/logout``. Verified against
    eisy 1.0.3 — ``POST /api/jwt/logout`` returns ``200`` with
    ``{"successful": true, "data": null}``. (Pre-2026-05-12 versions
    of this module used ``/api/logout``, which 404s.)
    """

    LOGIN_PATH = "/api/login"
    REFRESH_PATH = "/api/jwt/refresh"
    LOGOUT_PATH = "/api/jwt/logout"

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
        # Serialises _login / _refresh / _refresh_or_relogin so concurrent
        # consumers (e.g. a background poll racing a user command inside
        # the proactive-refresh leeway window) collapse onto a single
        # network round-trip. Each entry re-checks the cached token state
        # after acquiring the lock, so the second waiter no-ops.
        self._auth_lock: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        """Lazy-construct the lock so the constructor stays event-loop-free.

        Lock creation needs a running event loop; the constructor is
        called synchronously (often before the loop exists) so we defer
        construction to the first ``async`` entry point.
        """
        if self._auth_lock is None:
            self._auth_lock = asyncio.Lock()
        return self._auth_lock

    @property
    def tokens(self) -> TokenPair | None:
        """Currently held tokens, or ``None`` if not yet authenticated.

        Tests use this to assert state without forcing a real network round-trip.
        """
        return self._tokens

    async def authenticate(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Perform ``POST /api/login`` and store the returned token pair.

        Concurrent calls collapse onto a single login round-trip via the
        instance lock; the second caller observes the tokens already set
        and returns without making a network request.

        Raises:
            AuthError: When the login response is not ``successful: true``
                or lacks tokens.
        """
        async with self._lock():
            if self._tokens is not None:
                return
            await self._login_locked(session, base_url)

    async def request_kwargs(self, session: aiohttp.ClientSession, base_url: str) -> dict[str, Any]:
        """Return ``Authorization: Bearer <accessToken>`` headers.

        Refreshes the token proactively if it expires within
        :attr:`PROACTIVE_REFRESH_LEEWAY` seconds, avoiding the cost of an
        in-flight 401 + refresh + retry round. Concurrent callers that
        observe an expiring token both queue on the auth lock; the
        winner refreshes once, the runners-up re-check and skip.
        """
        if self._tokens is None:
            raise AuthError("PortalAuth.request_kwargs called before authenticate()")
        if self._tokens.access_expires_within(self.PROACTIVE_REFRESH_LEEWAY):
            await self._refresh_or_relogin(session, base_url)
        # Re-read after the lock — another coroutine may have refreshed.
        if self._tokens is None:  # pragma: no cover — defensive; refresh sets tokens or raises
            raise AuthError("tokens disappeared during refresh")
        return {"headers": {"Authorization": f"Bearer {self._tokens.access_token}"}}

    async def handle_unauthorized(self, session: aiohttp.ClientSession, base_url: str) -> bool:
        """Handle 401: try refresh, then re-login.

        Concurrent 401s from in-flight requests all enter
        ``_refresh_or_relogin``; the first runs the refresh, subsequent
        callers re-check the cached token (which has just been updated)
        and skip the network round-trip. Returns True if re-auth
        succeeded and the caller should retry the original request;
        False if both refresh and login failed.
        """
        token_at_call = self._tokens.access_token if self._tokens else None
        try:
            await self._refresh_or_relogin(session, base_url, observed_token=token_at_call)
        except AuthError:
            return False
        return True

    async def close(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Best-effort logout against ``POST /api/jwt/logout``, then clear
        the in-memory tokens.

        If we don't tell the eisy we're done, the refresh token stays
        live for its full 30-day TTL — useful only to attackers who
        somehow obtain it. The logout call is best-effort: any error
        (network down, controller already gone, stale token) is logged
        at debug level and swallowed. The local token state is cleared
        regardless so the consumer can construct a fresh
        :class:`PortalAuth` and re-authenticate.
        """
        if self._tokens is not None:
            await self._logout(session, base_url)
        self._tokens = None

    async def _logout(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Post ``POST /api/jwt/logout`` with a *live* bearer token.

        The access token is the credential the eisy authenticates the
        logout call with. After a long WebSocket-only session it's
        usually expired — proactive refresh only fires on REST calls
        (:meth:`request_kwargs`), and an event-stream-driven consumer
        makes almost none after init. Posting an expired bearer to
        ``/api/jwt/logout`` is what produces the intermittent ``HTTP
        404`` / ``HTTP 500`` responses seen at shutdown (the firmware's
        answer for "no such session" varies). So refresh first if the
        token is at/near expiry; the refresh rotates the refresh token
        too, so logging out with the new access token still invalidates
        the current pair. If the refresh itself fails the refresh token
        is already dead or the controller is unreachable — there's
        nothing left to invalidate, so skip the logout call.
        """
        if self._tokens is not None and self._tokens.access_expires_within(self.PROACTIVE_REFRESH_LEEWAY):
            async with self._lock():
                if self._tokens is not None and self._tokens.access_expires_within(
                    self.PROACTIVE_REFRESH_LEEWAY
                ):
                    try:
                        await self._refresh_locked(session, base_url)
                    except AuthError as exc:
                        _LOGGER.debug("pre-logout token refresh failed (%s); skipping logout", exc)
                        return
        if self._tokens is None:  # pragma: no cover — defensive; guarded by close()
            return
        url = f"{base_url.rstrip('/')}{self.LOGOUT_PATH}"
        headers = {"Authorization": f"Bearer {self._tokens.access_token}"}
        try:
            async with session.post(url, headers=headers) as resp:
                _LOGGER.debug("logout response: HTTP %s", resp.status)
        except Exception as exc:  # pylint: disable=broad-except
            # Network errors during logout are not fatal — the token
            # will expire naturally even if we couldn't tell the eisy.
            _LOGGER.debug("logout call failed (%s); token will expire naturally", exc)

    async def _refresh_or_relogin(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        *,
        observed_token: str | None = None,
    ) -> None:
        """Refresh the access token (or re-login on failure), serialised.

        ``observed_token`` is the access token the caller saw when it
        decided a refresh was needed. After acquiring the lock we
        compare against the current token; if it has changed, another
        coroutine already refreshed on our behalf and we no-op. This
        collapses concurrent 401s and concurrent
        proactive-refresh-window misses onto a single round-trip.
        """
        async with self._lock():
            current = self._tokens.access_token if self._tokens else None
            if observed_token is not None and current != observed_token:
                # Another waiter completed the refresh while we were queued.
                return
            if (
                observed_token is None
                and self._tokens is not None
                and not self._tokens.access_expires_within(self.PROACTIVE_REFRESH_LEEWAY)
            ):
                # Proactive path: while we waited for the lock, the
                # winner refreshed and the new token has comfortable
                # life left. Skip.
                return

            refreshed = False
            if self._tokens is not None:
                try:
                    await self._refresh_locked(session, base_url)
                except AuthError:
                    # Refresh failed — fall through to a full login.
                    self._tokens = None
                else:
                    refreshed = True
            if not refreshed:
                await self._login_locked(session, base_url)

    async def _login_locked(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """``POST /api/login`` — caller must hold ``_auth_lock``."""
        body = {"username": self._email, "password": self._password}
        url = f"{base_url.rstrip('/')}{self.LOGIN_PATH}"
        async with session.post(url, json=body) as resp:
            if resp.status != 200:
                raise AuthError(f"login failed: HTTP {resp.status}")
            data = await resp.json()
        self._tokens = self._tokens_from_response(data, "login")

    async def _refresh_locked(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """``POST /api/jwt/refresh`` — caller must hold ``_auth_lock``."""
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
