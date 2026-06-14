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

from cc_token_tracker import liveness
from cc_token_tracker.accounting import account_usage
from cc_token_tracker.context import estimate_context
from cc_token_tracker.display import _session_cost, _turn_usd
from cc_token_tracker.markers import OPEN, MarkerInfo, read_markers
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

    ``state`` is an additive, presentation-only liveness label
    (active/closing/dropped from
    :func:`cc_token_tracker.liveness.classify_liveness`). It is NOT frozen here
    at parse time -- liveness drifts with the wall clock while a summary may be
    served unchanged from cache, so the panel assembly recomputes it each
    render from ``last_write`` against the current ``now`` (see
    :func:`cc_token_tracker.roster.build_roster_view`). The default is a
    placeholder the render pass always overwrites.

    The four ``last_*`` fields are this session's most recent turn -- the
    in-flight one once it has started streaming usage, otherwise the last
    completed turn (see :func:`summarize_session`) -- the presentation data for
    the all-expanded roster block's ``Last:`` line, which therefore updates live
    while a prompt runs. Every session (not just the auto-followed one) renders
    its own block, so each carries its own figures straight from the parse rather
    than from a single live ``Frame``. They reuse the frozen pricing/turn output
    verbatim: ``last_input_tokens`` folds cache-creation into input just like the
    hero's IN cell, ``last_output_tokens`` is the turn's output,
    ``last_cache_read_tokens`` its cache-read, and ``last_cost_usd`` comes from
    :func:`cc_token_tracker.display._turn_usd` (``None`` when the model is
    unpriceable). All four are ``None`` when the transcript has no usable turn
    yet, which the renderer shows honestly.

    The three ``sum_*`` fields are the SESSION-WIDE totals for the roster block's
    ``Sum:`` line, broken down the same way ``Last:`` is: ``sum_input_tokens``
    folds cache-creation into input, ``sum_output_tokens`` is total output, and
    ``sum_cache_read_tokens`` is total cache-read, all from the one
    ``account_usage`` pass over the whole transcript. The matching dollar figure
    is ``total_cost_usd`` (with ``unpriced`` flagging a partial total).

    ``marker_event`` / ``marker_ts`` carry this session's latest hook marker
    (:mod:`cc_token_tracker.markers`) -- ``"SessionStart"``/``"SessionEnd"`` and
    its timestamp, or ``None`` when no marker exists. They are NOT frozen at parse
    time: a marker can flip open->closed with no transcript byte change, so
    :class:`SessionCache` re-attaches them FRESH each tick (even on cache-served
    summaries) and the render pass classifies liveness from them via
    :func:`cc_token_tracker.liveness.classify_with_marker`.
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
    state: str = liveness.ACTIVE
    # The session's real working directory (from the transcript's ``cwd``, or the
    # marker for a not-yet-written session); ``None`` when unknown. The roster
    # renders a ``~``-relative title from it, falling back to ``project``.
    cwd: str | None = None
    last_cost_usd: float | None = None
    last_input_tokens: int | None = None
    last_output_tokens: int | None = None
    last_cache_read_tokens: int | None = None
    sum_input_tokens: int = 0
    sum_output_tokens: int = 0
    sum_cache_read_tokens: int = 0
    marker_event: str | None = None
    marker_ts: float | None = None


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

    # The session's most recent turn, for the roster block's "Last:" line. We
    # prefer the TRAILING turn once it carries usage -- that is the in-flight
    # turn while a prompt runs, so "Last:" updates live as records stream rather
    # than only when the turn completes. When the tail is just a typed prompt
    # with no usage yet (turn_total == 0), we fall back to the last completed
    # turn so an idle prompt does not blank the line with zeros. Reads the frozen
    # turn output and prices via the frozen table; folds cache-creation into
    # input exactly like the hero's IN cell.
    last = None
    if costs:
        trailing = costs[-1]
        if trailing.turn_total > 0:
            last = trailing
        else:
            last = next((cost for cost in reversed(costs) if cost.complete), None)
    if last is not None:
        last_input_tokens = last.input_tokens + last.cache_creation_input_tokens
        last_output_tokens = last.output_tokens
        last_cache_read_tokens = last.cache_read_input_tokens
        last_cost_usd = _turn_usd(last)
    else:
        last_cost_usd = last_input_tokens = None
        last_output_tokens = last_cache_read_tokens = None

    # Session-wide totals for the "Sum:" line, broken down like "Last:": IN folds
    # cache-creation into input, from the one account_usage pass above.
    sum_input_tokens = (
        accounting.total_input_tokens
        + accounting.total_cache_creation_input_tokens
    )

    # The session's real working directory, from the first record that carries
    # one. Used for a readable title; the on-disk project dir name is a lossy
    # dash-encoding of this path.
    cwd = next((record.cwd for record in result.records if record.cwd), None)

    return SessionSummary(
        project=os.path.basename(os.path.dirname(path)),
        file_name=os.path.basename(path),
        cwd=cwd,
        total_tokens=accounting.session_total,
        total_cost_usd=total_cost_usd,
        unpriced=unpriced,
        context_used=estimate.used,
        context_limit=estimate.limit,
        context_percent=estimate.percent,
        last_write=stat.st_mtime,
        is_active=is_active,
        last_cost_usd=last_cost_usd,
        last_input_tokens=last_input_tokens,
        last_output_tokens=last_output_tokens,
        last_cache_read_tokens=last_cache_read_tokens,
        sum_input_tokens=sum_input_tokens,
        sum_output_tokens=accounting.total_output_tokens,
        sum_cache_read_tokens=accounting.total_cache_read_input_tokens,
    )


