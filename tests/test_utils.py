from __future__ import annotations

import pytest

from tmuxctl.utils import format_interval, parse_interval


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("30s", 30),
        ("15m", 900),
        ("2h", 7200),
        ("1d", 86400),
    ],
)
def test_parse_interval(value: str, seconds: int) -> None:
    assert parse_interval(value) == seconds


@pytest.mark.parametrize("value", ["", "1", "ab", "10x", "0m"])
def test_parse_interval_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_interval(value)


@pytest.mark.parametrize(
    ("seconds", "formatted"),
    [
        (30, "30s"),
        (900, "15m"),
        (7200, "2h"),
        (86400, "1d"),
        (61, "61s"),
    ],
)
def test_format_interval(seconds: int, formatted: str) -> None:
    assert format_interval(seconds) == formatted
