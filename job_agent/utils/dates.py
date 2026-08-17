"""
Date parsing helpers (Phase 3).

Job boards report posting dates in wildly inconsistent formats:
- ISO 8601: "2026-08-12T10:00:00Z", "2026-08-12"
- Relative: "2 days ago", "Posted 3 weeks ago", "today", "yesterday"
- Human: "August 12, 2026", "12 Aug 2026"

parse_posted_at() normalizes any of these to a naive UTC datetime, returning
None when the input cannot be understood (callers keep the raw text in
Job.posted_at_text so nothing is lost).
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """
    Current UTC time as a naive datetime.

    datetime.utcnow() is deprecated (removal scheduled), but every datetime
    column in the schema is naive, so an aware value would poison comparisons
    with stored rows. This keeps the storage convention while using the
    supported API.

    Returns:
        Naive datetime representing the current UTC time
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

# "2 days ago", "posted 3 weeks ago", "1 hour ago", "30+ days ago"
_RELATIVE_PATTERN = re.compile(
    r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month|year)s?\s*ago",
    re.IGNORECASE,
)

_RELATIVE_UNITS = {
    "minute": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}

# Absolute formats worth trying, most specific first
_ABSOLUTE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%m/%d/%Y",
]


def parse_posted_at(value: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Parse a posting date from any common job-board format.

    Args:
        value: Raw date text from the posting (may be None)
        now: Reference time for relative dates (defaults to utcnow)

    Returns:
        Naive UTC datetime, or None if the value could not be parsed
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value

    text = str(value).strip()
    if not text:
        return None

    reference = now or utcnow()
    lowered = text.lower()

    # Relative shorthands
    if lowered in ("today", "just posted", "new"):
        return reference
    if lowered == "yesterday":
        return reference - timedelta(days=1)

    match = _RELATIVE_PATTERN.search(lowered)
    if match:
        amount = int(match.group(1))
        unit = _RELATIVE_UNITS.get(match.group(2).lower())
        if unit is not None:
            return reference - (unit * amount)

    # ISO 8601 (handles trailing Z and offsets)
    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass

    # Explicit formats
    cleaned = re.sub(r"^posted\s+(on\s+)?", "", text, flags=re.IGNORECASE).strip()
    for fmt in _ABSOLUTE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue

    logger.debug(f"Could not parse posted_at value: {value!r}")
    return None
