"""Tests for the log-redactor."""

from __future__ import annotations

from pyisyox.redactor import REDACTED, redact_sensitive


def test_redacts_top_level_tokens() -> None:
    payload = {
        "successful": True,
        "data": {"accessToken": "AAA.BBB.CCC", "refreshToken": "DDD.EEE.FFF"},
    }
    out = redact_sensitive(payload)
    assert out["data"]["accessToken"] == REDACTED
    assert out["data"]["refreshToken"] == REDACTED
    assert out["successful"] is True
    # Original is not mutated.
    assert payload["data"]["accessToken"] == "AAA.BBB.CCC"


def test_redacts_pg3_ssl_keypair() -> None:
    payload = {
        "data": {
            "ssl": {"key": "----BEGIN PRIVATE KEY----...", "cert": "...", "ca": "..."},
            "uuid": "00:21:b9:f2:72:65",
        }
    }
    out = redact_sensitive(payload)
    assert out["data"]["ssl"] == REDACTED
    assert out["data"]["uuid"] == "00:21:b9:f2:72:65"


def test_redacts_nested_pg3_token() -> None:
    payload = {"data": {"pg3AuthResult": {"token": "secret-pg3-token", "ok": True}}}
    out = redact_sensitive(payload)
    assert out["data"]["pg3AuthResult"]["token"] == REDACTED
    assert out["data"]["pg3AuthResult"]["ok"] is True


def test_redacts_authorization_header_in_dict() -> None:
    payload = {"headers": {"Authorization": "Bearer sec.ret.tok", "User-Agent": "ua"}}
    out = redact_sensitive(payload)
    assert out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["User-Agent"] == "ua"


def test_redacts_inside_lists() -> None:
    payload = {"items": [{"accessToken": "x"}, {"name": "ok"}]}
    out = redact_sensitive(payload)
    assert out["items"][0]["accessToken"] == REDACTED
    assert out["items"][1]["name"] == "ok"


def test_passes_through_primitives() -> None:
    assert redact_sensitive(42) == 42
    assert redact_sensitive("plain") == "plain"
    assert redact_sensitive(None) is None
    assert redact_sensitive([1, 2, 3]) == [1, 2, 3]


def test_custom_sensitive_keys() -> None:
    payload = {"accessToken": "still-secret", "secret_field": "narrow-test"}
    out = redact_sensitive(payload, sensitive_keys=frozenset({"secret_field"}))
    # Only the override applies; default keys aren't redacted under custom override.
    assert out["accessToken"] == "still-secret"
    assert out["secret_field"] == REDACTED
