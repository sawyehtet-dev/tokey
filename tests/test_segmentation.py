"""Tests for cc_token_tracker.segmentation (Ticket 3).

Inputs are TranscriptRecord values constructed directly (segmentation does no
IO). Records carry a message_id used purely as a label so assertions can name
the records that landed in each turn.
"""

import unittest

from cc_token_tracker.segmentation import segment_turns
from cc_token_tracker.parser import TranscriptRecord
from conftest import prompt, tool_result


def assistant(mid, stop_reason):
    return TranscriptRecord(type="assistant", message_id=mid, role="assistant",
                            stop_reason=stop_reason)


def meta(mid):
    return TranscriptRecord(type="user", message_id=mid, role="user", is_meta=True)


def sidechain(mid, stop_reason="end_turn"):
    return TranscriptRecord(type="assistant", message_id=mid, role="assistant",
                            stop_reason=stop_reason, is_sidechain=True)


def ids(turn):
    return [r.message_id for r in turn.records]


class SegmentTurns(unittest.TestCase):
    def test_single_prompt_and_end_turn(self):
        # Prompt + one assistant end_turn -> one complete turn.
        turns = segment_turns([prompt("p1"), assistant("a1", "end_turn")])
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0].complete)
        self.assertEqual(ids(turns[0]), ["p1", "a1"])

    def test_tool_loop_is_one_turn(self):
        # Prompt + 2 tool-loop assistant lines + tool_result user + final
        # end_turn -> ONE turn containing all of them, complete=True.
        records = [
            prompt("p1"),
            assistant("a1", "tool_use"),
            assistant("a2", "tool_use"),
            tool_result("tr1"),
            assistant("a3", "end_turn"),
        ]
        turns = segment_turns(records)
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0].complete)
        self.assertEqual(ids(turns[0]), ["p1", "a1", "a2", "tr1", "a3"])

    def test_two_full_turns_back_to_back(self):
        records = [
            prompt("p1"), assistant("a1", "end_turn"),
            prompt("p2"), assistant("a2", "end_turn"),
        ]
        turns = segment_turns(records)
        self.assertEqual(len(turns), 2)
        self.assertEqual([t.complete for t in turns], [True, True])
        self.assertEqual(ids(turns[0]), ["p1", "a1"])
        self.assertEqual(ids(turns[1]), ["p2", "a2"])  # order preserved

    def test_meta_and_sidechain_are_dropped(self):
        # meta/sidechain interleaved are dropped: not in any turn, do not open
        # turns, and a sidechain end_turn does NOT close the turn.
        records = [
            prompt("p1"),
            meta("m1"),
            assistant("a1", "tool_use"),
            sidechain("s1", "end_turn"),  # must NOT close the turn
            tool_result("tr1"),
            assistant("a2", "end_turn"),
        ]
        turns = segment_turns(records)
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0].complete)
        self.assertEqual(ids(turns[0]), ["p1", "a1", "tr1", "a2"])
        all_ids = [r.message_id for t in turns for r in t.records]
        self.assertNotIn("m1", all_ids)
        self.assertNotIn("s1", all_ids)

    def test_meta_does_not_open_a_turn(self):
        # A meta user line before any prompt does not open a turn.
        turns = segment_turns([meta("m1"), assistant("a1", "end_turn")])
        self.assertEqual(turns, [])

    def test_trailing_turn_without_end_turn_is_incomplete(self):
        records = [
            prompt("p1"), assistant("a1", "end_turn"),
            prompt("p2"), assistant("a2", "tool_use"),  # no end_turn
        ]
        turns = segment_turns(records)
        self.assertEqual(len(turns), 2)
        self.assertTrue(turns[0].complete)
        self.assertEqual(ids(turns[0]), ["p1", "a1"])
        self.assertFalse(turns[1].complete)            # in-flight
        self.assertEqual(ids(turns[1]), ["p2", "a2"])

    def test_records_before_first_prompt_ignored(self):
        records = [
            assistant("a0", "end_turn"),
            tool_result("tr0"),
            prompt("p1"),
            assistant("a1", "end_turn"),
        ]
        turns = segment_turns(records)
        self.assertEqual(len(turns), 1)
        self.assertEqual(ids(turns[0]), ["p1", "a1"])  # a0, tr0 ignored

    def test_empty_input(self):
        self.assertEqual(segment_turns([]), [])


if __name__ == "__main__":
    unittest.main()
