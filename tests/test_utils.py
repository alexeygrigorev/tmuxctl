from __future__ import annotations

import pytest

from tmuxctl.utils import format_bytes, format_cpu_time, format_interval, parse_interval


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


@pytest.mark.parametrize(
    ("num_bytes", "formatted"),
    [
        (0, "0B"),
        (512, "512B"),
        (1024, "1.0K"),
        (1536, "1.5K"),
        (3 * 1024**3, "3.0G"),
        (5 * 1024**4, "5.0T"),
    ],
)
def test_format_bytes(num_bytes: int, formatted: str) -> None:
    assert format_bytes(num_bytes) == formatted


@pytest.mark.parametrize(
    ("nanoseconds", "formatted"),
    [
        (0, "0.0s"),
        (int(0.4 * 1e9), "0.4s"),
        (133 * 1_000_000_000, "2m13s"),
        (3700 * 1_000_000_000, "1h01m40s"),
    ],
)
def test_format_cpu_time(nanoseconds: int, formatted: str) -> None:
    assert format_cpu_time(nanoseconds) == formatted
