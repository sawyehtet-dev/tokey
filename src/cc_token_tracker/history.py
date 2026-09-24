"""Durable per-session spend history: the one thing the roster cannot remember.

The roster is a LIVE view over a 7-day window of transcripts. Claude Code
rotates those transcripts away, so "what did last month cost" is unanswerable
from them. This module is the only part of tokey that writes state: one row per
finished session, keyed on the session id, in a stdlib ``sqlite3`` database.

Consumes, never recomputes. Every figure stored here comes verbatim off a
frozen :class:`cc_token_tracker.sessions.SessionSummary` -- no re-pricing, no
re-summing. The pipeline rule in CLAUDE.md holds: this is another consumer of
``SessionSummary``, not a second accounting path.

Two writers, deliberately:

- ``tokey-hook`` on SessionEnd, the primary path (:mod:`cc_token_tracker.hook`);
- the roster at startup, which backfills every transcript still on disk
  (:func:`backfill_on_disk`) -- a session killed by a crash, ``kill -9``, or a
  closed terminal never fires SessionEnd, and those are exactly the ones worth
  not losing. It scans past the roster's 7-day window on purpose: Claude Code
  keeps transcripts for weeks, and the ones the live view has already dropped
  are precisely the ones history exists for.

Both funnel through :func:`record_session`, whose UPSERT on ``session_id``
makes the double-write a no-op and makes a later write self-correcting: a
session recorded mid-flight by backfill is refreshed with its final figures the
next time either writer sees it.

``unpriced`` is carried forward per row so an aggregate containing an
unpriceable turn can render ``$123.45+`` rather than a clean lie, the same
honesty contract the roster blocks keep.

Never raises. History is a side effect of watching, not the product: a locked
database, a read-only home directory, or a corrupt file degrades tokey to its
live view rather than taking it down, and the hook contract forbids raising into
Claude Code at all.
"""

from __future__ import annotations

import math
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import closing, suppress
from dataclasses import dataclass

from cc_token_tracker.sessions import (
    SessionSummary,
    discover_sessions,
    summarize_session,
)

__all__ = [
    "DayTotal",
    "MonthTotal",
    "ProjectTotal",
    "backfill",
    "backfill_on_disk",
    "daily_totals",
    "default_db_path",
    "monthly_totals",
    "project_totals",
    "record_session",
    "session_id_of",
]

# Busy timeout for the write lock. Several sessions can end at the same moment
# (closing a terminal with tabs open), so a writer waits rather than dropping
# the row; 5s is far beyond a single-row UPSERT and still cannot hang a hook
# long enough to be noticed.
_BUSY_TIMEOUT_SECONDS = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    project           TEXT NOT NULL,
    cwd               TEXT,
    ended_at          REAL NOT NULL,
    total_tokens      INTEGER NOT NULL,
    input_tokens      INTEGER NOT NULL,
    output_tokens     INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cost_usd          REAL NOT NULL,
    unpriced          INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_ended_at ON sessions (ended_at);
