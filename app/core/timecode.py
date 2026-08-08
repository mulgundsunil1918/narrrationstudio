"""Timecode conversion.

The canonical internal unit is the **integer millisecond**. Floats are never used
to carry a timestamp between modules: they accumulate drift, and this app depends
on ``segment.end == next_segment.start`` being exactly true. Samples are derived
from milliseconds only at the moment audio is written.
"""

from __future__ import annotations

import re

# 00:00:04,680 / 00:00:04.680 / 0:00:04,68 -- hours may be 1+ digits, the
# fractional part may be 1-3 digits, and either separator is accepted on input.
_TIMESTAMP_RE = re.compile(
    r"^\s*(?P<h>\d{1,3}):(?P<m>\d{1,2}):(?P<s>\d{1,2})(?:[,.](?P<frac>\d{1,3}))?\s*$"
)

MS_PER_SECOND = 1000
MS_PER_MINUTE = 60 * MS_PER_SECOND
MS_PER_HOUR = 60 * MS_PER_MINUTE


class TimecodeError(ValueError):
    """Raised when a timestamp cannot be understood."""


def parse_timestamp(text: str) -> int:
    """Parse an SRT timestamp into milliseconds.

    Accepts both ``,`` and ``.`` as the fractional separator, and tolerates
    truncated fractions (``00:00:04,68`` -> 4680 ms) the way most players do.
    """
    match = _TIMESTAMP_RE.match(text)
    if not match:
        raise TimecodeError(f"Not a valid timestamp: {text!r}")

    minutes = int(match["m"])
    seconds = int(match["s"])
    if minutes > 59:
        raise TimecodeError(f"Minutes out of range in {text!r}")
    if seconds > 59:
        raise TimecodeError(f"Seconds out of range in {text!r}")

    # "4,68" means 680 ms, not 68 ms -- pad on the right, not the left.
    frac = (match["frac"] or "0").ljust(3, "0")

    return (
        int(match["h"]) * MS_PER_HOUR
        + minutes * MS_PER_MINUTE
        + seconds * MS_PER_SECOND
        + int(frac)
    )


def format_timestamp(ms: int, separator: str = ",") -> str:
    """Format milliseconds as ``HH:MM:SS,mmm`` for writing SRT files."""
    if ms < 0:
        raise TimecodeError(f"Cannot format a negative timestamp: {ms}")
    ms = int(round(ms))
    hours, rest = divmod(ms, MS_PER_HOUR)
    minutes, rest = divmod(rest, MS_PER_MINUTE)
    seconds, millis = divmod(rest, MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def format_display(ms: int) -> str:
    """Format milliseconds as ``HH:MM:SS.mmm`` for on-screen display."""
    return format_timestamp(ms, separator=".")


def format_duration(ms: int) -> str:
    """Format a span as a compact human duration, e.g. ``4.680s`` or ``1:23.400``."""
    if ms < MS_PER_MINUTE:
        return f"{ms / MS_PER_SECOND:.3f}s"
    minutes, rest = divmod(int(ms), MS_PER_MINUTE)
    return f"{minutes}:{rest / MS_PER_SECOND:06.3f}"


def ms_to_samples(ms: int, sample_rate: int) -> int:
    """Convert milliseconds to a sample offset at ``sample_rate``.

    Uses integer arithmetic with round-half-up so that adjacent segments meeting
    at the same millisecond also meet at the same sample -- no one-sample gaps or
    overlaps at segment boundaries.
    """
    if sample_rate <= 0:
        raise TimecodeError(f"Sample rate must be positive, got {sample_rate}")
    return (int(ms) * sample_rate + MS_PER_SECOND // 2) // MS_PER_SECOND


def samples_to_ms(samples: int, sample_rate: int) -> int:
    """Convert a sample offset back to milliseconds."""
    if sample_rate <= 0:
        raise TimecodeError(f"Sample rate must be positive, got {sample_rate}")
    return (int(samples) * MS_PER_SECOND + sample_rate // 2) // sample_rate


def seconds_to_ms(seconds: float) -> int:
    """Convert float seconds to integer milliseconds (round-half-away-from-zero)."""
    return int(round(seconds * MS_PER_SECOND))


def ms_to_seconds(ms: int) -> float:
    """Convert integer milliseconds to float seconds (for display/DSP only)."""
    return ms / MS_PER_SECOND
