"""Tests for cc_token_tracker.hook: the SessionStart/SessionEnd hook entry point.

The hook is a pure side effect (write a marker) that must never raise to Claude
Code and must ignore anything malformed. These tests pin the event branching,
the input validation, and the always-0 / never-raise main(). A temp markers dir
is injected so nothing touches the real store.
"""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from cc_token_tracker import hook
from cc_token_tracker.history import daily_totals
from cc_token_tracker.markers import CLOSED, OPEN, read_markers


def payload(**fields):
    return json.dumps(fields)


class RunHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "sessions")
        # Injected everywhere so no hook test can ever touch the real
        # ~/.claude/tokey/history.db.
        self.db = os.path.join(self.tmp.name, "history.db")

    def test_session_start_writes_open_marker(self):
        wrote = hook.run_hook(
            payload(hook_event_name="SessionStart", session_id="sid-1",
                    transcript_path="/p/a.jsonl", cwd="/p"),
            markers_dir=self.dir, db_path=self.db,
        )
        self.assertTrue(wrote)
        markers = read_markers(self.dir)
        self.assertEqual(markers["/p/a.jsonl"].event, OPEN)

    def test_session_end_writes_closed_marker(self):
        hook.run_hook(
            payload(hook_event_name="SessionStart", session_id="sid-1",
                    transcript_path="/p/a.jsonl", cwd="/p"),
            markers_dir=self.dir, db_path=self.db,
        )
        wrote = hook.run_hook(
            payload(hook_event_name="SessionEnd", session_id="sid-1",
                    transcript_path="/p/a.jsonl", cwd="/p"),
            markers_dir=self.dir, db_path=self.db,
        )
        self.assertTrue(wrote)
        markers = read_markers(self.dir)
        self.assertEqual(markers["/p/a.jsonl"].event, CLOSED)

    def test_unknown_event_writes_nothing(self):
        wrote = hook.run_hook(
            payload(hook_event_name="PreToolUse", session_id="sid-1",
                    transcript_path="/p/a.jsonl"),
            markers_dir=self.dir, db_path=self.db,
        )
        self.assertFalse(wrote)
        self.assertEqual(read_markers(self.dir), {})

    def test_missing_fields_and_garbage_write_nothing(self):
        for text in (
            "{ not json",
            "[]",
            "null",
            payload(hook_event_name="SessionStart"),  # no session_id/path
            payload(hook_event_name="SessionStart", session_id="sid-1"),
            payload(session_id="sid-1", transcript_path="/p/a.jsonl"),  # no event
        ):
            self.assertFalse(hook.run_hook(text, markers_dir=self.dir, db_path=self.db))
        self.assertEqual(read_markers(self.dir), {})


class HistoryRecording(unittest.TestCase):
    """SessionEnd is the one moment a finished session is guaranteed to be
    recorded before its transcript ages out of the roster's window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "sessions")
        self.db = os.path.join(self.tmp.name, "history.db")
        self.transcript = os.path.join(self.tmp.name, "sid-1.jsonl")
        with open(self.transcript, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "message": {
                "id": "u1", "role": "user", "content": "hi"}}) + "\n")
            handle.write(json.dumps({"type": "assistant", "message": {
                "id": "a1", "role": "assistant", "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 500,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0}}}) + "\n")

    def _fire(self, event):
        return hook.run_hook(
            payload(hook_event_name=event, session_id="sid-1",
                    transcript_path=self.transcript, cwd="/p"),
            markers_dir=self.dir, db_path=self.db,
        )

    def test_session_end_records_the_session(self):
        self._fire("SessionEnd")
        days = daily_totals(db_path=self.db)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0].sessions, 1)
        self.assertEqual(days[0].total_tokens, 1500)
        # 1000 in + 500 out on opus-5: 1000*5 + 500*25 per MTok.
        self.assertAlmostEqual(days[0].cost_usd, 0.0175)

    def test_session_start_records_nothing(self):
        self._fire("SessionStart")
        self.assertEqual(daily_totals(db_path=self.db), [])

    def test_a_failing_history_write_still_returns_the_marker_result(self):
        # A db path whose parent is a file: every history write fails.
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("")
        wrote = hook.run_hook(
            payload(hook_event_name="SessionEnd", session_id="sid-1",
                    transcript_path=self.transcript, cwd="/p"),
            markers_dir=self.dir,
            db_path=os.path.join(blocker, "nested", "history.db"),
        )
        self.assertTrue(wrote)  # the marker is what the roster needs
        self.assertEqual(read_markers(self.dir)[self.transcript].event, CLOSED)


class Main(unittest.TestCase):
    def test_main_reads_stdin_returns_zero_and_never_raises(self):
        blob = json.dumps({"hook_event_name": "SessionStart"}).encode("utf-8")
        fake_stdin = mock.Mock()
        fake_stdin.buffer = io.BytesIO(blob)
        with mock.patch("sys.stdin", fake_stdin):
            self.assertEqual(hook.main(), 0)

    def test_main_swallows_a_broken_stdin(self):
        broken = mock.Mock()
        broken.buffer.read.side_effect = OSError("boom")
        with mock.patch("sys.stdin", broken):
            self.assertEqual(hook.main(), 0)  # swallowed, still 0


if __name__ == "__main__":
    unittest.main()
