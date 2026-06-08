"""Tests for cc_token_tracker.turn_cost (Ticket 4).

Builds Turn values directly and checks that per-turn cost equals account_usage
run over that turn's records (deduped, not per-line), order preserved, and the
complete flag carried through.
"""

import unittest

from cc_token_tracker.accounting import account_usage
from cc_token_tracker.parser import TranscriptRecord, Usage
from cc_token_tracker.segmentation import Turn
from cc_token_tracker.turn_cost import turn_costs


def prompt(mid):
    return TranscriptRecord(type="user", message_id=mid, role="user")


def tool_result(mid):
    return TranscriptRecord(type="user", message_id=mid, role="user",
                            is_tool_result=True)


def assistant(mid, input_tokens, output_tokens, cache_creation, cache_read,
              stop_reason="end_turn"):
    return TranscriptRecord(
        type="assistant", message_id=mid, role="assistant", stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                    cache_creation_input_tokens=cache_creation,
                    cache_read_input_tokens=cache_read),
    )


class TurnCosts(unittest.TestCase):
    def test_single_message_turn(self):
        # One complete single-message turn -> one TurnCost, total matches
        # account_usage on those records.
        records = [prompt("p1"), assistant("a1", 100, 50, 0, 0)]
        turn = Turn(records=records, complete=True)

        result = turn_costs([turn])

        self.assertEqual(len(result), 1)
        tc = result[0]
        self.assertTrue(tc.complete)
        self.assertEqual(tc.turn_total, 150)
        self.assertEqual(tc.turn_total, account_usage(records).session_total)
        self.assertEqual(tc.input_tokens, 100)
        self.assertEqual(tc.output_tokens, 50)
        self.assertEqual(tc.cache_creation_input_tokens, 0)
        self.assertEqual(tc.cache_read_input_tokens, 0)

    def test_multiline_tool_loop_turn_is_deduped(self):
        # The real per-command number: a tool-loop turn whose first assistant
        # message is written as TWO lines (identical usage). The turn cost must
        # dedupe that message (count once) and match account_usage over the
        # WHOLE turn -- not sum per line.
        records = [
            prompt("p1"),
            assistant("a1", 10, 5, 0, 0, "tool_use"),   # message a1, line 1
            assistant("a1", 10, 5, 0, 0, "tool_use"),   # message a1, line 2 (dup)
            tool_result("tr1"),
            assistant("a2", 20, 7, 0, 0, "end_turn"),   # message a2
        ]
        turn = Turn(records=records, complete=True)

        result = turn_costs([turn])
        tc = result[0]

        # a1 counted ONCE (15), a2 (27) -> 42; the naive per-line sum would be 57.
        self.assertEqual(tc.turn_total, 42)
        self.assertNotEqual(tc.turn_total, 57)
        self.assertEqual(tc.turn_total, account_usage(records).session_total)
        # four-field breakdown is the deduped aggregate
        self.assertEqual(tc.input_tokens, 30)   # 10 + 20
        self.assertEqual(tc.output_tokens, 12)  # 5 + 7
        self.assertEqual(tc.turn_total,
                         tc.input_tokens + tc.cache_creation_input_tokens
                         + tc.cache_read_input_tokens + tc.output_tokens)

    def test_two_turns_costed_independently_in_order(self):
        t1 = Turn(records=[prompt("p1"), assistant("a1", 1, 2, 3, 4)], complete=True)
        t2 = Turn(records=[prompt("p2"), assistant("a2", 10, 20, 30, 40)], complete=True)

        result = turn_costs([t1, t2])

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].turn_total, 10)
        # independent, NOT cumulative: second turn is 100, not 110.
        self.assertEqual(result[1].turn_total, 100)

    def test_incomplete_turn_still_costed(self):
        # Trailing in-flight turn -> still costed, complete=False carried.
        turn = Turn(records=[prompt("p2"), assistant("a2", 10, 5, 0, 0, "tool_use")],
                    complete=False)

        result = turn_costs([turn])
        tc = result[0]

        self.assertFalse(tc.complete)
        self.assertEqual(tc.turn_total, 15)

    def test_empty_turn_list(self):
        self.assertEqual(turn_costs([]), [])


if __name__ == "__main__":
    unittest.main()
