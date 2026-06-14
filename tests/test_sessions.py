"""Tests for cc_token_tracker.sessions (multi-session discovery and summaries).

Uses real temp directories shaped like ~/.claude/projects (project dirs holding
*.jsonl transcripts), with mtimes pinned via os.utime so recency and the
discovery window are deterministic. No frozen module is touched; transcripts
are parsed by the real pipeline.
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from cc_token_tracker import markers as markers_mod
from cc_token_tracker import sessions
from cc_token_tracker.markers import CLOSED, OPEN, write_marker
from cc_token_tracker.roster import build_roster_view
from cc_token_tracker.sessions import (
    SessionCache,
    discover_sessions,
    summarize_session,
)

# A genuine typed prompt: opens a turn, carries no usage.
PROMPT = '{"type":"user","message":{"role":"user","content":"hi"}}'

# Rates (per MTok) from the pricing table, used to compute expected dollars:
# claude-opus-4-8 input $5 / output $25; claude-sonnet-4-6 input $3 / output $15.
OPUS = "claude-opus-4-8"
SONNET = "claude-sonnet-4-6"
UNKNOWN = "totally-unknown-model-9000"


def assistant_line(message_id, model=None, input_tokens=0, output_tokens=0,
                   stop_reason="end_turn"):
    message = {
        "id": message_id,
        "role": "assistant",
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    if model is not None:
        message["model"] = model
    return json.dumps({"type": "assistant", "message": message})


def turn(message_id, model, input_tokens=1000, output_tokens=1000):
    """One complete turn: a typed prompt plus its closing assistant line."""
    return [
        PROMPT,
        assistant_line(message_id, model=model, input_tokens=input_tokens,
                       output_tokens=output_tokens),
    ]


def usage_assistant(message_id, model=OPUS, input_tokens=0, output_tokens=0,
                    cache_creation=0, cache_read=0, stop_reason="end_turn"):
    """An assistant line carrying full usage (incl. cache), any stop_reason.

    A non-``end_turn`` stop_reason (e.g. ``tool_use``) leaves the turn in-flight,
    which the real-time ``Last Prompt:`` pick keys on.
    """
    return json.dumps({"type": "assistant", "message": {
        "id": message_id, "role": "assistant", "model": model,
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cache_creation_input_tokens": cache_creation,
                  "cache_read_input_tokens": cache_read}}})


class SessionsBase(unittest.TestCase):
    """Shared fixture: a temp projects dir plus transcript-writing helpers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = self.tmp.name
        # Isolate the marker store: a separate temp dir, patched in as the
        # default so SessionCache(...) with no markers_dir never reads the real
        # ~/.claude store. Empty by default -> summaries behave as pre-marker.
        self.markers_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.markers_tmp.cleanup)
        self.markers = self.markers_tmp.name
        patcher = mock.patch.object(
            markers_mod, "DEFAULT_MARKERS_DIR", self.markers
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = time.time()

    def write_marker(self, project, name, event, age_seconds=0.0):
        """Write a session marker for <projects>/<project>/<name>, ts pinned to
        now - age_seconds. The transcript file itself need not exist."""
        transcript_path = os.path.join(self.projects, project, name)
        write_marker(f"{project}-{name}", transcript_path, self.projects, event,
                     markers_dir=self.markers, now=self.now - age_seconds)
        return transcript_path

    def write_transcript(self, project, name, lines, age_seconds=0.0):
        """Create <projects>/<project>/<name> holding lines, mtime pinned to
        now - age_seconds. Returns the path."""
        project_dir = os.path.join(self.projects, project)
        os.makedirs(project_dir, exist_ok=True)
        path = os.path.join(project_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        stamp = self.now - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def discover(self, **kwargs):
        kwargs.setdefault("now", self.now)
        return discover_sessions(self.projects, **kwargs)


class DiscoverSessions(SessionsBase):
    def test_sorted_newest_first_across_project_dirs(self):
        oldest = self.write_transcript("proj-a", "s1.jsonl", [PROMPT],
                                       age_seconds=300)
        newest = self.write_transcript("proj-b", "s2.jsonl", [PROMPT],
                                       age_seconds=10)
        middle = self.write_transcript("proj-c", "s3.jsonl", [PROMPT],
                                       age_seconds=100)

        records = self.discover()

        self.assertEqual([r.path for r in records], [newest, middle, oldest])
        # Project names are the directory names verbatim (current discovery
        # decodes nothing).
        self.assertEqual([r.project for r in records],
                         ["proj-b", "proj-c", "proj-a"])

    def test_two_transcripts_in_one_project_yield_two_entries(self):
        first = self.write_transcript("proj-a", "s1.jsonl", [PROMPT],
                                      age_seconds=100)
        second = self.write_transcript("proj-a", "s2.jsonl", [PROMPT],
                                       age_seconds=10)

        records = self.discover()

        self.assertEqual([r.path for r in records], [second, first])
        self.assertEqual([r.project for r in records], ["proj-a", "proj-a"])

    def test_empty_projects_directory(self):
        self.assertEqual(self.discover(), [])

    def test_missing_projects_directory(self):
        missing = os.path.join(self.projects, "does-not-exist")
        self.assertEqual(discover_sessions(missing, now=self.now), [])

    def test_window_excludes_old_files_and_is_parameterized(self):
        fresh = self.write_transcript("proj-a", "fresh.jsonl", [PROMPT],
                                      age_seconds=10)
        stale = self.write_transcript("proj-a", "stale.jsonl", [PROMPT],
                                      age_seconds=8 * 86400)

        within_default = self.discover()
        self.assertEqual([r.path for r in within_default], [fresh])

        widened = self.discover(window_days=30)
        self.assertEqual([r.path for r in widened], [fresh, stale])

    def test_non_jsonl_and_stray_files_ignored(self):
        self.write_transcript("proj-a", "notes.txt", ["not a transcript"],
                              age_seconds=5)
        # A stray file directly under the projects root is not a project dir.
        stray = os.path.join(self.projects, "stray.jsonl")
        with open(stray, "w", encoding="utf-8") as handle:
            handle.write(PROMPT + "\n")
        real = self.write_transcript("proj-a", "real.jsonl", [PROMPT],
                                     age_seconds=10)

        records = self.discover()
        self.assertEqual([r.path for r in records], [real])

    def test_mtime_carried_on_record(self):
        path = self.write_transcript("proj-a", "s1.jsonl", [PROMPT],
                                     age_seconds=50)
        (record,) = self.discover()
        self.assertEqual(record.path, path)
        self.assertAlmostEqual(record.mtime, self.now - 50, places=3)


class SummarizeSession(SessionsBase):
    def test_totals_and_per_model_cost(self):
        # Two completed turns on different models: each priced by its OWN model.
        lines = turn("m1", OPUS) + turn("m2", SONNET)
        path = self.write_transcript("proj-a", "s1.jsonl", lines)

        summary = summarize_session(path)

        self.assertIsNotNone(summary)
        self.assertEqual(summary.project, "proj-a")
        self.assertEqual(summary.file_name, "s1.jsonl")
        self.assertEqual(summary.total_tokens, 4000)  # 2 turns x (1000 in + 1000 out)
        # opus: 1000*5/1e6 + 1000*25/1e6 = 0.030; sonnet: 0.003 + 0.015 = 0.018
        self.assertAlmostEqual(summary.total_cost_usd, 0.048)
        self.assertFalse(summary.unpriced)
        self.assertFalse(summary.is_active)
        self.assertAlmostEqual(summary.last_write, os.path.getmtime(path),
                               places=6)

    def test_is_active_flag_passed_through(self):
        path = self.write_transcript("proj-a", "s1.jsonl", turn("m1", OPUS))
        self.assertTrue(summarize_session(path, is_active=True).is_active)
        self.assertFalse(summarize_session(path).is_active)

    def test_unknown_model_session_is_unpriced_not_zero_dollar_claim(self):
        # Every turn unknown: the dollar figure alone would read $0.00, so the
        # unpriced flag MUST be set (the figure is partial, not a real total).
        path = self.write_transcript("proj-a", "s1.jsonl", turn("m1", UNKNOWN))

        summary = summarize_session(path)

        self.assertTrue(summary.unpriced)
        self.assertEqual(summary.total_cost_usd, 0.0)
        self.assertEqual(summary.total_tokens, 2000)  # tokens still counted

    def test_unpriced_propagates_alongside_priced_turns(self):
        lines = turn("m1", OPUS) + turn("m2", UNKNOWN)
        path = self.write_transcript("proj-a", "s1.jsonl", lines)

        summary = summarize_session(path)

        self.assertTrue(summary.unpriced)
        # Only the priceable turn is summed; the unknown one is excluded, not $0.
        self.assertAlmostEqual(summary.total_cost_usd, 0.030)

    def test_zero_token_in_flight_turn_does_not_set_unpriced(self):
        # A trailing typed prompt with no assistant usage yet: same exception
        # as the panel's session total (no flag flash on every new prompt).
        lines = turn("m1", OPUS) + [PROMPT]
        path = self.write_transcript("proj-a", "s1.jsonl", lines)

        summary = summarize_session(path)

        self.assertFalse(summary.unpriced)
        self.assertAlmostEqual(summary.total_cost_usd, 0.030)

    def test_context_fields_carry_the_estimate(self):
        # Replaces the v0.4 test that pinned these fields to None (the one
        # sanctioned existing-test change of ticket v0.5.0-T2): the fields now
        # carry estimate_context's figures from the MOST RECENT usage-bearing
        # assistant record.
        last = json.dumps({
            "type": "assistant",
            "message": {
                "id": "m2", "role": "assistant", "model": OPUS,
                "content": [{"type": "text", "text": "x"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 1000, "output_tokens": 500,
                    "cache_creation_input_tokens": 4000,
                    "cache_read_input_tokens": 95000,
                },
            },
        })
        lines = turn("m1", OPUS) + [PROMPT, last]
        path = self.write_transcript("proj-a", "s1.jsonl", lines)

        summary = summarize_session(path)

        self.assertEqual(summary.context_used, 100_000)  # 1000 + 4000 + 95000
        self.assertEqual(summary.context_limit, 1_000_000)
        self.assertAlmostEqual(summary.context_percent, 10.0)

    def test_context_limit_none_for_unknown_model_never_guessed(self):
        path = self.write_transcript("proj-a", "s1.jsonl", turn("m1", UNKNOWN))

        summary = summarize_session(path)

        self.assertEqual(summary.context_used, 1000)  # input side still counted
        self.assertIsNone(summary.context_limit)
        self.assertIsNone(summary.context_percent)

    def test_deleted_file_returns_none(self):
        path = self.write_transcript("proj-a", "s1.jsonl", turn("m1", OPUS))
        os.remove(path)
        self.assertIsNone(summarize_session(path))

    def test_last_turn_figures_fold_cache_creation_and_carry_cache_read(self):
        # The v0.6 roster block's "Last Prompt:" line: IN folds cache-creation into
        # input, CACHE is the read, OUT is output, priced via the frozen table.
        asst = json.dumps({"type": "assistant", "message": {
            "id": "m1", "role": "assistant",
            "content": [{"type": "text", "text": "x"}],
            "stop_reason": "end_turn", "model": OPUS,
            "usage": {"input_tokens": 1000, "output_tokens": 500,
                      "cache_creation_input_tokens": 300,
                      "cache_read_input_tokens": 4000}}})
        path = self.write_transcript("proj-a", "s1.jsonl", [PROMPT, asst])

        summary = summarize_session(path)

        self.assertEqual(summary.last_input_tokens, 1300)  # 1000 + 300 creation
        self.assertEqual(summary.last_output_tokens, 500)
        self.assertEqual(summary.last_cache_read_tokens, 4000)
        self.assertIsNotNone(summary.last_cost_usd)

    def test_last_turn_is_the_most_recent_completed_turn(self):
        path = self.write_transcript(
            "proj-a", "s1.jsonl",
            turn("m1", OPUS, input_tokens=1000, output_tokens=1000)
            + turn("m2", OPUS, input_tokens=2000, output_tokens=500),
        )

        summary = summarize_session(path)

        self.assertEqual(summary.last_input_tokens, 2000)  # the m2 turn, not m1
        self.assertEqual(summary.last_output_tokens, 500)

    def test_last_turn_cost_none_for_unpriceable_model_tokens_still_carried(self):
        path = self.write_transcript("proj-a", "s1.jsonl", turn("m1", UNKNOWN))

        summary = summarize_session(path)

        self.assertIsNone(summary.last_cost_usd)        # renders "$?"
        self.assertEqual(summary.last_output_tokens, 1000)  # turn still exists

    def test_last_turn_all_none_without_a_completed_turn(self):
        path = self.write_transcript("proj-a", "s1.jsonl", [PROMPT])

        summary = summarize_session(path)

        self.assertIsNone(summary.last_cost_usd)
        self.assertIsNone(summary.last_input_tokens)
        self.assertIsNone(summary.last_output_tokens)
        self.assertIsNone(summary.last_cache_read_tokens)


class CacheAndActiveFlag(SessionsBase):
    def test_active_flag_marks_only_the_newest(self):
        self.write_transcript("proj-a", "old.jsonl", turn("m1", OPUS),
                              age_seconds=200)
        self.write_transcript("proj-b", "new.jsonl", turn("m2", OPUS),
                              age_seconds=10)

        summaries = SessionCache(self.projects).summaries(now=self.now)

        by_name = {s.file_name: s for s in summaries}
        self.assertEqual([s.file_name for s in summaries],
                         ["new.jsonl", "old.jsonl"])
        self.assertTrue(by_name["new.jsonl"].is_active)
        self.assertFalse(by_name["old.jsonl"].is_active)

    def test_newly_started_session_appears_on_next_pass_no_restart(self):
        # Acceptance for v0.6.0 CHANGE 2: discovery re-globs on every
        # summaries() call, so a session started AFTER tokey is already running
        # shows up on the next refresh tick -- no restart, the SAME cache
        # object. Mirrors run(), which holds one SessionCache and calls
        # summaries() once per ~1s tick.
        self.write_transcript("proj-a", "first.jsonl", turn("m1", OPUS),
                              age_seconds=100)
        cache = SessionCache(self.projects)

        first = cache.summaries(now=self.now)
        self.assertEqual([s.file_name for s in first], ["first.jsonl"])

        # A brand-new session starts while tokey keeps running (newest active).
        self.write_transcript("proj-b", "fresh.jsonl", turn("m2", OPUS),
                              age_seconds=1)
        second = cache.summaries(now=self.now)

        by_name = {s.file_name: s for s in second}
        self.assertIn("fresh.jsonl", by_name)        # appeared, no restart
        self.assertTrue(by_name["fresh.jsonl"].is_active)
        self.assertFalse(by_name["first.jsonl"].is_active)

    def test_active_flag_moves_when_recency_changes(self):
        old = self.write_transcript("proj-a", "a.jsonl", turn("m1", OPUS),
                                    age_seconds=200)
        self.write_transcript("proj-b", "b.jsonl", turn("m2", OPUS),
                              age_seconds=10)
        cache = SessionCache(self.projects)
        cache.summaries(now=self.now)

        # a.jsonl becomes the most recently modified.
        os.utime(old, (self.now - 1, self.now - 1))
        summaries = cache.summaries(now=self.now)

        by_name = {s.file_name: s for s in summaries}
        self.assertTrue(by_name["a.jsonl"].is_active)
        self.assertFalse(by_name["b.jsonl"].is_active)

    def test_non_active_reparsed_only_on_mtime_size_change(self):
        non_active = self.write_transcript("proj-a", "old.jsonl",
                                           turn("m1", OPUS), age_seconds=200)
        active = self.write_transcript("proj-b", "new.jsonl",
                                       turn("m2", OPUS), age_seconds=10)
        cache = SessionCache(self.projects)

        calls = []
        real = sessions.summarize_session

        def counting(path, **kwargs):
            calls.append(path)
            return real(path, **kwargs)

        with mock.patch.object(sessions, "summarize_session",
                               side_effect=counting):
            cache.summaries(now=self.now)
            self.assertEqual(sorted(calls), sorted([non_active, active]))

            # Unchanged files: only the active one is re-parsed.
            calls.clear()
            cache.summaries(now=self.now)
            self.assertEqual(calls, [active])

            # The non-active file changes (content + mtime, still older than
            # the active one): it must be re-parsed.
            with open(non_active, "a", encoding="utf-8") as handle:
                handle.write(PROMPT + "\n")
            stamp = self.now - 150
            os.utime(non_active, (stamp, stamp))
            calls.clear()
            cache.summaries(now=self.now)
            self.assertEqual(sorted(calls), sorted([non_active, active]))

    def test_previously_active_served_from_cache_with_flag_cleared(self):
        # b is active on pass 1. On pass 2 a new transcript outranks it; b is
        # unchanged, so it must come from cache AND carry is_active=False.
        self.write_transcript("proj-b", "b.jsonl", turn("m1", OPUS),
                              age_seconds=100)
        cache = SessionCache(self.projects)
        first = cache.summaries(now=self.now)
        self.assertTrue(first[0].is_active)

        newer = self.write_transcript("proj-c", "c.jsonl", turn("m2", OPUS),
                                      age_seconds=5)
        calls = []
        real = sessions.summarize_session

        def counting(path, **kwargs):
            calls.append(path)
            return real(path, **kwargs)

        with mock.patch.object(sessions, "summarize_session",
                               side_effect=counting):
            second = cache.summaries(now=self.now)

        self.assertEqual(calls, [newer])  # b.jsonl came from cache
        by_name = {s.file_name: s for s in second}
        self.assertTrue(by_name["c.jsonl"].is_active)
        self.assertFalse(by_name["b.jsonl"].is_active)

    def test_file_deleted_between_discovery_and_read_is_skipped(self):
        survivor = self.write_transcript("proj-a", "keep.jsonl",
                                         turn("m1", OPUS), age_seconds=10)
        doomed = self.write_transcript("proj-b", "gone.jsonl",
                                       turn("m2", OPUS), age_seconds=100)
        cache = SessionCache(self.projects)

        real_discover = sessions.discover_sessions

        def discover_then_delete(*args, **kwargs):
            records = real_discover(*args, **kwargs)
            if os.path.exists(doomed):
                os.remove(doomed)
            return records

        with mock.patch.object(sessions, "discover_sessions",
                               side_effect=discover_then_delete):
            summaries = cache.summaries(now=self.now)

        self.assertEqual([s.file_name for s in summaries], ["keep.jsonl"])
        self.assertTrue(summaries[0].is_active)
        self.assertTrue(os.path.exists(survivor))

    def test_unpriced_propagates_through_cache(self):
        self.write_transcript("proj-a", "odd.jsonl", turn("m1", UNKNOWN),
                              age_seconds=100)
        self.write_transcript("proj-b", "ok.jsonl", turn("m2", OPUS),
                              age_seconds=10)
        cache = SessionCache(self.projects)

        # Twice: the second pass serves odd.jsonl from cache; the flag must
        # survive the round-trip.
        cache.summaries(now=self.now)
        summaries = cache.summaries(now=self.now)

        by_name = {s.file_name: s for s in summaries}
        self.assertTrue(by_name["odd.jsonl"].unpriced)
        self.assertEqual(by_name["odd.jsonl"].total_cost_usd, 0.0)
        self.assertFalse(by_name["ok.jsonl"].unpriced)

    def test_empty_projects_dir_yields_empty_summaries(self):
        self.assertEqual(SessionCache(self.projects).summaries(now=self.now), [])


class RealTimeLast(SessionsBase):
    """Requirement 1: the Last line tracks the in-flight turn, not only the
    last completed one."""

    def test_in_flight_turn_drives_last_figures(self):
        # A completed turn, then an in-flight one (no end_turn) carrying usage.
        # Last must reflect the in-flight turn so it updates live mid-prompt.
        lines = turn("m1", OPUS, input_tokens=1000, output_tokens=1000) + [
            PROMPT,
            usage_assistant("m2", input_tokens=2000, output_tokens=400,
                            stop_reason="tool_use"),
        ]
        path = self.write_transcript("proj-a", "s1.jsonl", lines)

        summary = summarize_session(path)

        self.assertEqual(summary.last_input_tokens, 2000)   # the in-flight turn
        self.assertEqual(summary.last_output_tokens, 400)
        self.assertIsNotNone(summary.last_cost_usd)         # priced, not blank

    def test_typed_prompt_tail_falls_back_to_last_completed(self):
        # The idle tail (a typed prompt with no usage) must NOT blank Last with
        # zeros; it falls back to the last completed turn.
        lines = turn("m1", OPUS, input_tokens=1000, output_tokens=1000) + [PROMPT]
        path = self.write_transcript("proj-a", "s1.jsonl", lines)

        summary = summarize_session(path)

        self.assertEqual(summary.last_output_tokens, 1000)  # m1, not None/0


class SumFigures(SessionsBase):
    """Requirement 2: the Sum line's session-wide breakdown."""

    def test_sum_fields_total_the_session_folding_cache_creation(self):
        lines = [
            PROMPT,
            usage_assistant("m1", input_tokens=1000, output_tokens=500,
                            cache_creation=300, cache_read=4000),
            PROMPT,
            usage_assistant("m2", input_tokens=2000, output_tokens=700,
                            cache_creation=100, cache_read=1000),
        ]
        path = self.write_transcript("proj-a", "s1.jsonl", lines)

        summary = summarize_session(path)

        # IN folds cache-creation: (1000+2000) + (300+100) = 3400.
        self.assertEqual(summary.sum_input_tokens, 3400)
        self.assertEqual(summary.sum_output_tokens, 1200)   # 500 + 700
        self.assertEqual(summary.sum_cache_read_tokens, 5000)  # 4000 + 1000


class MarkerDrivenLiveness(SessionsBase):
    """Requirement 3: hook markers drive appearance/disappearance and liveness."""

    def cache(self):
        return SessionCache(self.projects, markers_dir=self.markers)

    def test_open_marker_before_first_prompt_synthesizes_a_block(self):
        # No transcript on disk yet, only a SessionStart marker: the session must
        # still appear (a brand-new session before its first prompt).
        self.write_marker("proj-new", "sNEW.jsonl", OPEN, age_seconds=1)

        summaries = self.cache().summaries(now=self.now)

        (only,) = summaries
        self.assertEqual(only.file_name, "sNEW.jsonl")
        self.assertEqual(only.project, "proj-new")
        self.assertEqual(only.marker_event, OPEN)
        self.assertTrue(only.is_active)          # newest -> auto-followed
        self.assertIsNone(only.last_output_tokens)  # nothing parsed yet
        # And it renders as an ACTIVE block.
        view = build_roster_view(summaries, now=self.now)
        self.assertEqual(view.active_count, 1)

    def test_open_marker_keeps_an_idle_session_active_past_the_mtime_bound(self):
        # A transcript untouched well past the 720s dropped boundary would drop
        # on mtime alone; an open marker keeps it on screen.
        self.write_transcript("proj-a", "s1.jsonl", turn("m1", OPUS),
                              age_seconds=5_000)
        self.write_marker("proj-a", "s1.jsonl", OPEN, age_seconds=5_000)

        summaries = self.cache().summaries(now=self.now)
        view = build_roster_view(summaries, now=self.now)

        self.assertEqual([s.file_name for s in view.sessions], ["s1.jsonl"])
        self.assertEqual(view.active_count, 1)

    def test_closed_marker_drops_a_session_with_a_fresh_transcript(self):
        # The 10-minute-lingering fix: a just-written transcript + a SessionEnd
        # marker leaves the roster at once.
        self.write_transcript("proj-a", "s1.jsonl", turn("m1", OPUS),
                              age_seconds=1)
        self.write_marker("proj-a", "s1.jsonl", CLOSED, age_seconds=1)

        summaries = self.cache().summaries(now=self.now)
        view = build_roster_view(summaries, now=self.now)

        self.assertEqual(view.sessions, [])      # gone despite fresh mtime

    def test_marker_flip_is_seen_on_a_cache_served_summary(self):
        # An idle transcript is served from cache on the second pass (its key did
        # not move), but a marker that flipped open->closed in between MUST be
        # re-attached fresh, or a closed session would wrongly linger. A newer
        # transcript keeps s1 off the always-reparsed "newest" slot.
        s1 = self.write_transcript("proj-a", "s1.jsonl", turn("m1", OPUS),
                                   age_seconds=100)
        self.write_marker("proj-a", "s1.jsonl", OPEN, age_seconds=100)
        self.write_transcript("proj-b", "newest.jsonl", turn("m2", OPUS),
                              age_seconds=1)
        cache = self.cache()

        first = {s.file_name: s for s in cache.summaries(now=self.now)}
        self.assertEqual(first["s1.jsonl"].marker_event, OPEN)

        # s1 is closed; its transcript file does not change.
        self.write_marker("proj-a", "s1.jsonl", CLOSED, age_seconds=1)
        calls = []
        real = sessions.summarize_session

        def counting(path, **kwargs):
            calls.append(path)
            return real(path, **kwargs)

        with mock.patch.object(sessions, "summarize_session",
                               side_effect=counting):
            second = cache.summaries(now=self.now)

        self.assertNotIn(s1, calls)               # s1 served from cache
        by_name = {s.file_name: s for s in second}
        self.assertEqual(by_name["s1.jsonl"].marker_event, CLOSED)  # fresh marker
        names = [s.file_name for s in build_roster_view(second, now=self.now).sessions]
        self.assertNotIn("s1.jsonl", names)       # closed -> dropped from roster
        self.assertIn("newest.jsonl", names)      # the other stays


if __name__ == "__main__":
    unittest.main()
