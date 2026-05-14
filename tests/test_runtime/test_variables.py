"""Tests for the typed ``Variable`` runtime wrapper.

The wrapper layer sits over :class:`pyisyox.client.VariableRecord` and
routes mutations through :meth:`IoXClient.post_variable_update`. Reads
hit the in-memory record directly so the test surface is just:

* read-side properties round-trip the underlying record;
* each mutation coroutine posts to ``/api/variables/{type}/{id}`` with
  the right body and updates the record in place on success.
"""

from __future__ import annotations

import logging

import pytest

from pyisyox.auth import LocalAuth
from pyisyox.client import IoXClient, VariableRecord
from pyisyox.runtime import Variable
from tests.test_client.conftest import FakeSession

BASE = "https://eisy.local"


def _make_client(session: FakeSession) -> IoXClient:
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True
    return client


def _make_record(**overrides) -> VariableRecord:
    base = {
        "type_id": "2",
        "id": "8",
        "name": "Boost Mode",
        "value": 60,
        "init": 0,
        "precision": 0,
        "ts": "2026-05-08T13:56:48.000Z",
    }
    base.update(overrides)
    return VariableRecord(**base)


def test_variable_exposes_record_fields() -> None:
    """Read-side properties just forward to the underlying record."""
    record = _make_record(precision=2, value=12345)
    variable = Variable.from_record(record, _make_client(FakeSession(BASE)))

    assert variable.type_id == "2"
    assert variable.id == "8"
    assert variable.address == "2.8"
    assert variable.name == "Boost Mode"
    assert variable.value == 12345
    assert variable.init == 0
    assert variable.precision == 2
    assert variable.ts == "2026-05-08T13:56:48.000Z"


@pytest.mark.asyncio
async def test_set_value_posts_value_body_and_updates_record() -> None:
    """``set_value`` hits POST /api/variables/{type}/{id} with ``{"value": N}``
    and reflects the new value on the wrapper after success — so a consumer
    reading ``variable.value`` immediately after the await sees the new state
    without waiting for a WS frame."""
    record = _make_record(value=60)
    session = FakeSession(BASE)
    session.set_route("POST", "/api/variables/2/8", 200, '{"successful": true}')
    variable = Variable.from_record(record, _make_client(session))

    await variable.set_value(75)

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/variables/2/8")
    assert kwargs["json"] == {"value": 75}
    assert variable.value == 75
    assert record.value == 75  # underlying record updated in place


@pytest.mark.asyncio
async def test_set_value_passes_float_through_to_wire() -> None:
    """Floats reach the wire verbatim. The modern ``POST /api/variables``
    endpoint accepts both ``int`` and ``float``; on a ``precision > 0``
    variable, the controller applies the ``* 10**precision`` scale to
    a float server-side. Coercing to ``int`` here would truncate the
    fractional portion and double-shift the value (controller would
    pre-scale the truncated int again), producing wrong stored state."""
    record = _make_record(precision=1)
    session = FakeSession(BASE)
    session.set_route("POST", "/api/variables/2/8", 200, '{"successful": true}')
    variable = Variable.from_record(record, _make_client(session))

    await variable.set_value(51.5)

    _, _, kwargs = session.calls[-1]
    assert kwargs["json"] == {"value": 51.5}
    assert variable.value == 51.5  # record updated in place


@pytest.mark.asyncio
async def test_set_value_accepts_legacy_string_input() -> None:
    """A string caller is parsed — matches the legacy
    ``Controller.set_variable_value`` contract. Strings with a decimal
    point parse as ``float`` so the float-passthrough path applies."""
    record = _make_record()
    session = FakeSession(BASE)
    session.set_route("POST", "/api/variables/2/8", 200, '{"successful": true}')
    variable = Variable.from_record(record, _make_client(session))

    await variable.set_value("42")  # type: ignore[arg-type]
    _, _, kwargs = session.calls[-1]
    assert kwargs["json"] == {"value": 42}
    assert variable.value == 42

    await variable.set_value("3.14")  # type: ignore[arg-type]
    _, _, kwargs = session.calls[-1]
    assert kwargs["json"] == {"value": 3.14}
    assert variable.value == 3.14


