"""Tests for :func:`pyisyox.logging.enable_logging`.

The helper is small but ships with three branches that interact with
process-global ``logging`` state — colorlog formatter, plain
formatter, NullHandler — and they all need exercising under isolation
so a test that exercises one branch doesn't leak handlers into the
next.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import patch

import pytest

from pyisyox.logging import _LOGGER, LOG_VERBOSE, enable_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> None:
    """Snapshot/restore the root + ``pyisyox`` logger state around each
    test. ``enable_logging`` calls ``basicConfig`` which mutates the
    root handler list; without this, tests pollute each other and
    the ordering becomes load-bearing.

    Also clears root handlers *before* the test runs — pytest installs
    a capture handler at session start, which would otherwise make
    ``basicConfig`` a no-op (it bails when any handlers are configured)
    and we'd never observe the level/formatter changes that the helper
    is supposed to apply."""
    root = logging.getLogger()
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_pkg_handlers = list(_LOGGER.handlers)
    saved_pkg_level = _LOGGER.level
    root.handlers[:] = []
    yield
    root.handlers[:] = saved_root_handlers
    root.setLevel(saved_root_level)
    _LOGGER.handlers[:] = saved_pkg_handlers
    _LOGGER.setLevel(saved_pkg_level)


def test_verbose_level_is_below_debug() -> None:
    """``LOG_VERBOSE`` must remain below DEBUG so consumer code that
    selects it gets *more* output, not less."""
    assert LOG_VERBOSE < logging.DEBUG


def test_verbose_level_name_registered_at_import_time() -> None:
    """The 'VERBOSE' level name must be registered with stdlib logging
    at module import — consumers like Home Assistant never call
    ``enable_logging`` but still need the name resolved so
    ``configuration.yaml`` ``logger: { logs: { pyisyox: verbose } }``
    works and log records render as VERBOSE (not 'Level 5')."""
    # Note: we don't call enable_logging here — the name must already
    # be registered as a side-effect of `import pyisyox.logging`.
    assert logging.getLevelName(LOG_VERBOSE) == "VERBOSE"


def test_default_uses_colored_formatter_when_colorlog_available() -> None:
    """The colorlog branch wraps the root handler's formatter with
    one whose format string contains the ``%(log_color)s`` token."""
    enable_logging(logging.INFO)
    root = logging.getLogger()
    assert root.handlers, "basicConfig should have set up at least one handler"
    fmt_str = root.handlers[0].formatter._style._fmt  # type: ignore[union-attr]
    assert "log_color" in fmt_str


def test_add_null_handler_attaches_to_package_logger() -> None:
    """Library-mode init (``add_null_handler=True``) attaches a
    NullHandler to the package logger so apps that import pyisyox
    without configuring logging don't get the "no handlers" warning."""
    before = [type(h) for h in _LOGGER.handlers]
    enable_logging(add_null_handler=True)
    after = [type(h) for h in _LOGGER.handlers]
    assert any(t is logging.NullHandler for t in after) and after != before


def test_aiohttp_access_logger_silenced() -> None:
    """``aiohttp.access`` is one of the noisiest stdlib loggers when
    the eisy is responding to bursts of property updates. It must
    be pinned at WARNING regardless of the level passed in."""
    enable_logging(LOG_VERBOSE)
    assert logging.getLogger("aiohttp.access").level == logging.WARNING


def test_falls_back_when_colorlog_missing() -> None:
    """If ``colorlog`` isn't installed, the helper must still
    successfully configure logging via the bare-formatter fallback.
    Simulate the import failure with a sys.modules patch."""
    with patch.dict(sys.modules, {"colorlog": None}):
        enable_logging(logging.INFO)
    fmt = logging.getLogger().handlers[0].formatter
    assert fmt is not None
