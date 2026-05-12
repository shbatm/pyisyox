"""Regression tests for fixture anonymization.

Captures committed under ``tests/fixtures/`` come from real eisy /
ISY-994 sessions and accidentally leak Insteon device prefixes, JWT
tokens, eisy MAC addresses, and email addresses if not scrubbed
first. The reproducible scrubber lives at
``~/src/.devcontainer_shared/home-assistant-core/isy994_stashed/anonymize_insteon_addresses.py``.

These tests assert that no committed fixture contains:

* JWT-shaped tokens longer than the synthetic placeholder.
* 6-byte colon-separated MAC addresses other than ``00:00:00:00:00:00``.
* Anything that looks like a personal email address.

They run as part of the regular pytest suite, so a contributor who
forgets to scrub before committing fails CI immediately rather than
the bad data slipping through review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: JWT shape: three URL-safe-base64 segments split by dots, with realistic
#: header + payload + signature lengths. The synthetic placeholder ends with
#: ``.synthetic`` (10 chars after the second dot) which is shorter than any
#: real ES256 signature (~80+ chars), so this regex passes the placeholder
#: while catching real tokens.
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}")

#: 6-byte colon-separated MAC.
MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")

#: Liberal email match — same shape the scrubber uses.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

#: The scrubber's placeholders. Anything matching the real-data regexes is
#: filtered against these to avoid flagging the placeholder itself.
PLACEHOLDER_MAC = "00:00:00:00:00:00"
PLACEHOLDER_EMAIL = "user@example.invalid"


def _all_fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURE_DIR.rglob("*") if p.is_file())


@pytest.mark.parametrize("fixture", _all_fixture_files(), ids=lambda p: str(p.relative_to(FIXTURE_DIR)))
def test_fixture_has_no_unscrubbed_jwt(fixture: Path) -> None:
    """No fixture should contain a real JWT — they all leak ``uuid``,
    ``sub``, ``iat``, ``exp`` claims, and (when ES256) the eisy's
    private-key signature material."""
    text = fixture.read_text(errors="replace")
    matches = JWT_RE.findall(text)
    real_tokens = [m for m in matches if not m.endswith(".synthetic") and m.count(".") == 2]
    assert real_tokens == [], (
        f"unscrubbed JWT in {fixture}: {real_tokens[0][:60]}... "
        f"run the scrubber to replace with the synthetic placeholder."
    )


@pytest.mark.parametrize("fixture", _all_fixture_files(), ids=lambda p: str(p.relative_to(FIXTURE_DIR)))
def test_fixture_has_no_unscrubbed_mac(fixture: Path) -> None:
    """eisy MAC addresses appear in JWT ``uuid`` claims, ``/api/config``
    responses, and event metadata. After scrubbing, only the placeholder
    should remain."""
    text = fixture.read_text(errors="replace")
    macs = {m for m in MAC_RE.findall(text) if m != PLACEHOLDER_MAC}
    assert macs == set(), (
        f"unscrubbed MAC(s) in {fixture}: {sorted(macs)[:3]}; "
        f"replace with {PLACEHOLDER_MAC} via the scrubber."
    )


@pytest.mark.parametrize("fixture", _all_fixture_files(), ids=lambda p: str(p.relative_to(FIXTURE_DIR)))
def test_fixture_has_no_personal_email(fixture: Path) -> None:
    """Portal logins use a real email; capture the JWT ``sub`` claim
    leaks it. Anything matching email shape that isn't the placeholder
    is suspect."""
    text = fixture.read_text(errors="replace")
    emails = {e for e in EMAIL_RE.findall(text) if e != PLACEHOLDER_EMAIL}
    assert emails == set(), (
        f"unscrubbed email(s) in {fixture}: {sorted(emails)[:3]}; "
        f"replace with {PLACEHOLDER_EMAIL} via the scrubber."
    )
