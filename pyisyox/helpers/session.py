"""HTTP session and SSL context helpers for IoX 6+ controllers.

eisy/Polisy on current IoX firmware:

* Reject TLS 1.0 and 1.1 — confirmed against a current-firmware eisy
  with ``openssl s_client`` (TLS 1.0/1.1 → "no protocols available";
  TLS 1.2 and 1.3 negotiate).
* Ship a self-signed certificate. ``verify_ssl=False`` is the default
  so out-of-the-box deployments connect; consumers who deploy their
  own CA can opt into verification.

This module exposes two pure helpers that take discrete parameters
(no connection-info object) so they're trivial to call from the
:class:`pyisyox.controller.Controller` and from tests:

* :func:`build_sslcontext` — returns an :class:`ssl.SSLContext` (or
  ``None`` when the URL is HTTP-only) honouring ``tls_version`` and
  ``verify_ssl``.
* :func:`can_https` — preflight check that the requested TLS version
  is supported on this Python build.

Original ISY-994 hardware (TLS 1.1 only) is out of scope for this
library — that path stays on PyISY 3.x. ``tls_version=1.1`` here
raises rather than silently downgrading.
"""

from __future__ import annotations

import ssl

from pyisyox.logging import _LOGGER

_SUPPORTED_TLS_VERSIONS: tuple[float, ...] = (1.2, 1.3)


class TLSVersionError(ValueError):
    """Raised when the requested TLS version isn't usable on this build."""


def build_sslcontext(
    *,
    use_https: bool,
    tls_version: float | None = None,
    verify_ssl: bool = False,
) -> ssl.SSLContext | None:
    """Build an :class:`ssl.SSLContext` for the connection, or ``None``
    when the controller is reached over HTTP.

    Args:
        use_https: ``False`` short-circuits to ``None``.
        tls_version: ``None`` (default) auto-negotiates the highest
            mutually-supported version. ``1.2`` or ``1.3`` pin a
            specific minimum + maximum. Anything else raises.
        verify_ssl: ``False`` (default) accepts the controller's
            self-signed certificate. ``True`` enables strict
            verification — requires consumers to deploy their own CA.

    Raises:
        TLSVersionError: When ``tls_version`` isn't ``None`` / ``1.2``
            / ``1.3``.
    """
    if not use_https:
        return None

    if tls_version is None:
        # PROTOCOL_TLS_CLIENT auto-negotiates the highest mutually-
        # supported version. We also pin minimum_version = TLSv1_2
        # explicitly — modern OpenSSL builds default to that, but a
        # custom or older build could allow lower versions, and
        # current eisy firmware rejects anything below 1.2 anyway.
        # Defence in depth, mirroring PyISY/PyISY#499.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    elif tls_version == 1.2:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    elif tls_version == 1.3:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
    else:
        raise TLSVersionError(
            f"Unsupported TLS version {tls_version!r}; valid values are "
            f"{list(_SUPPORTED_TLS_VERSIONS)} or None for auto-negotiate"
        )

    if not verify_ssl:
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
    or ``1.3``). Returns ``True`` otherwise.
    """
    if tls_ver is None:
        return True
    if tls_ver not in _SUPPORTED_TLS_VERSIONS:
        _LOGGER.error(
            "Cannot use HTTPS with TLS %s: only %s are supported on IoX 6+. "
            "Set tls_version=None to auto-negotiate.",
            tls_ver,
            list(_SUPPORTED_TLS_VERSIONS),
        )
        return False
    return True
