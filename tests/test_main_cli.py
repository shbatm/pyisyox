"""Tests for the ``python -m pyisyox`` smoke-test CLI.

The CLI is small, but it's the only place the auth-mode picker lives
(email username → ``PortalAuth``; otherwise → ``LocalAuth``). It also
exercises the ``Controller.connect`` failure path, which the rest of
the suite reaches only through synthetic FakeSession scaffolding.
"""

from __future__ import annotations

import argparse
import json as _json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyisyox import LocalAuth, PortalAuth
from pyisyox.__main__ import _build_auth, _resolve_log_level, main, parse_args, run
from pyisyox.logging import LOG_VERBOSE

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
        "dump": None,
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


# --- --dump: controller snapshot to JSON ----------------------------------


def test_parse_args_dump_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dump`` is optional; absent means no snapshot."""
    monkeypatch.setattr("sys.argv", ["pyisyox", "https://x", "u", "p"])
    assert parse_args().dump is None


def test_parse_args_dump_captures_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv", ["pyisyox", "https://x", "u", "p", "--dump", "./snap.json"]
    )
    assert parse_args().dump == "./snap.json"


async def test_main_dump_writes_controller_snapshot(
    fake_controller, tmp_path
) -> None:
    """``--dump <path>`` writes ``Controller.to_dict()`` as pretty
    JSON to ``path``. Parent dirs auto-create so the user can pass a
    nested path (mirrors the v1-beta dumper)."""
    snapshot = {"config": {"uuid": "TEST-UUID"}, "nodes": {}}
    fake_controller.return_value.to_dict.return_value = snapshot

    target = tmp_path / "nested" / "snap.json"
    rc = await main(_ns(dump=str(target)))

    assert rc == 0
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    # Pretty-printed (indented) and ends with a trailing newline.
    assert body.endswith("\n")
    assert "  " in body
    # Round-trips back to the original dict.
    assert _json.loads(body) == snapshot
    fake_controller.return_value.to_dict.assert_called_once_with()


async def test_main_dump_handles_unexpected_objects_via_default_str(
    fake_controller, tmp_path
) -> None:
    """``default=str`` is a guardrail — if ``to_dict()`` ever returns a
    non-JSON-native value (datetime, Path, dataclass that slipped
    through), the dump should stringify it rather than crashing."""
    snapshot = {"timestamp": datetime(2026, 5, 13, 12, 0, 0)}
    fake_controller.return_value.to_dict.return_value = snapshot

    target = tmp_path / "snap.json"
    rc = await main(_ns(dump=str(target)))
    assert rc == 0
    assert "2026-05-13" in target.read_text(encoding="utf-8")


# --- events loop: holds open until KeyboardInterrupt -----------------------


async def test_main_events_loop_exits_on_keyboard_interrupt(fake_controller) -> None:
    """``--events`` (the default) parks the coroutine in an
    ``asyncio.sleep(60)`` loop until Ctrl-C. The CLI must catch the
    interrupt, log a shutdown line, and return 0 — not let the
    ``KeyboardInterrupt`` propagate past ``main()``."""
    with patch("pyisyox.__main__.asyncio.sleep", AsyncMock(side_effect=KeyboardInterrupt)):
        rc = await main(_ns(events=True))
    assert rc == 0
    fake_controller.return_value.connect.assert_awaited_once_with(start_websocket=True)
    fake_controller.return_value.stop.assert_awaited_once()


# --- _resolve_log_level + run(): bootstrap branch coverage ----------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (_ns(events=False, verbose=True, debug=False), LOG_VERBOSE),
        (_ns(events=False, verbose=False, debug=True), logging.DEBUG),
        (_ns(events=False, verbose=False, debug=False), logging.INFO),
        # verbose wins over debug.
        (_ns(events=False, verbose=True, debug=True), LOG_VERBOSE),
    ],
)
def test_resolve_log_level_precedence(args: argparse.Namespace, expected: int) -> None:
    assert _resolve_log_level(args) == expected


def test_run_calls_main_and_returns_its_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run()`` is the bootstrap that ``__main__`` invokes — it must
    parse args, set the log level, dispatch to ``main()`` via
    ``asyncio.run``, and surface the int exit code."""
    parsed = _ns(events=False, debug=True, verbose=False)

    def fake_run(coro: object) -> int:
        # Close the coroutine so the test doesn't trigger a "never
        # awaited" RuntimeWarning under -W error.
        coro.close()  # type: ignore[union-attr]
        return 0

    monkeypatch.setattr("pyisyox.__main__.parse_args", lambda: parsed)
    monkeypatch.setattr("pyisyox.__main__.asyncio.run", fake_run)
    enable_logging_mock = MagicMock()
    monkeypatch.setattr("pyisyox.__main__.enable_logging", enable_logging_mock)

    assert run() == 0
    enable_logging_mock.assert_called_once_with(logging.DEBUG)
