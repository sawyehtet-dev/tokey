"""Tests for cc_token_tracker.reader (Ticket 5).

Uses real temp files (tempfile), not mocks of open. read_transcript full
re-reads an already-resolved transcript path each tick; find_active_transcript
resolves WHICH path by recency.
"""

import os
import tempfile
import unittest
from unittest import mock

from cc_token_tracker.parser import TranscriptRecord
from cc_token_tracker.reader import find_active_transcript, read_transcript


# A genuine typed prompt (no message id) and an assistant line carrying a
# message id, so tests can name which records came back.
PROMPT = '{"type":"user","message":{"role":"user","content":"hi"}}'


def assistant_line(message_id, text):
    return (
        '{"type":"assistant","message":{"id":"' + message_id + '",'
        '"role":"assistant","content":[{"type":"text","text":"' + text + '"}],'
        '"stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":1,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}'
    )


class ReadTranscript(unittest.TestCase):
    """read_transcript parses an already-resolved transcript path. The path is
    resolved upstream (find_active_transcript); these tests pin the read/parse
    and no-op behavior by handing a path in directly.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.transcript = os.path.join(self.base, "transcript.jsonl")

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def assert_noop(self, result):
        self.assertEqual(result.records, [])
        self.assertIsNone(result.transcript_path)

    def test_none_path_is_noop(self):
        # nothing resolved this tick (find_active_transcript returned None)
        self.assert_noop(read_transcript(None))

    def test_missing_transcript_is_noop(self):
        # a resolved path that does not exist on disk
        self.assert_noop(read_transcript(self.transcript))  # never created

    def test_happy_path_multiple_turns(self):
        lines = [
            PROMPT,
            assistant_line("m1", "first"),
            PROMPT,
            assistant_line("m2", "second"),
        ]
        self.write(self.transcript, "\n".join(lines) + "\n")

        result = read_transcript(self.transcript)

        self.assertEqual(result.transcript_path, self.transcript)
        self.assertEqual(len(result.records), 4)
        self.assertTrue(all(isinstance(r, TranscriptRecord) for r in result.records))
        self.assertEqual([r.message_id for r in result.records],
                         [None, "m1", None, "m2"])

    def test_final_line_without_trailing_newline_is_kept(self):
        # well-formed final line with NO trailing newline must still be returned
        content = PROMPT + "\n" + assistant_line("m1", "done")
        self.write(self.transcript, content)

        result = read_transcript(self.transcript)

        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[-1].message_id, "m1")

    def test_truncated_final_line_dropped_rest_kept(self):
        # load-bearing: final line truncated mid-JSON is dropped, prior records
        # kept, and the read does not raise
        good = PROMPT + "\n" + assistant_line("m1", "ok") + "\n"
        truncated = '{"type":"assi'
        self.write(self.transcript, good + truncated)

        result = read_transcript(self.transcript)

        self.assertEqual(len(result.records), 2)
        self.assertEqual([r.message_id for r in result.records], [None, "m1"])
        self.assertEqual(result.transcript_path, self.transcript)

    def test_line_with_raw_u2028_is_one_record(self):
        # pin: a single valid JSONL line whose content carries a raw U+2028
        # (legal unescaped in a JSON string per RFC 8259, emitted raw by V8's
        # JSON.stringify) must stay one line. split("\n") keeps it intact;
        # splitlines() would break it into two fragments that both fail to parse
        # and the real assistant message would vanish with no error.
        line = assistant_line("u2028msg", "before\u2028after")
        self.write(self.transcript, line + "\n")

        result = read_transcript(self.transcript)

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].message_id, "u2028msg")

    def test_distinct_reads_do_not_mix(self):
        # each read_transcript call is a fresh full re-read with no cross-file
        # state: reading A then B returns only the file actually read. This is
        # what makes a session switch rebase to the new transcript alone.
        file_a = os.path.join(self.base, "a.jsonl")
        file_b = os.path.join(self.base, "b.jsonl")
        self.write(file_a, PROMPT + "\n" + assistant_line("msgA", "A") + "\n")
        self.write(file_b, PROMPT + "\n" + assistant_line("msgB", "B") + "\n")

        first = read_transcript(file_a)
        self.assertEqual(first.transcript_path, file_a)
        ids_first = [r.message_id for r in first.records]
        self.assertIn("msgA", ids_first)
        self.assertNotIn("msgB", ids_first)

        second = read_transcript(file_b)
        self.assertEqual(second.transcript_path, file_b)
        ids_second = [r.message_id for r in second.records]
        self.assertIn("msgB", ids_second)
        self.assertNotIn("msgA", ids_second)


class FindActiveTranscript(unittest.TestCase):
    """find_active_transcript resolves the most recently modified *.jsonl under
    ~/.claude/projects (recursively), or None. A tmp projects dir is substituted
    via os.path.expanduser so the real home is never touched; mtimes are set
    explicitly so recency is deterministic.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The dir find_active_transcript will resolve "~/.claude/projects" to.
        self.projects = os.path.join(self.tmp.name, "projects")

    def write_jsonl(self, path, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(PROMPT + "\n")
        os.utime(path, (mtime, mtime))

    def test_returns_newest_of_several(self):
        # Three transcripts, one nested in a subdir to exercise the recursive
        # walk; explicit, distinct mtimes. The newest wins regardless of depth.
        os.makedirs(self.projects, exist_ok=True)
        older = os.path.join(self.projects, "older.jsonl")
        middle = os.path.join(self.projects, "proj-a", "middle.jsonl")
        newest = os.path.join(self.projects, "proj-b", "newest.jsonl")
        self.write_jsonl(older, 1000)
        self.write_jsonl(middle, 2000)
        self.write_jsonl(newest, 3000)

        with mock.patch("os.path.expanduser", return_value=self.projects):
            self.assertEqual(find_active_transcript(), newest)

    def test_missing_dir_returns_none(self):
        # The projects dir is never created.
        with mock.patch("os.path.expanduser", return_value=self.projects):
            self.assertIsNone(find_active_transcript())

    def test_dir_without_jsonl_returns_none(self):
        # The dir exists but holds no .jsonl file.
        os.makedirs(self.projects, exist_ok=True)
        with open(os.path.join(self.projects, "notes.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("not a transcript")
        with mock.patch("os.path.expanduser", return_value=self.projects):
            self.assertIsNone(find_active_transcript())

    def test_unreadable_entry_is_skipped(self):
        # One entry's mtime probe raises OSError (a file removed mid-scan or a
        # permission error on that entry). It is skipped without crashing, and
        # the other entry still resolves -- even though the failing one is newer.
        os.makedirs(self.projects, exist_ok=True)
        good = os.path.join(self.projects, "good.jsonl")
        bad = os.path.join(self.projects, "bad.jsonl")
        self.write_jsonl(good, 1000)
        self.write_jsonl(bad, 5000)  # newer, but its probe will fail

        real_getmtime = os.path.getmtime

        def flaky_getmtime(path):
            if path == bad:
                raise OSError("simulated mid-scan removal")
            return real_getmtime(path)

        with mock.patch("os.path.expanduser", return_value=self.projects), \
                mock.patch("os.path.getmtime", side_effect=flaky_getmtime):
            self.assertEqual(find_active_transcript(), good)


if __name__ == "__main__":
    unittest.main()