def _synthesize_summary(marker: MarkerInfo) -> SessionSummary:
    """A placeholder summary for an OPEN session whose transcript does not exist.

    Claude Code writes a session's transcript only at its first prompt, but the
    SessionStart hook drops an OPEN marker immediately, carrying the transcript
    path it WILL use. This turns that marker into a zero-figure block so a
    brand-new session shows in the roster at once ("no completed turn yet"),
    before there is anything to parse. Project and file name come from the marker
    path exactly as the transcript-backed discovery decodes them; ``last_write``
    is the marker timestamp so liveness and recency have a stamp to use. The
    marker fields themselves are attached by :meth:`SessionCache.summaries`.
    """
    return SessionSummary(
        project=os.path.basename(os.path.dirname(marker.transcript_path)),
        file_name=os.path.basename(marker.transcript_path),
        cwd=marker.cwd or None,
        total_tokens=0,
        total_cost_usd=0.0,
        unpriced=False,
        context_used=None,
        context_limit=None,
        context_percent=None,
        last_write=marker.ts,
        is_active=False,
    )


class SessionCache:
    """Summaries across calls, re-parsing changed transcripts only.

    Each :meth:`summaries` call runs a fresh discovery pass and reads the session
    markers, then builds the summary list. The newest transcript of the pass is
    always re-parsed (it is the one growing under a live session); any other
    transcript is served from cache while its ``(mtime, size)`` is unchanged and
    re-parsed when that key moves -- so a second concurrently-running session is
    re-parsed too, its key moving every tick. Entries for paths no longer
    discovered are dropped so a deleted or aged-out transcript does not pin
    memory.

    Two things are decided FRESH each pass, never from cache, and stamped onto
    every returned summary (cached or freshly parsed):

    - the hook marker (``marker_event`` / ``marker_ts``), because a marker can
      flip open->closed with no transcript byte change, so a cached summary's
      liveness must still see the current marker; and
    - ``is_active``, the single auto-followed (``▶``) session, the newest by
      recency (``max(last_write, marker_ts)``) among the non-closed ones.

    Markers also ADD sessions to the pass: an OPEN marker whose transcript does
    not exist yet (a session before its first prompt) is synthesized into a
    zero-figure block so it shows immediately.
    """

    def __init__(
        self,
        projects_dir: str | None = None,
        *,
        window_days: float = DEFAULT_WINDOW_DAYS,
        markers_dir: str | None = None,
    ) -> None:
        self._projects_dir = projects_dir
        self._window_days = window_days
        self._markers_dir = markers_dir
        # path -> ((mtime, size), summary); the cached summary carries no live
        # is_active / marker fields -- those are re-stamped fresh each pass.
        self._cache: dict[str, tuple[tuple[float, int], SessionSummary]] = {}

    def summaries(self, *, now: float | None = None) -> list[SessionSummary]:
        """One pass: discover, read markers, summarize, stamp, order. Never raises.

        Returns the roster summaries newest-first by recency (the later of the
        transcript mtime and the marker timestamp), each stamped with its fresh
        marker and the single ``is_active`` flag. A transcript that vanishes
        between discovery and read is skipped without crashing.
        """
        records = discover_sessions(
            self._projects_dir, window_days=self._window_days, now=now
        )
        markers = read_markers(self._markers_dir, now=now)
        newest_transcript = records[0].path if records else None
        discovered_paths = {record.path for record in records}

        # (transcript_path, summary) so markers and recency can be matched by
        # path after the parse; summaries are stored without live fields.
        built: list[tuple[str, SessionSummary]] = []
        seen: set[str] = set()
        for record in records:
            force = record.path == newest_transcript
            try:
                stat = os.stat(record.path)
                key: tuple[float, int] | None = (stat.st_mtime, stat.st_size)
            except OSError:
                key = None  # vanished since discovery; the read below decides

            if not force and key is not None:
                cached = self._cache.get(record.path)
                if cached is not None and cached[0] == key:
                    built.append((record.path, cached[1]))
                    seen.add(record.path)
                    continue

            summary = summarize_session(record.path)
            if summary is None:
                continue  # deleted between discovery and read: skip
            if key is not None:
                self._cache[record.path] = (key, summary)
            built.append((record.path, summary))
            seen.add(record.path)

        # Synthesize a block for every OPEN marker whose transcript has not been
        # created yet -- a brand-new session before its first prompt.
        for transcript_path, marker in markers.items():
            if marker.event == OPEN and transcript_path not in discovered_paths:
                built.append((transcript_path, _synthesize_summary(marker)))

        # Drop cache entries no longer discovered (deleted or aged out).
        self._cache = {
            path: entry for path, entry in self._cache.items() if path in seen
        }

        return self._stamp_and_order(built, markers)

    @staticmethod
    def _stamp_and_order(
        built: list[tuple[str, SessionSummary]],
        markers: dict[str, MarkerInfo],
    ) -> list[SessionSummary]:
        """Attach fresh marker + is_active fields and order newest-first.

        Recency is ``max(last_write, marker_ts)``; the auto-followed (``▶``)
        session is the newest one that is not closed, so an exited session never
        keeps the marker. Ties break by path for determinism, matching discovery.
        """

        def recency(path: str, summary: SessionSummary) -> float:
            marker = markers.get(path)
            stamps = [summary.last_write]
            if marker is not None:
                stamps.append(marker.ts)
            return max(stamps)

        active_path: str | None = None
        active_recency = float("-inf")
        for path, summary in built:
            marker = markers.get(path)
            if marker is not None and marker.event != OPEN:
                continue  # a closed session is dropped; never auto-followed
            score = recency(path, summary)
            if score > active_recency:
                active_recency = score
                active_path = path

        ordered = sorted(
            built, key=lambda item: (-recency(*item), item[0])
        )
        result: list[SessionSummary] = []
        for path, summary in ordered:
            marker = markers.get(path)
            result.append(
                replace(
                    summary,
                    is_active=(path == active_path),
                    marker_event=(marker.event if marker is not None else None),
                    marker_ts=(marker.ts if marker is not None else None),
                )
            )
        return result
