"""Tests for cc_token_tracker.display (Ticket 7).

Pure fixtures: no clock, no real poll loop. The display consumes the existing
reader/parser/accounting/segmentation/turn_cost layers unchanged, so these tests
build records directly and feed ReadResult values, exactly like the pipeline
would. The rendered text is cosmetic and deliberately not pinned.
"""

import contextlib
import io
import unittest
from unittest import mock

import cc_token_tracker.shim as shim
from cc_token_tracker import display
from cc_token_tracker.accounting import account_usage
from cc_token_tracker.parser import TranscriptRecord, Usage
from cc_token_tracker.reader import ReadResult


def prompt(mid):
    return TranscriptRecord(type="user", message_id=mid, role="user")


def assistant(mid, input_tokens, output_tokens, cache_creation, cache_read,
              stop_reason="end_turn"):
    return TranscriptRecord(
        type="assistant", message_id=mid, role="assistant", stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                    cache_creation_input_tokens=cache_creation,
                    cache_read_input_tokens=cache_read),
    )


def read_result(records, path):
    return ReadResult(records=records, transcript_path=path)


class ComputeFrame(unittest.TestCase):
    def test_happy_multi_turn(self):
        # delta is the LAST turn; session_total is account_usage over ALL records.
        records = [
            prompt("p1"), assistant("a1", 100, 50, 0, 0),
            prompt("p2"), assistant("a2", 10, 5, 0, 0),
        ]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))

        self.assertIsNotNone(frame.delta)
        self.assertTrue(frame.delta.complete)
        self.assertEqual(frame.delta.turn_total, 15)  # the second turn alone
        self.assertEqual(frame.session_total,
                         account_usage(records).session_total)
        self.assertEqual(frame.session_total, 165)  # 150 + 15, whole transcript
        self.assertEqual(frame.transcript_path, "/x/t.jsonl")

    def test_last_turn_in_flight(self):
        # trailing turn has no end_turn -> delta is that turn, complete False.
        records = [
            prompt("p1"), assistant("a1", 100, 50, 0, 0),
            prompt("p2"), assistant("a2", 10, 5, 0, 0, stop_reason="tool_use"),
        ]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))

        self.assertIsNotNone(frame.delta)
        self.assertFalse(frame.delta.complete)
        self.assertEqual(frame.delta.turn_total, 15)

    def test_records_but_no_turns(self):
        # only pre-prompt records (no typed prompt opens a turn): delta None,
        # no raise, but session_total is still computed over the records.
        records = [assistant("a1", 100, 50, 0, 0)]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))

        self.assertIsNone(frame.delta)
        self.assertEqual(frame.session_total, 150)
        self.assertEqual(frame.transcript_path, "/x/t.jsonl")


class DisplayStateUpdate(unittest.TestCase):
    def test_none_path_holds_prior_frame(self):
        # a real reading, then a no-op tick (path None, empty records): HOLD.
        state = display.DisplayState()
        records = [prompt("p1"), assistant("a1", 100, 50, 0, 0)]
        good = state.update(read_result(records, "/x/t.jsonl"))

        held = state.update(read_result([], None))

        self.assertIs(held, good)  # exact same frame, unchanged
        self.assertEqual(held.transcript_path, "/x/t.jsonl")
        self.assertEqual(held.session_total, 150)

    def test_valid_path_empty_records_is_fresh_zero_frame(self):
        # prior real frame held, then a fresh session whose transcript has no
        # records yet: a NEW zero frame for that path, not the held prior.
        state = display.DisplayState()
        state.update(read_result([prompt("p1"), assistant("a1", 100, 50, 0, 0)],
                                 "/a/t.jsonl"))

        fresh = state.update(read_result([], "/b/new.jsonl"))

        self.assertIsNone(fresh.delta)
        self.assertEqual(fresh.session_total, 0)
        self.assertEqual(fresh.transcript_path, "/b/new.jsonl")

    def test_session_switch_resets_total(self):
        # load-bearing RESET: A's total is held, then B arrives. The frame is
        # B's total ALONE, not A + B, and the path is B's.
        state = display.DisplayState()
        a_records = [prompt("pa"), assistant("aa", 100, 50, 0, 0)]   # 150
        b_records = [prompt("pb"), assistant("ab", 1, 2, 3, 4)]      # 10

        a_frame = state.update(read_result(a_records, "/a/t.jsonl"))
        self.assertEqual(a_frame.session_total, 150)

        b_frame = state.update(read_result(b_records, "/b/t.jsonl"))

        b_alone = account_usage(b_records).session_total
        self.assertEqual(b_frame.session_total, b_alone)
        self.assertEqual(b_frame.session_total, 10)
        self.assertNotEqual(b_frame.session_total, 160)  # never A + B
        self.assertEqual(b_frame.transcript_path, "/b/t.jsonl")


class RunDefaults(unittest.TestCase):
    def test_run_defaults_to_shim_pointer_path(self):
        # run with no pointer_path must poll the shim's DEFAULT_POINTER_PATH.
        # We do not run the real loop: a stubbed read_tick captures the path and
        # raises KeyboardInterrupt to stop after the first tick, which also
        # exercises the clean exit-0 path. stdout is swallowed so the terminal
        # control codes do not leak into test output.
        self.assertIs(display.DEFAULT_POINTER_PATH, shim.DEFAULT_POINTER_PATH)

        captured = {}

        def stub_read_tick(pointer_path):
            captured["path"] = pointer_path
            raise KeyboardInterrupt

        with mock.patch.object(display, "read_tick", stub_read_tick):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = display.run()

        self.assertEqual(captured["path"], shim.DEFAULT_POINTER_PATH)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
