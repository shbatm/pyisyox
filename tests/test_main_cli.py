"""Tests for the ``python -m pyisyox`` smoke-test CLI.

The CLI is small, but it's the only place the auth-mode picker lives
(email username → ``PortalAuth``; otherwise → ``LocalAuth``). It also
exercises the ``Controller.connect`` failure path, which the rest of
the suite reaches only through synthetic FakeSession scaffolding.
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import pytest

from pyisyox import LocalAuth, PortalAuth
from pyisyox.__main__ import _build_auth, main, parse_args

# --- _build_auth: auth-mode picker ----------------------------------------


def test_build_auth_picks_portal_for_email_username() -> None:
    auth = _build_auth("https://eisy.local:443", "you@example.invalid", "pw")
    assert isinstance(auth, PortalAuth)


def test_build_auth_picks_local_for_admin_username() -> None:
    auth = _build_auth("https://eisy.local:8443", "admin", "pw")
    assert isinstance(auth, LocalAuth)


@pytest.mark.parametrize(
    ("username", "expected_cls"),
    [
        ("user@example.invalid", PortalAuth),
        ("admin", LocalAuth),
        ("operator", LocalAuth),
        ("a+b@c.invalid", PortalAuth),
    ],
)
def test_build_auth_matrix(username: str, expected_cls: type) -> None:
    """The picker keys solely on the presence of ``@`` in the username."""
    assert isinstance(_build_auth("https://x", username, "pw"), expected_cls)


# --- parse_args: argparse surface ----------------------------------------


def test_parse_args_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["pyisyox", "https://eisy.local:443", "u", "p"])
    ns = parse_args()
    assert ns.url == "https://eisy.local:443"
    assert ns.username == "u"
    assert ns.password == "p"
    assert ns.events is True  # default: events on
    assert ns.verbose is False
    assert ns.tls_version is None
    assert ns.verify_ssl is False


def test_parse_args_no_events_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["pyisyox", "https://x", "u", "p", "--no-events"])
    assert parse_args().events is False


def test_parse_args_tls_version_constrained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only 1.2 / 1.3 are valid (TLS 1.0/1.1 dropped per scope memo)."""
    monkeypatch.setattr("sys.argv", ["pyisyox", "https://x", "u", "p", "--tls-version", "1.0"])
    with pytest.raises(SystemExit):
        parse_args()


def test_parse_args_tls_version_accepts_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["pyisyox", "https://x", "u", "p", "--tls-version", "1.3"])
    assert parse_args().tls_version == 1.3


def test_parse_args_verify_ssl_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["pyisyox", "https://x", "u", "p", "--verify-ssl"])
    assert parse_args().verify_ssl is True


# --- main(): orchestration ------------------------------------------------


def _ns(**overrides) -> argparse.Namespace:
    base = {
        "url": "https://eisy.local:443",
        "username": "user@example.invalid",
        "password": "pw",
        "verbose": False,
        "events": False,  # don't loop forever
        "tls_version": None,
        "verify_ssl": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def fake_controller():
    """Patch :class:`pyisyox.Controller` for the CLI module so we
    don't open a real connection. Yields the patched class so tests
    can configure side effects."""
    with patch("pyisyox.__main__.Controller") as cls:
        instance = cls.return_value
        instance.connect = AsyncMock()
        instance.stop = AsyncMock()
        instance.config = type("C", (), {"uuid": "TEST-UUID", "version": "6.0.0"})
        instance.nodes = {
            "AB CD EF 1": type(
                "N",
                (),
                {
                    "name": "Test Node",
                    "nodedef_id": "KeypadDimmer",
                    "properties": {"ST": type("P", (), {"formatted": "On", "value": "100"})()},
                },
            )()
        }
        yield cls


async def test_main_happy_path(fake_controller) -> None:
    """Connect succeeds, summary is logged, stop is awaited, exit 0."""
    rc = await main(_ns())
    assert rc == 0
    fake_controller.return_value.connect.assert_awaited_once_with(start_websocket=False)
    fake_controller.return_value.stop.assert_awaited_once()


async def test_main_returns_1_on_connect_failure(fake_controller) -> None:
    """Exception in ``connect()`` must trigger ``stop()`` (cleanup)
    and a non-zero exit code, *not* a propagating exception — the
    CLI is the outermost frame."""
    fake_controller.return_value.connect.side_effect = RuntimeError("boom")
    rc = await main(_ns())
    assert rc == 1
    fake_controller.return_value.stop.assert_awaited_once()


async def test_main_passes_tls_and_verify_ssl(fake_controller) -> None:
    await main(_ns(tls_version=1.3, verify_ssl=True))
    _, kwargs = fake_controller.call_args
    assert kwargs["tls_version"] == 1.3
    assert kwargs["verify_ssl"] is True
