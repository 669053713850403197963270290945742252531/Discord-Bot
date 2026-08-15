"""Date formatting/parsing, Discord timestamps, and temp-whitelist expiration helpers."""

import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from . import config


def format_join_date(dt: Optional[datetime] = None) -> str:
    """Formats a datetime as m/d/yyyy, h:mm:ss AM/PM in LOCAL_TZ, e.g. '6/19/2026, 3:24:53 AM'.

    Month, day, and hour are not zero-padded; minutes and seconds are.
    Automatically accounts for EST/EDT.
    """
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(config.LOCAL_TZ)
    hour_12 = dt.hour % 12 or 12
    period = "AM" if dt.hour < 12 else "PM"
    return f"{dt.month}/{dt.day}/{dt.year}, {hour_12}:{dt.minute:02d}:{dt.second:02d} {period}"


def parse_join_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parses a JoinDate-style 'm/d/yyyy, h:mm:ss AM/PM' string back into a
    tz-aware datetime in LOCAL_TZ. Also accepts the older 'yyyy-mm-dd'
    format for entries created before the JoinDate format change (assumed
    UTC, since that's how it was originally stored).

    Returns None if `date_str` is empty or doesn't match either format --
    meaning there's nothing to parse, not that something is broken.
    """
    if not date_str:
        return None

    try:
        return datetime.strptime(date_str, "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=config.LOCAL_TZ)
    except ValueError:
        pass

    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    return None


def format_discord_timestamp(date_str: Optional[str], fmt: str = "D") -> str:
    """Converts a JoinDate-style string into a Discord <t:...:fmt> timestamp, falling back to the raw string on failure."""
    if not date_str:
        return "N/A"

    dt = parse_join_date(date_str)
    if dt is None:
        return date_str

    return f"<t:{int(dt.timestamp())}:{fmt}>"


# =========================================================================
# Temporary whitelist expiration (stored in the Notes field)
# =========================================================================
#
# The expiration is written straight into the Notes field (reusing the same
# date format as JoinDate), so it survives bot restarts and can be read back
# by anything that looks at the whitelist, not just the bot process.

EXPIRATION_NOTE_RE = re.compile(r"^Expires on (.+)$")


def format_expiration_note(expiration_dt: datetime) -> str:
    """
    Builds the Notes-field string that marks a whitelist entry as temporary
    and records exactly when it expires, e.g. 'Expires on 7/19/2026, 5:51:12 AM'.
    Reuses format_join_date()'s format so it round-trips through
    parse_expiration_note().
    """
    return f"Expires on {format_join_date(expiration_dt)}"


def parse_expiration_note(notes: Optional[str]) -> Optional[datetime]:
    """
    Reverses format_expiration_note(): pulls the expiration datetime back
    out of a Notes field, returned as a tz-aware datetime in LOCAL_TZ.

    Returns None if `notes` is empty, doesn't match the "Expires on ..."
    pattern, or has an unparseable date -- meaning it's an unrelated/manual
    note rather than a temp-whitelist marker, not that something is broken.
    """
    if not notes:
        return None
    match = EXPIRATION_NOTE_RE.match(notes.strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=config.LOCAL_TZ)
    except ValueError:
        return None


def is_notes_locked(entry: Dict[str, Any]) -> bool:
    """
    True if `entry`'s Notes field currently holds an unexpired temporary-
    whitelist expiration marker (see format_expiration_note /
    parse_expiration_note above).

    While this is True, nothing should overwrite or clear this entry's
    Notes field -- not to blank, and not to some other custom value --
    since that field is the *only* record of when this entry's temporary
    whitelist expires.

    Returns False (unlocked) once the marker has expired -- an expired temp
    whitelist's Notes are just as removable/editable as a normal note.
    """
    expires_at = parse_expiration_note(entry.get("Notes"))
    return expires_at is not None and expires_at > datetime.now(timezone.utc)


def humanize_timeleft(delta: timedelta, *, suffix: bool = True) -> str:
    """
    Renders a timedelta as a single friendly '<value> <unit>' string using
    the largest whole unit that fits (e.g. '1 month', '3 weeks',
    '5 seconds'), so it reads naturally regardless of whether the whitelist
    duration was 5 minutes or a full year.

    By default appends " left" (e.g. "1 month left") for standalone use
    like a "Time Left" field. Pass suffix=False for call sites that already
    supply their own framing -- e.g. "You can reset your HWID again in
    {...}" reads better as "... in 6 days." than "... in 6 days left."

    Month/year lengths are approximate (30/365 days) since this is a
    human-readable countdown, not a calendar calculation.
    """
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "Expired"

    units = [
        ("year", 31536000),
        ("month", 2592000),
        ("week", 604800),
        ("day", 86400),
        ("hour", 3600),
        ("minute", 60),
        ("second", 1),
    ]
    for name, seconds_per_unit in units:
        value = total_seconds // seconds_per_unit
        if value >= 1:
            label = name if value == 1 else f"{name}s"
            return f"{value} {label} left" if suffix else f"{value} {label}"
    return "Expired"


# =========================================================================
# UTC ISO-8601 timestamps (storage/BotState.json)
# =========================================================================
#
# BotState.json's timestamps (banned_at/unban_at, started_at/unlock_at,
# granted_at/expires_at, etc.) always use this format -- plain UTC with a
# trailing Z, unlike JoinDate's local-timezone "m/d/yyyy, h:mm:ss AM/PM"
# above -- since BotState entries are read back by reconcile_*() functions
# to re-derive a precise "how many seconds until this fires" figure, not
# just displayed to a human.

def format_iso(dt: datetime) -> str:
    """Formats a datetime as UTC ISO-8601 with a trailing Z, e.g.
    '2026-07-31T18:42:07Z'. Naive datetimes are assumed to already be UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Reverses format_iso(). Returns None if `value` is empty or doesn't
    match the expected format -- callers should treat that as "nothing
    scheduled" rather than an error."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def seconds_until(dt: Optional[datetime]) -> float:
    """How many seconds from now until `dt`, clamped to 0 -- never negative,
    so a timer whose target time has already passed (e.g. the bot was down
    past a scheduled unban/unlock/expiry) fires almost immediately on
    reconciliation instead of raising or waiting a negative amount of time.
    A missing `dt` (None) also resolves to 0, so a caller doesn't need a
    separate branch for "nothing to wait for -- do it now"."""
    if dt is None:
        return 0.0
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def hwid_reset_cooldown_remaining(entry: Dict[str, Any]) -> Optional[timedelta]:
    """Returns how much time is left before `entry` can use the control
    panel's "Reset HWID" button again, based on its LastHwidReset field and
    RESET_HWID_COOLDOWN. Returns None if a reset is allowed right now --
    either because LastHwidReset is missing/unparseable (never reset
    before), or because RESET_HWID_COOLDOWN has already elapsed since the
    last one."""
    last_reset = parse_join_date(entry.get("LastHwidReset"))
    if not last_reset:
        return None

    remaining = config.RESET_HWID_COOLDOWN - (datetime.now(timezone.utc) - last_reset)
    return remaining if remaining.total_seconds() > 0 else None


# =========================================================================
# Time-range filter parsing (shared by /url clear's `before` option)
# =========================================================================

# Matches a relative duration like "20 minutes", "2 hours ago", "3 days" --
# an optional trailing "ago" is accepted but not required, since both read
# naturally for a cutoff ("clear everything from 20 minutes ago" and
# "clear everything from the last 20 minutes" mean the same cutoff here).
_RELATIVE_TIME_RE = re.compile(
    r"^(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|years?|yrs?)\s*(?:ago)?$",
    re.IGNORECASE,
)

# Seconds-per-unit for every unit _RELATIVE_TIME_RE can match, keyed by the
# singular form (the regex's plural "s"/alternate spelling is stripped
# before lookup -- see parse_time_filter()). Month/year are approximate
# (30/365 days), same convention as humanize_timeleft() above -- a cutoff a
# few hours off a calendar month doesn't meaningfully change what /url
# clear catches.
_RELATIVE_TIME_UNIT_SECONDS = {
    "second": 1, "sec": 1,
    "minute": 60, "min": 60,
    "hour": 3600, "hr": 3600,
    "day": 86400,
    "week": 604800, "wk": 604800,
    "month": 2592000,
    "year": 31536000, "yr": 31536000,
}

# Absolute date/time formats parse_time_filter() falls back to once the
# relative-duration shape doesn't match. Interpreted in LOCAL_TZ (like
# parse_join_date()'s JoinDate format above) since that's how every other
# date staff sees in this bot (JoinDate, Notes expiration markers, Discord
# <t:...> timestamps rendered in their own client) already reads -- typing
# a wall-clock time here should mean *their* wall clock, not UTC. Ordered
# most-specific first purely so the first match found is also the most
# precise one, though strptime rejects non-matches outright regardless of
# order.
_ABSOLUTE_LOCAL_TIME_FORMATS = (
    "%m/%d/%Y, %I:%M:%S %p",  # JoinDate's own format, e.g. "6/19/2026, 3:24:53 AM"
    "%m/%d/%Y, %I:%M %p",     # same, no seconds -- e.g. "8/13/2026, 2:50 AM"
    "%m/%d/%Y %I:%M:%S %p",   # same, no comma
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",                # date only -- midnight local time
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def parse_time_filter(text: Optional[str]) -> Optional[datetime]:
    """
    Parses /url clear's `before` option into a tz-aware UTC cutoff
    datetime, for filtering storage/shortened-urls.json entries by their
    created_at (itself always UTC ISO -- see parse_iso() above). Accepts
    two shapes, tried in this order:

      1. A relative duration -- "20 minutes", "2 hours ago", "3 days" --
         resolved against the current moment, so "20 minutes" always means
         20 minutes before *now*, not 20 minutes before some other
         reference point.
      2. An absolute date/time, matched against
         _ABSOLUTE_LOCAL_TIME_FORMATS (JoinDate's own "m/d/yyyy,
         h:mm:ss AM/PM" plus a handful of looser variants -- no comma, no
         seconds, date-only) and interpreted in LOCAL_TZ, or a plain UTC
         ISO-8601 string (format_iso()'s own output), interpreted as UTC
         directly.

    Returns None if `text` matches neither shape, or is empty -- callers
    should treat that as "couldn't understand this", not as "no filter".
    """
    if not text:
        return None
    text = text.strip()
    if not text:
        return None

    relative = _RELATIVE_TIME_RE.match(text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower().rstrip("s")
        seconds_per_unit = _RELATIVE_TIME_UNIT_SECONDS.get(unit)
        if seconds_per_unit is not None:
            return datetime.now(timezone.utc) - timedelta(seconds=amount * seconds_per_unit)

    # ISO-8601 UTC (format_iso()'s own shape) -- checked before the local-
    # time formats since its trailing "Z" makes it unambiguous.
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    for fmt in _ABSOLUTE_LOCAL_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=config.LOCAL_TZ)
        except ValueError:
            continue

    return None