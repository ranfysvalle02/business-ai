"""Unit tests for slug validation — pure, no DB required."""
from __future__ import annotations

import pytest

import main


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme", "acme"),
        ("  Beta  ", "beta"),
        ("MyStore", "mystore"),
        ("my-store-01", "my-store-01"),
    ],
)
def test_validate_slug_normalizes(raw, expected):
    assert main._validate_slug(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "ab",           # too short (< 3)
        "-lead",        # leading hyphen
        "trail-",       # trailing hyphen
        "has space",    # space
        "UP_score",     # underscore / uppercase
        "emoji😀store",  # non-ascii
        "a" * 41,       # too long (> 40)
    ],
)
def test_validate_slug_rejects_invalid(bad):
    with pytest.raises(ValueError):
        main._validate_slug(bad)


def test_validate_slug_rejects_every_banned_slug():
    # Every reserved/banned word that is otherwise slug-shaped must be rejected.
    checked = 0
    for banned in main.BANNED_SLUGS:
        if not main._SLUG_RE.match(banned):
            continue  # would already fail on shape; the banned-set guard is what we test here
        checked += 1
        with pytest.raises(ValueError):
            main._validate_slug(banned)
    assert checked > 0, "expected at least one slug-shaped banned word to guard"


def test_reserved_segments_are_banned():
    # Nothing that routes to a global surface may be claimed as a store.
    assert main.RESERVED_SEGMENTS <= main.BANNED_SLUGS
    for seg in ("admin", "api", "auth", "manage", "static"):
        assert seg in main.BANNED_SLUGS
