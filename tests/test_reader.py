"""Tests for cc_token_tracker.reader (Ticket 5).

Uses real temp files (tempfile), not mocks of open. Each tick reads a pointer
file that names the current transcript and full re-reads that transcript.
"""

import os
import tempfile
import unittest

from cc_token_tracker.parser import TranscriptRecord
from cc_token_tracker.reader import read_tick


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


class ReaderTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.pointer = os.path.join(self.base, "pointer")
        self.transcript = os.path.join(self.base, "transcript.jsonl")

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def set_pointer(self, target_path):
        # trailing newline exercises the pointer-strip path
        self.write(self.pointer, target_path + "\n")

    def assert_noop(self, result):
        self.assertEqual(result.records, [])
        self.assertIsNone(result.transcript_path)

    def test_pointer_absent_is_noop(self):
        # pointer file is never created
        self.assert_noop(read_tick(self.pointer))

    def test_pointer_whitespace_is_noop(self):
        self.write(self.pointer, "   \n\t ")
        self.assert_noop(read_tick(self.pointer))

    def test_pointer_to_missing_transcript_is_noop(self):
        self.set_pointer(self.transcript)  # transcript never created
        self.assert_noop(read_tick(self.pointer))

    def test_happy_path_multiple_turns(self):
        lines = [
            PROMPT,
            assistant_line("m1", "first"),
            PROMPT,
            assistant_line("m2", "second"),
        ]
        self.write(self.transcript, "\n".join(lines) + "\n")
        self.set_pointer(self.transcript)

        result = read_tick(self.pointer)

        self.assertEqual(result.transcript_path, self.transcript)
        self.assertEqual(len(result.records), 4)
        self.assertTrue(all(isinstance(r, TranscriptRecord) for r in result.records))
        self.assertEqual([r.message_id for r in result.records],
                         [None, "m1", None, "m2"])

    def test_final_line_without_trailing_newline_is_kept(self):
        # well-formed final line with NO trailing newline must still be returned
        content = PROMPT + "\n" + assistant_line("m1", "done")
        self.write(self.transcript, content)
        self.set_pointer(self.transcript)

        result = read_tick(self.pointer)

        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[-1].message_id, "m1")

    def test_truncated_final_line_dropped_rest_kept(self):
        # load-bearing: final line truncated mid-JSON is dropped, prior records
        # kept, and the tick does not raise
        good = PROMPT + "\n" + assistant_line("m1", "ok") + "\n"
        truncated = '{"type":"assi'
        self.write(self.transcript, good + truncated)
        self.set_pointer(self.transcript)

        result = read_tick(self.pointer)

        self.assertEqual(len(result.records), 2)
        self.assertEqual([r.message_id for r in result.records], [None, "m1"])
        self.assertEqual(result.transcript_path, self.transcript)

    def test_session_switch_across_ticks(self):
        # pointer rewritten to a different transcript between ticks: second tick
        # returns only the new file's records, never mixing the two
        file_a = os.path.join(self.base, "a.jsonl")
        file_b = os.path.join(self.base, "b.jsonl")
        self.write(file_a, PROMPT + "\n" + assistant_line("msgA", "A") + "\n")
        self.write(file_b, PROMPT + "\n" + assistant_line("msgB", "B") + "\n")

        self.set_pointer(file_a)
        first = read_tick(self.pointer)
        self.assertEqual(first.transcript_path, file_a)
        ids_first = [r.message_id for r in first.records]
        self.assertIn("msgA", ids_first)
        self.assertNotIn("msgB", ids_first)

        self.set_pointer(file_b)
        second = read_tick(self.pointer)
        self.assertEqual(second.transcript_path, file_b)
        ids_second = [r.message_id for r in second.records]
        self.assertIn("msgB", ids_second)
        self.assertNotIn("msgA", ids_second)


if __name__ == "__main__":
    unittest.main()