@pytest.mark.asyncio
async def test_set_init_posts_init_body() -> None:
    record = _make_record(init=0)
    session = FakeSession(BASE)
    session.set_route("POST", "/api/variables/2/8", 200, '{"successful": true}')
    variable = Variable.from_record(record, _make_client(session))

    await variable.set_init(100)

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/variables/2/8")
    assert kwargs["json"] == {"init": 100}
    assert variable.init == 100


@pytest.mark.asyncio
async def test_rename_posts_name_body_and_updates_record() -> None:
    record = _make_record(name="Old Name")
    session = FakeSession(BASE)
    session.set_route("POST", "/api/variables/2/8", 200, '{"successful": true}')
    variable = Variable.from_record(record, _make_client(session))

    await variable.rename("New Name")

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/variables/2/8")
    assert kwargs["json"] == {"name": "New Name"}
    assert variable.name == "New Name"
    assert record.name == "New Name"


@pytest.mark.asyncio
async def test_set_value_logs_debug_with_url_and_body(caplog: pytest.LogCaptureFixture) -> None:
    """Every wire write should leave a DEBUG breadcrumb at the client
    layer so a user filing a bug can show the request that went out
    (the WS echo only confirms the *change*, not the original call)."""
    record = _make_record()
    session = FakeSession(BASE)
    session.set_route("POST", "/api/variables/2/8", 200, '{"successful": true}')
    variable = Variable.from_record(record, _make_client(session))

    with caplog.at_level(logging.DEBUG, logger="pyisyox.client"):
        await variable.set_value(42)

    assert any(
        "Variable write" in msg and "/api/variables/2/8" in msg
        for msg in caplog.messages
    )


@pytest.mark.asyncio
async def test_set_precision_posts_prec_body_and_updates_record() -> None:
    """``set_precision`` hits POST /api/variables/{type}/{id} with
    ``{"prec": N}`` and reflects the new precision on the wrapper. The
    controller fires VARIABLE_TABLE_CHANGED on this write (not the
    per-value 6/7 frames), so the in-place record update is the only
    way the wrapper sees the new value before the next refresh."""
    record = _make_record(precision=0)
    session = FakeSession(BASE)
    session.set_route("POST", "/api/variables/2/8", 200, '{"successful": true}')
    variable = Variable.from_record(record, _make_client(session))

    await variable.set_precision(1)

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("POST", "/api/variables/2/8")
    assert kwargs["json"] == {"prec": 1}
    assert variable.precision == 1
    assert record.precision == 1


@pytest.mark.parametrize("bad", [-1, 1.5, True, "1", None])
@pytest.mark.asyncio
async def test_set_precision_rejects_non_int_or_negative(bad: object) -> None:
    """Defensive guard — caller-side validation so a bad value can't
    silently corrupt the wire write."""
    record = _make_record()
    variable = Variable.from_record(record, _make_client(FakeSession(BASE)))
    with pytest.raises(ValueError):
        await variable.set_precision(bad)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delete_hits_delete_endpoint() -> None:
    """``delete`` hits DELETE /api/variables/{type}/{id}. The wrapper
    doesn't drop itself from the controller's registry — the
    accompanying VARIABLE_TABLE_CHANGED frame triggers the controller's
    auto-refresh listener which prunes the entry."""
    record = _make_record()
    session = FakeSession(BASE)
    # DELETE returns no body in many controller versions; FakeSession
    # serves a `None` body which becomes "" — _send_json handles that.
    session.set_route("DELETE", "/api/variables/2/8", 200, None)
    variable = Variable.from_record(record, _make_client(session))

    await variable.delete()

    method, path, _ = session.calls[-1]
    assert (method, path) == ("DELETE", "/api/variables/2/8")


async def test_variable_repr_includes_identifying_fields() -> None:
    """Debuggability — ``repr`` should show enough to identify the variable
    without dumping the timestamp / init noise."""
    record = _make_record()
    variable = Variable.from_record(record, _make_client(FakeSession(BASE)))
    text = repr(variable)
    assert "Variable" in text
    assert "type_id='2'" in text
    assert "id='8'" in text
    assert "name='Boost Mode'" in text
    assert "value=60" in text
