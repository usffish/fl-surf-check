"""
Your personal surf log - the only thing this tool stores about YOU.

Everything else the app persists (the climatology cache) describes Florida and
is identical for every user. This file is the exception: it is a record of
where and when you actually surfed, and it exists to feed two scoring factors
that no amount of forecast data can supply.

  - ITCH: how many days since you last surfed anywhere. The longer the dry
    spell, the more driving you will tolerate.
  - NOVELTY: how often you have surfed each spot, relative to the others. Spots
    you keep returning to get handicapped so less-visited ones surface.

STORAGE
-------
A JSON file, appended to and never rewritten in place, so a bad write cannot
lose history:

    {"sessions": [{"date": "2026-08-18", "spot": "Ponce Inlet"}, ...]}

It lives OUTSIDE the repo by default (~/.fl_surf_log.json). It is personal
data, not project content, and a public repo is the wrong place for a record
of where someone goes and when.

THE HONEST CAVEAT
-----------------
Both factors are worth exactly as much as your logging discipline. An unlogged
session is invisible: novelty will think you have never surfed a spot you go to
every week, and itch will keep climbing as though you have not been in the
water. Nothing here can detect that - the tool has no way to know you surfed
unless you tell it. `days_since_last` returning None (empty log) is treated as
"no itch" rather than "infinite itch" for exactly this reason: a fresh install
should not open by insisting you drive to the panhandle.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
from dataclasses import dataclass, field

#: Default location. Outside the repo - this is personal data.
DEFAULT_LOG_PATH = os.path.expanduser("~/.fl_surf_log.json")

#: Environment override, matching the FL_SURF_ZIP convention.
LOG_ENV_VAR = "FL_SURF_LOG"


@dataclass(frozen=True)
class Session:
    """One surf session: a date and a spot name."""
    date: dt.date
    spot: str


@dataclass
class SurfLog:
    """Every session on record, in the order they were written."""
    sessions: list[Session] = field(default_factory=list)
    path: str = DEFAULT_LOG_PATH

    def days_since_last(self, today: dt.date | None = None) -> int | None:
        """
        Days since the most recent session anywhere.

        Returns None for an empty log - "unknown", not "forever". Callers treat
        that as zero itch, because a brand-new log should not behave as though
        the user has been out of the water for years.
        """
        if not self.sessions:
            return None
        today = today or dt.date.today()
        latest = max(s.date for s in self.sessions)
        return max(0, (today - latest).days)

    def visit_counts(self, spot_names) -> dict[str, int]:
        """
        Sessions per spot, including zeros for spots never surfed.

        The zeros matter: novelty is a z-score across the whole spot list, so
        the never-visited spots are most of the distribution and must be
        present for it to mean anything.
        """
        counts = {name: 0 for name in spot_names}
        for s in self.sessions:
            if s.spot in counts:
                counts[s.spot] += 1
        return counts

    def total_sessions(self) -> int:
        return len(self.sessions)


def load_log(path: str | None = None) -> SurfLog:
    """
    Read the surf log. A missing or unreadable file yields an empty log rather
    than an error - the tool must still run for someone who has never logged
    anything, which is everyone on first use.
    """
    resolved = path or os.environ.get(LOG_ENV_VAR) or DEFAULT_LOG_PATH
    try:
        with open(resolved) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return SurfLog([], resolved)

    sessions = []
    for entry in raw.get("sessions", []):
        try:
            sessions.append(Session(dt.date.fromisoformat(entry["date"]), entry["spot"]))
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed rows rather than discarding the whole log
    return SurfLog(sessions, resolved)


def record_session(spot: str, on: dt.date | None = None, path: str | None = None) -> SurfLog:
    """
    Append a session and persist it.

    Written atomically via a temp file and os.replace, so an interrupted write
    cannot truncate an existing log.
    """
    log = load_log(path)
    log.sessions.append(Session(on or dt.date.today(), spot))

    payload = {
        "sessions": [
            {"date": s.date.isoformat(), "spot": s.spot}
            for s in sorted(log.sessions, key=lambda x: x.date)
        ]
    }
    tmp = f"{log.path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, log.path)
    return log


def resolve_spot_name(query: str, spot_names) -> str | None:
    """
    Match a typed spot name against the real list.

    Exact match first, then case-insensitive substring, then fuzzy - so
    "apollo" finds "Apollo Beach (Canaveral NS)" without anyone typing the
    parenthetical. Returns None if nothing is close enough, rather than
    guessing wrong and silently logging a session against the wrong break.
    """
    names = list(spot_names)
    if query in names:
        return query

    lowered = query.lower().strip()
    exact_ci = [n for n in names if n.lower() == lowered]
    if exact_ci:
        return exact_ci[0]

    substring = [n for n in names if lowered in n.lower()]
    if len(substring) == 1:
        return substring[0]
    if len(substring) > 1:
        # Prefer the shortest - "Cocoa Beach Pier" over a longer name that
        # merely contains the same words.
        return min(substring, key=len)

    close = difflib.get_close_matches(query, names, n=1, cutoff=0.6)
    return close[0] if close else None
