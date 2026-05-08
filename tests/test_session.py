"""Tests for :mod:`pyisyox.helpers.session` — SSL context construction."""

from __future__ import annotations

import ssl

import pytest

from pyisyox.helpers.session import (
    TLSVersionError,
    build_sslcontext,
    can_https,
)

# --- build_sslcontext ----------------------------------------------------


def test_http_returns_none() -> None:
    assert build_sslcontext(use_https=False) is None
    assert build_sslcontext(use_https=False, tls_version=1.2) is None


def test_auto_pins_minimum_to_tlsv1_2() -> None:
    """Defence in depth: even though modern OpenSSL builds default to
    TLSv1_2 minimum, an older or custom-compiled OpenSSL could permit
    lower versions. Current eisy firmware rejects anything below 1.2,
    so make the floor explicit."""
    ctx = build_sslcontext(use_https=True, tls_version=None)
    assert ctx is not None
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    # No max pin in the auto path — let OpenSSL negotiate up to 1.3.
    # MAXIMUM_SUPPORTED is the "no cap" sentinel.
    assert ctx.maximum_version == ssl.TLSVersion.MAXIMUM_SUPPORTED


def test_pin_tlsv1_2() -> None:
    ctx = build_sslcontext(use_https=True, tls_version=1.2)
    assert ctx is not None
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2


def test_pin_tlsv1_3() -> None:
    ctx = build_sslcontext(use_https=True, tls_version=1.3)
    assert ctx is not None
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_3


@pytest.mark.parametrize("bad_version", [1.0, 1.1, 1.4, 2.0, "auto"])
def test_unsupported_tls_versions_raise(bad_version: object) -> None:
    """1.0/1.1 are rejected by current eisy firmware. We deliberately don't
    accept the PyISY-style ``"auto"`` string — pyisyox uses ``None``."""
    with pytest.raises(TLSVersionError, match="Unsupported TLS version"):
        build_sslcontext(use_https=True, tls_version=bad_version)  # type: ignore[arg-type]


def test_default_does_not_verify_certificate() -> None:
    """Out-of-the-box eisy ships a self-signed cert. Default config must
    accept it; consumers with a managed CA can opt into strict verification."""
    ctx = build_sslcontext(use_https=True, tls_version=None)
    assert ctx is not None
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_verify_ssl_true_enables_strict_verification() -> None:
    ctx = build_sslcontext(use_https=True, tls_version=None, verify_ssl=True)
    assert ctx is not None
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


# --- can_https -----------------------------------------------------------


def test_can_https_accepts_none_and_supported_versions() -> None:
    assert can_https(None) is True
    assert can_https(1.2) is True
    assert can_https(1.3) is True


@pytest.mark.parametrize("bad_version", [1.0, 1.1, 1.4])
def test_can_https_rejects_unsupported(bad_version: float) -> None:
    assert can_https(bad_version) is False
