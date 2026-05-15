"""Tests for the :class:`pyisyox.runtime.Program` typed-state accessors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pyisyox.client import IoXClient, ProgramRecord
from pyisyox.runtime.events import ProgramEvalState, ProgramRunState
from pyisyox.runtime.program import Program


def _client() -> IoXClient:
    return IoXClient.__new__(IoXClient)


def _program(
    running: str | None = None,
    *,
    last_run_time: str | None = None,
    last_finish_time: str | None = None,
    next_scheduled_run_time: str | None = None,
) -> Program:
    record = ProgramRecord(
        address="0030",
        name="Sunset",
        path="/Programs/Sunset",
        parent_address=None,
        is_folder=False,
        status=True,
        running=running,
        last_run_time=last_run_time,
        last_finish_time=last_finish_time,
        next_scheduled_run_time=next_scheduled_run_time,
    )
    return Program(record, _client())


@pytest.mark.parametrize(
    ("running", "run_state", "eval_state"),
    [
        ("21", ProgramRunState.IDLE, ProgramEvalState.TRUE),
        ("22", ProgramRunState.THEN, ProgramEvalState.TRUE),
        ("33", ProgramRunState.ELSE, ProgramEvalState.FALSE),
        ("11", ProgramRunState.IDLE, ProgramEvalState.UNKNOWN),
        ("F0", None, ProgramEvalState.NOT_LOADED),
        ("f0", None, ProgramEvalState.NOT_LOADED),
    ],
)
def test_run_state_decodes_ws_byte(
    running: str, run_state: ProgramRunState | None, eval_state: ProgramEvalState
) -> None:
    """WS frames carry ``<s>`` as two ASCII hex digits per cookbook §8.5.3."""
    program = _program(running)
    assert program.run_state is run_state
    assert program.eval_state is eval_state


@pytest.mark.parametrize(
    ("running", "run_state"),
    [
        ("idle", ProgramRunState.IDLE),
        ("Idle", ProgramRunState.IDLE),
        ("running then", ProgramRunState.THEN),
        ("Running Then", ProgramRunState.THEN),
        ("running  else", ProgramRunState.ELSE),
    ],
)
def test_run_state_decodes_rest_label(
    running: str, run_state: ProgramRunState
) -> None:
    """REST ``/api/programs`` returns human labels; eval is None there."""
    program = _program(running)
    assert program.run_state is run_state
    assert program.eval_state is None


@pytest.mark.parametrize("running", [None, "", "garbage", "ZZ"])
def test_run_state_returns_none_for_unparseable(running: str | None) -> None:
    program = _program(running)
    assert program.run_state is None
    assert program.eval_state is None


@pytest.mark.parametrize(
    ("attr", "wire"),
    [
        ("last_run_time", "2026-05-10T14:49:53.000Z"),
        ("last_finish_time", "2026-05-10T14:49:54.123Z"),
        ("next_scheduled_run_time", "2026-05-10T15:00:00.000Z"),
    ],
)
def test_timestamp_properties_parse_iso8601_z(attr: str, wire: str) -> None:
    """REST ``Z``-suffixed UTC strings parse to tz-aware ``datetime``."""
    program = _program(**{attr: wire})
    parsed = getattr(program, attr)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_timestamp_property_preserves_explicit_offset() -> None:
    """An ISO 8601 string with a non-UTC offset round-trips its tz."""
    program = _program(last_run_time="2026-05-10T09:49:53-05:00")
    parsed = program.last_run_time
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=-5)


def test_timestamp_property_naive_string_defaults_to_utc() -> None:
    """A bare ISO 8601 string with no tz is coerced to UTC for symmetry."""
    program = _program(last_run_time="2026-05-10T14:49:53")
    assert program.last_run_time == datetime(2026, 5, 10, 14, 49, 53, tzinfo=UTC)


@pytest.mark.parametrize("raw", [None, "", "not-a-date", "2026-13-99T99:99:99Z"])
def test_timestamp_property_returns_none_for_unparsable(raw: str | None) -> None:
    """Missing / blank / garbage timestamps round-trip to ``None``."""
    program = _program(last_run_time=raw)
    assert program.last_run_time is None
