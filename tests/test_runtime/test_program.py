"""Tests for the :class:`pyisyox.runtime.Program` typed-state accessors."""

from __future__ import annotations

import pytest

from pyisyox.client import IoXClient, ProgramRecord
from pyisyox.runtime.events import ProgramEvalState, ProgramRunState
from pyisyox.runtime.program import Program


def _client() -> IoXClient:
    return IoXClient.__new__(IoXClient)


def _program(running: str | None) -> Program:
    record = ProgramRecord(
        address="0030",
        name="Sunset",
        path="/Programs/Sunset",
        parent_address=None,
        is_folder=False,
        status=True,
        running=running,
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
