from __future__ import annotations

from datetime import datetime, timezone


INTERVAL_SUFFIXES = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def display_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    dt = parse_timestamp(value)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def display_unix_timestamp(value: int) -> str:
    dt = datetime.fromtimestamp(value, tz=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def parse_interval(value: str) -> int:
    if len(value) < 2:
        raise ValueError("interval must look like 15m, 30s, 2h, or 1d")
    suffix = value[-1].lower()
    if suffix not in INTERVAL_SUFFIXES:
        raise ValueError("interval suffix must be one of s, m, h, d")
    number = value[:-1]
    if not number.isdigit():
        raise ValueError("interval value must start with an integer")
    seconds = int(number) * INTERVAL_SUFFIXES[suffix]
    if seconds <= 0:
        raise ValueError("interval must be greater than zero")
    return seconds


def format_interval(seconds: int) -> str:
    for suffix, unit in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds % unit == 0:
            return f"{seconds // unit}{suffix}"
    return f"{seconds}s"
