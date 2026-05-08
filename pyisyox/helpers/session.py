"""HTTP session and SSL context helpers for IoX 6+ controllers.

eisy/Polisy on current IoX firmware:

* Reject TLS 1.0 and 1.1 — confirmed against a current-firmware eisy
  with ``openssl s_client`` (TLS 1.0/1.1 → "no protocols available";
  TLS 1.2 and 1.3 negotiate).
* Ship a self-signed certificate. ``verify_ssl=False`` is the default
  so out-of-the-box deployments connect; consumers who deploy their
  own CA can opt into verification.

This module exposes:

* :func:`get_new_client_session` — builds an aiohttp ClientSession
  with a cookie jar configured for IP-based hosts.
* :func:`get_sslcontext` — builds an :class:`ssl.SSLContext` honouring
  the connection-info ``tls_version`` (``None`` auto-negotiates 1.2 or
  1.3) and ``verify_ssl`` flags.
* :func:`can_https` — preflight check that the requested TLS version
  is supported on this Python build.

Original ISY-994 hardware (TLS 1.1 only) is out of scope for this
library — that path stays on PyISY 3.x. ``tls_version=1.1`` here
raises rather than silently downgrading.
"""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING

import aiohttp

from pyisyox.logging import _LOGGER

if TYPE_CHECKING:
    from pyisyox.connection import ISYConnectionInfo


_SUPPORTED_TLS_VERSIONS: tuple[float, ...] = (1.2, 1.3)


class TLSVersionError(ValueError):
    """Raised when the requested TLS version isn't usable on this build."""


def get_new_client_session(conn_info: ISYConnectionInfo) -> aiohttp.ClientSession:
    """Create a new aiohttp ClientSession suitable for an IoX controller.

    The cookie jar uses ``unsafe=True`` so cookies set on bare-IP
    hosts (typical LAN deployments) survive — aiohttp's default jar
    rejects them as a precaution that doesn't apply to a known LAN
    target.
    """
    if conn_info.use_https and not can_https(conn_info.tls_version):
        raise TLSVersionError("Cannot use HTTPS with the requested TLS version. See log for details.")
    if conn_info.use_https:
        return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    return aiohttp.ClientSession()


def get_sslcontext(conn_info: ISYConnectionInfo) -> ssl.SSLContext | None:
    """Build an :class:`ssl.SSLContext` for the connection, or ``None``
    when the controller is HTTP-only.

    ``tls_version=None`` (default) returns a context that lets OpenSSL
    pick the highest mutually-supported version (1.2 or 1.3 on current
    eisy firmware). Explicit values (``1.2`` or ``1.3``) pin a minimum
    so consumers can lock in a specific behavior — but TLS 1.0 / 1.1
    are deliberately rejected.

    ``verify_ssl=False`` (default) accepts the controller's self-signed
    certificate. Consumers who deploy their own CA can pass
    ``verify_ssl=True`` to enable strict verification.
    """
    if not conn_info.use_https:
        return None

    requested = conn_info.tls_version
    if requested is None:
        # PROTOCOL_TLS_CLIENT auto-negotiates the highest mutually-
        # supported version. Modern OpenSSL disables 1.0/1.1 by
        # default, matching what current eisy firmware accepts.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    elif requested == 1.2:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    elif requested == 1.3:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
    else:
        raise TLSVersionError(
            f"Unsupported TLS version {requested!r}; valid values are "
            f"{list(_SUPPORTED_TLS_VERSIONS)} or None for auto-negotiate"
        )

    verify = getattr(conn_info, "verify_ssl", False)
    if not verify:
        # Match the legacy default — eisy ships a self-signed cert.
        # PROTOCOL_TLS_CLIENT defaults to CERT_REQUIRED + check_hostname=True;
        # disabling both lets the connection succeed on out-of-the-box
        # deployments. Consumers managing their own CA can pass
        # verify_ssl=True for strict verification.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def can_https(tls_ver: float | None) -> bool:
    """Pre-flight check that HTTPS is usable with the requested TLS version.

    Returns ``False`` and logs an error when the version is one we
    don't support on IoX 6+ (anything other than ``None``, ``1.2``,
    or ``1.3``). Returns ``True`` otherwise — including when the
    Python build lacks a specific TLS-version constant, since
    ``PROTOCOL_TLS_CLIENT`` covers our needs without depending on
    deprecated module-level constants.
    """
    if tls_ver is None:
        return True
    if tls_ver not in _SUPPORTED_TLS_VERSIONS:
        _LOGGER.error(
            "PyISYoX cannot use HTTPS with TLS %s: only %s are supported on IoX 6+. "
            "Set tls_version=None to auto-negotiate.",
            tls_ver,
            list(_SUPPORTED_TLS_VERSIONS),
        )
        return False
    return True
