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
