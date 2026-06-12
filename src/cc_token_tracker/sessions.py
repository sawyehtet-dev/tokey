"""Multi-session discovery and per-session accounting summaries.

Three layers, all consuming the existing frozen pipeline and reimplementing
none of it:

- :func:`discover_sessions` enumerates ``~/.claude/projects/*/*.jsonl`` and
  returns lightweight per-transcript records (path, project name, mtime),
  newest first, excluding transcripts older than a parameterized window.
- :func:`summarize_session` full-parses one transcript through the existing
  reader/parser/accounting/segmentation/turn-cost pipeline into a
  :class:`SessionSummary`.
- :class:`SessionCache` holds summaries across calls so a non-active session
  is re-parsed only when its ``(mtime, size)`` changes.

Project-name note: the current single-session discovery
(:func:`cc_token_tracker.reader.find_active_transcript`) performs NO decoding
of project directory names -- it resolves paths only, and the renderer shows
just the transcript file basename. "Decoded identically to current discovery"
therefore means the project directory name is carried VERBATIM (e.g.
``-home-saulyehtet-cc-tracker``); no dash-to-slash reconstruction is attempted,
because that decoding does not exist anywhere today and would be lossy to
invent (dashes from path separators and dashes/spaces in real directory names
are indistinguishable).

Context note: the ``context_used`` / ``context_limit`` / ``context_percent``
fields are the :func:`cc_token_tracker.context.estimate_context` estimate --
the last prompt's input-side token total against the documented window of that
record's model. An unknown model or a transcript with no usage-bearing
assistant record yields ``None`` fields, never a fabricated limit or a fake 0.

Dollar-cost semantics are EXACTLY the existing session total's: each turn is
priced by its own model and the dollars summed; an unpriceable token-bearing
turn is left out of the sum and flips ``unpriced``; a zero-token in-flight turn
never flips it. This is guaranteed by calling the same frozen helper the panel
uses (``display._session_cost``), not by reimplementing the rules here.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace

from cc_token_tracker.accounting import account_usage
from cc_token_tracker.context import estimate_context
from cc_token_tracker.display import _session_cost
from cc_token_tracker.reader import read_transcript
from cc_token_tracker.segmentation import segment_turns
from cc_token_tracker.turn_cost import turn_costs

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "SessionRecord",
    "SessionSummary",
    "discover_sessions",
    "summarize_session",
    "SessionCache",
]

# Transcripts whose mtime is older than this many days are excluded from
# discovery. One knob; discover_sessions takes it as a parameter.
DEFAULT_WINDOW_DAYS = 7.0

_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class SessionRecord:
    """One discovered transcript: where it is, whose project, how fresh.

    ``project`` is the project directory name verbatim (current discovery
    decodes nothing; see the module docstring). ``mtime`` is the file's
    modification time at scan, the same recency signal
    ``find_active_transcript`` uses.
    """

    path: str
    project: str
    mtime: float


@dataclass(frozen=True)
class SessionSummary:
    """Full-parse summary of one transcript.

    ``total_tokens`` is ``account_usage(...).session_total`` over the whole
    transcript. ``total_cost_usd``/``unpriced`` carry the existing session
    total's semantics (per-turn pricing by each turn's own model; see module
    docstring) -- when ``unpriced`` is True the dollar figure covers the
    priceable turns only, so callers must render it as partial, never as a
    complete $ total. The three ``context_*`` fields carry the
    :class:`cc_token_tracker.context.ContextEstimate` for the transcript;
    each is ``None`` when not honestly computable (module docstring), and
    ``context_percent`` may exceed 100. ``last_write`` is the transcript's
    mtime when summarized. ``is_active`` marks the single most recently
    modified transcript of a discovery pass.
    """

    project: str
    file_name: str
    total_tokens: int
    total_cost_usd: float
    unpriced: bool
    context_used: int | None
    context_limit: int | None
    context_percent: float | None
    last_write: float
    is_active: bool


def discover_sessions(
    projects_dir: str | None = None,
    *,
    window_days: float = DEFAULT_WINDOW_DAYS,
    now: float | None = None,
) -> list[SessionRecord]:
    """Enumerate ``<projects_dir>/*/*.jsonl``, newest first, within the window.

    ``projects_dir`` defaults to ``~/.claude/projects`` (the same root the
    current discovery scans); tests inject a temp dir. Transcripts with mtime
    older than ``window_days`` days before ``now`` (default: the current time)
    are excluded. A missing or unlistable projects dir yields ``[]``, and any
    per-entry filesystem failure (a file vanishing mid-scan, a permission
    error) skips that entry -- the same never-crash posture as
    ``find_active_transcript``. Ties on mtime sort by path for determinism.
    """
    if projects_dir is None:
        projects_dir = os.path.expanduser("~/.claude/projects")
    if now is None:
        now = time.time()
    cutoff = now - window_days * _SECONDS_PER_DAY

    try:
        project_names = os.listdir(projects_dir)
    except OSError:
        return []

    records: list[SessionRecord] = []
    for project in project_names:
        project_path = os.path.join(projects_dir, project)
        if not os.path.isdir(project_path):
            continue
        try:
            names = os.listdir(project_path)
        except OSError:
            continue  # project dir vanished or unreadable: skip it
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(project_path, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue  # vanished mid-scan or unreadable: skip this entry
            if mtime < cutoff:
                continue
            records.append(SessionRecord(path=path, project=project, mtime=mtime))

    records.sort(key=lambda record: (-record.mtime, record.path))
    return records


def summarize_session(path: str, *, is_active: bool = False) -> SessionSummary | None:
    """Full-parse one transcript into a :class:`SessionSummary`, or ``None``.

    The transcript is read with the existing reader (full re-read, the only
    read mode the pipeline has) and parsed with the existing frozen parser;
    totals come from ``account_usage`` and dollars from the same per-turn
    pricing helper the panel's session total uses. ``None`` means the file
    could not be statted or read -- e.g. deleted between discovery and here --
    and the caller must skip it; this function never raises for filesystem
    reasons.

    ``is_active`` is caller-supplied: whether this transcript is the most
    recently modified one is a fact about the whole discovery pass, not about
    one file, so it is decided upstream (see :class:`SessionCache`).
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    result = read_transcript(path)
    if result.transcript_path is None:
        # Vanished or became unreadable between stat and read.
        return None

    accounting = account_usage(result.records)
    costs = turn_costs(segment_turns(result.records))
    total_cost_usd, unpriced = _session_cost(costs)
    estimate = estimate_context(result.records)

    return SessionSummary(
        project=os.path.basename(os.path.dirname(path)),
        file_name=os.path.basename(path),
        total_tokens=accounting.session_total,
        total_cost_usd=total_cost_usd,
        unpriced=unpriced,
        context_used=estimate.used,
        context_limit=estimate.limit,
        context_percent=estimate.percent,
        last_write=stat.st_mtime,
        is_active=is_active,
    )


class SessionCache:
    """Summaries across calls, re-parsing non-active sessions only on change.

    Each :meth:`summaries` call runs a fresh discovery pass, then builds the
    summary list. The active transcript (the newest of the pass) is always
    re-parsed -- it is the one growing under a live session. A non-active
    transcript is served from cache while its ``(mtime, size)`` is unchanged
    and re-parsed when that key moves.

    Cached entries store ``is_active=False``; the flag describes the CURRENT
    pass, so a transcript that just stopped being active is served from cache
    (its key did not move) already carrying the right flag, and the new active
    one is re-parsed fresh with ``is_active=True``. Entries for paths no
    longer discovered are dropped so a deleted or aged-out transcript does not
    pin memory.
    """

    def __init__(
        self,
        projects_dir: str | None = None,
        *,
        window_days: float = DEFAULT_WINDOW_DAYS,
    ) -> None:
        self._projects_dir = projects_dir
        self._window_days = window_days
        # path -> ((mtime, size), summary-with-is_active-False)
        self._cache: dict[str, tuple[tuple[float, int], SessionSummary]] = {}

    def summaries(self, *, now: float | None = None) -> list[SessionSummary]:
        """One pass: discover, summarize (from cache where valid), return.

        Order matches discovery (newest first). A transcript that vanishes
        between discovery and read is skipped without crashing; if the active
        one vanishes, this pass simply carries no ``is_active`` entry rather
        than promoting a stale second-newest mid-pass.
        """
        records = discover_sessions(
            self._projects_dir, window_days=self._window_days, now=now
        )
        active_path = records[0].path if records else None

        summaries: list[SessionSummary] = []
        seen: set[str] = set()
        for record in records:
            is_active = record.path == active_path
            try:
                stat = os.stat(record.path)
                key: tuple[float, int] | None = (stat.st_mtime, stat.st_size)
            except OSError:
                key = None  # vanished since discovery; the read below decides

            if not is_active and key is not None:
                cached = self._cache.get(record.path)
                if cached is not None and cached[0] == key:
                    summaries.append(cached[1])
                    seen.add(record.path)
                    continue

            summary = summarize_session(record.path, is_active=is_active)
            if summary is None:
                continue  # deleted between discovery and read: skip
            if key is not None:
                self._cache[record.path] = (key, replace(summary, is_active=False))
            summaries.append(summary)
            seen.add(record.path)

        # Drop cache entries no longer discovered (deleted or aged out).
        self._cache = {
            path: entry for path, entry in self._cache.items() if path in seen
        }
        return summaries
