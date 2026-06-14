"""Tests for cc_token_tracker.hook: the SessionStart/SessionEnd hook entry point.

The hook is a pure side effect (write a marker) that must never raise to Claude
Code and must ignore anything malformed. These tests pin the event branching,
the input validation, and the always-0 / never-raise main(). A temp markers dir
is injected so nothing touches the real store.
"""

import io
import os
import json
import tempfile
import unittest
from unittest import mock

from cc_token_tracker import hook
from cc_token_tracker.markers import CLOSED, OPEN, read_markers


def payload(**fields):
    return json.dumps(fields)


class RunHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "sessions")

    def test_session_start_writes_open_marker(self):
        wrote = hook.run_hook(
            payload(hook_event_name="SessionStart", session_id="sid-1",
                    transcript_path="/p/a.jsonl", cwd="/p"),
            markers_dir=self.dir,
        )
        self.assertTrue(wrote)
        markers = read_markers(self.dir)
        self.assertEqual(markers["/p/a.jsonl"].event, OPEN)

    def test_session_end_writes_closed_marker(self):
        hook.run_hook(
            payload(hook_event_name="SessionStart", session_id="sid-1",
                    transcript_path="/p/a.jsonl", cwd="/p"),
            markers_dir=self.dir,
        )
        wrote = hook.run_hook(
            payload(hook_event_name="SessionEnd", session_id="sid-1",
                    transcript_path="/p/a.jsonl", cwd="/p"),
            markers_dir=self.dir,
        )
        self.assertTrue(wrote)
        markers = read_markers(self.dir)
        self.assertEqual(markers["/p/a.jsonl"].event, CLOSED)

    def test_unknown_event_writes_nothing(self):
        wrote = hook.run_hook(
            payload(hook_event_name="PreToolUse", session_id="sid-1",
                    transcript_path="/p/a.jsonl"),
            markers_dir=self.dir,
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
            self.assertFalse(hook.run_hook(text, markers_dir=self.dir))
        self.assertEqual(read_markers(self.dir), {})


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