"""

_UPSERT = """
INSERT INTO sessions (
    session_id, project, cwd, ended_at, total_tokens, input_tokens,
    output_tokens, cache_read_tokens, cost_usd, unpriced, cache_write_tokens
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    project = excluded.project,
    cwd = excluded.cwd,
    ended_at = excluded.ended_at,
    total_tokens = excluded.total_tokens,
    input_tokens = excluded.input_tokens,
    output_tokens = excluded.output_tokens,
    cache_read_tokens = excluded.cache_read_tokens,
    cost_usd = excluded.cost_usd,
    unpriced = excluded.unpriced,
    cache_write_tokens = excluded.cache_write_tokens
"""


@dataclass(frozen=True)
class DayTotal:
    """One local calendar day's spend. ``unpriced`` flags a partial dollar sum."""

    day: str
    sessions: int
    total_tokens: int
    cost_usd: float
    unpriced: bool


@dataclass(frozen=True)
class MonthTotal:
    """One local calendar month's spend (``YYYY-MM``). Same partial-sum contract."""

    month: str
    sessions: int
    total_tokens: int
    cost_usd: float
    unpriced: bool


@dataclass(frozen=True)
class ProjectTotal:
    """One project's spend over the queried span. Same partial-sum contract.

    ``label`` is the human name and the grouping key: the repository a session
    ran in (see :func:`project_label`), so a repo's git worktrees fold into the
    repo and throwaway temp-dir sessions fold into one row. ``projects`` lists
    the transcript directory names merged into the row (e.g.
    ``-home-saulyehtet-cc-tracker``), most expensive first, so the display name
    is never mistaken for the identity.
    """

    label: str
    sessions: int
    total_tokens: int
    cost_usd: float
    unpriced: bool
    projects: tuple[str, ...]


def default_db_path() -> str:
    """``~/.claude/tokey/history.db``, tokey's own directory beside the markers."""
    return os.path.expanduser(os.path.join("~", ".claude", "tokey", "history.db"))


def session_id_of(summary: SessionSummary) -> str | None:
    """The session id a summary belongs to, or ``None`` when it has no usable one.

    Claude Code names each transcript ``<session_id>.jsonl``, so the id is the
    file name with its suffix removed. Deriving it here rather than taking it
    from the hook payload is what lets the roster backfill sessions whose hook
    never fired.
    """
    name = summary.file_name
    if not name.endswith(".jsonl"):
        return None
    stem = name[: -len(".jsonl")]
    return stem or None


def _connect(db_path: str | None) -> sqlite3.Connection:
    """Open (creating parents and schema) the history database. May raise."""
    path = db_path if db_path is not None else default_db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS)
    # WAL lets the roster's reads run while a hook writes, instead of either
    # side blocking the other.
    with suppress(sqlite3.DatabaseError):
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(_SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Bring a database made by an older tokey up to the current schema.

    v0.8.0 databases predate ``cache_write_tokens``. The column is added with a
    0 default; the next backfill rewrites every row whose transcript is still on
    disk, and there ``input_tokens`` also changes meaning, from input with cache
    writes folded in to uncached input only. A row whose transcript is gone keeps
    its old figures; its ``total_tokens`` and cost were right all along.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    if "cache_write_tokens" not in columns:
        connection.execute(
            "ALTER TABLE sessions ADD COLUMN cache_write_tokens "
            "INTEGER NOT NULL DEFAULT 0"
        )


def record_session(summary: SessionSummary, *, db_path: str | None = None) -> bool:
    """UPSERT one session's totals. Return whether a row was written.

    Every figure is taken verbatim off ``summary``; nothing is recomputed. A
    summary with no derivable session id is skipped. Never raises: any
    filesystem or sqlite failure returns False, because neither the hook nor the
    panel may die over a history write.
    """
    session_id = session_id_of(summary)
    if session_id is None:
        return False
    try:
        with closing(_connect(db_path)) as connection, connection:
            connection.execute(
                _UPSERT,
                (
                    session_id,
                    summary.project,
                    summary.cwd,
                    summary.last_write,
                    summary.total_tokens,
                    summary.sum_input_tokens,
                    summary.sum_output_tokens,
                    summary.sum_cache_read_tokens,
                    summary.total_cost_usd,
                    int(summary.unpriced),
                    summary.sum_cache_write_tokens,
                ),
            )
    except (sqlite3.Error, OSError):
        return False
    return True


def backfill(
    summaries: Iterable[SessionSummary], *, db_path: str | None = None
) -> int:
    """Record every summary, returning how many rows were written.

    The roster's startup path. Writing sessions that are still running is
    intentional and safe: the UPSERT refreshes the row the next time either
    writer sees the session, so a mid-flight row self-corrects rather than
    freezing a partial total.
    """
    return sum(record_session(s, db_path=db_path) for s in summaries)


def backfill_on_disk(
    projects_dir: str | None = None, *, db_path: str | None = None
) -> int:
    """Record every transcript still on disk, returning how many rows were written.

    No age window: whatever Claude Code has not yet rotated away is recorded, so
    a session the hook missed is recoverable for as long as its transcript
    survives. Re-recording an unchanged session is a no-op UPSERT.

    ponytail: re-summarizes every transcript each startup (~1s for ~450MB on
    disk), which is why the roster runs it off the render thread. Skip rows
    whose ``ended_at`` already matches the transcript mtime if it ever gets slow.
    """
    return backfill(
        (
            summary
            for record in discover_sessions(projects_dir, window_days=math.inf)
            if (summary := summarize_session(record.path)) is not None
        ),
        db_path=db_path,
    )


def _query(sql: str, params: tuple, db_path: str | None) -> list[tuple]:
    """Run one read-only aggregate. Never raises; a failure reads as empty."""
    try:
        with closing(_connect(db_path)) as connection:
            return list(connection.execute(sql, params))
    except (sqlite3.Error, OSError):
        return []


_DAILY = """
SELECT date(ended_at, 'unixepoch', 'localtime') AS day,
       COUNT(*), SUM(total_tokens), SUM(cost_usd), MAX(unpriced)
FROM sessions
GROUP BY day
ORDER BY day DESC
LIMIT ?
"""

_MONTHLY = """
SELECT strftime('%Y-%m', ended_at, 'unixepoch', 'localtime') AS month,
       COUNT(*), SUM(total_tokens), SUM(cost_usd), MAX(unpriced)
FROM sessions
GROUP BY month
ORDER BY month DESC
"""

_BY_PROJECT = """
SELECT project, COUNT(*), SUM(total_tokens), SUM(cost_usd), MAX(unpriced),
       MAX(cwd)
FROM sessions
GROUP BY project
ORDER BY SUM(cost_usd) DESC
"""

# Throwaway sessions (a tool running Claude Code in a scratch dir) are grouped
# into one row under this label rather than listed one random dir name each.
TEMP_LABEL = "(temp dirs)"


def daily_totals(*, limit: int = 30, db_path: str | None = None) -> list[DayTotal]:
    """The most recent ``limit`` local calendar days that have sessions, newest first.

    Days are bucketed in LOCAL time (``ended_at`` is a Unix timestamp), so a day
    here matches the day you worked, not UTC. Days with no sessions are absent
    rather than zero-filled.
    """
    return [
        DayTotal(day, sessions, tokens or 0, cost or 0.0, bool(unpriced))
        for day, sessions, tokens, cost, unpriced in _query(
            _DAILY, (limit,), db_path
        )
    ]


def monthly_totals(*, db_path: str | None = None) -> list[MonthTotal]:
    """Every local calendar month that has sessions, newest first.

    Whole months straight from the database, so the oldest month is never cut
    short the way a sum over the last N days would be.
    """
    return [
        MonthTotal(month, sessions, tokens or 0, cost or 0.0, bool(unpriced))
        for month, sessions, tokens, cost, unpriced in _query(
            _MONTHLY, (), db_path
        )
    ]


def project_label(cwd: str | None, project: str) -> str:
    """The repository a session belongs to, as a short human name.

    - a cwd inside the system temp dir is a throwaway: :data:`TEMP_LABEL`;
    - a cwd inside a git worktree resolves to the repo it was cut from, for the
      three layouts in use: ``~/.t3/worktrees/<repo>/<name>`` (T3 Code),
      ``<repo>-worktrees/<name>`` (the ``wt`` command), and
      ``<repo>/.claude/worktrees/<name>`` (Claude Code);
    - anything else is the cwd's base name, and with no cwd the raw project key.

    Pure path parsing on purpose: worktrees are usually deleted long before
    anyone reads the history, so asking git is not an option.
    ponytail: layouts are hard-coded; another tool's worktrees show their own
    dir name until its layout is added here.
    """
    if not cwd:
        return project
    path = os.path.normpath(cwd)
    temp = os.path.normpath(tempfile.gettempdir())
    if path == temp or path.startswith(temp + os.sep):
        return TEMP_LABEL
    parts = path.split(os.sep)
    for i in range(len(parts) - 1, -1, -1):
        part = parts[i]
        if part.endswith("-worktrees") and part != "-worktrees":
            return part.removesuffix("-worktrees")
        if part == "worktrees":
            if i > 0 and parts[i - 1] == ".claude":
                if i > 1 and parts[i - 2]:
                    return parts[i - 2]
            elif i + 1 < len(parts):
                return parts[i + 1]
    return os.path.basename(path) or project


def project_totals(
    *, limit: int = 20, db_path: str | None = None
) -> list[ProjectTotal]:
    """All-time spend per repository (see :func:`project_label`), most expensive
    first, at most ``limit`` rows."""
    groups: dict[str, list[tuple]] = {}
    for row in _query(_BY_PROJECT, (), db_path):
        groups.setdefault(project_label(row[5], row[0]), []).append(row)
    totals = [
        ProjectTotal(
            label=label,
            sessions=sum(row[1] for row in rows),
            total_tokens=sum(row[2] or 0 for row in rows),
            cost_usd=sum(row[3] or 0.0 for row in rows),
            unpriced=any(row[4] for row in rows),
            projects=tuple(row[0] for row in rows),
        )
        for label, rows in groups.items()
    ]
    totals.sort(key=lambda total: total.cost_usd, reverse=True)
    return totals[:limit]
