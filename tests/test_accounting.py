"""Tests for cc_token_tracker.accounting (Ticket 2).

Inputs are TranscriptRecord values (the output of parse_line), constructed
directly here -- accounting does no IO. Cost is the four-field sum
(input + cache_creation + cache_read + output), absent counts coalesced to 0.
"""

import unittest

from cc_token_tracker.accounting import account_usage
from cc_token_tracker.parser import TranscriptRecord, Usage


def assistant(message_id, input_tokens, output_tokens, cache_creation, cache_read):
    """A minimal usage-bearing assistant record."""
    return TranscriptRecord(
        type="assistant",
        message_id=message_id,
        role="assistant",
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
        stop_reason="end_turn",
    )


class AccountUsage(unittest.TestCase):
    def test_single_message_full_usage(self):
        # One assistant message, one line, full usage.
        rec = assistant("msg_a", input_tokens=1200, output_tokens=85,
                        cache_creation=4000, cache_read=0)
        result = account_usage([rec])

        self.assertEqual(len(result.messages), 1)
        mc = result.messages[0]
        self.assertEqual(mc.message_id, "msg_a")
        self.assertEqual(mc.input_tokens, 1200)
        self.assertEqual(mc.cache_creation_input_tokens, 4000)
        self.assertEqual(mc.cache_read_input_tokens, 0)
        self.assertEqual(mc.output_tokens, 85)
        self.assertEqual(mc.message_total, 1200 + 4000 + 0 + 85)  # 5285
        self.assertEqual(mc.session_total, 5285)
        self.assertEqual(result.session_total, 5285)

    def test_multiline_message_counted_once(self):
        # Load-bearing: 3 lines, same message_id, identical usage -> counted
        # ONCE, not tripled. (thinking / text / tool_use blocks of one turn.)
        usage = dict(input_tokens=10, output_tokens=40, cache_creation=20, cache_read=30)
        records = [
            assistant("msg_multi", **usage),
            assistant("msg_multi", **usage),
            assistant("msg_multi", **usage),
        ]
        result = account_usage(records)

        self.assertEqual(len(result.messages), 1)  # not 3
        mc = result.messages[0]
        self.assertEqual(mc.message_id, "msg_multi")
        self.assertEqual(mc.message_total, 100)     # not 300
        self.assertEqual(mc.session_total, 100)
        self.assertEqual(result.session_total, 100)  # not 300

    def test_two_distinct_messages_accumulate(self):
        # Two distinct messages in order -> cumulative session total accrues.
        m1 = assistant("msg_1", input_tokens=1, output_tokens=4,
                       cache_creation=2, cache_read=3)            # total 10
        m2 = assistant("msg_2", input_tokens=10, output_tokens=40,
                       cache_creation=20, cache_read=30)          # total 100
        result = account_usage([m1, m2])

        self.assertEqual([mc.message_id for mc in result.messages], ["msg_1", "msg_2"])
        self.assertEqual(result.messages[0].message_total, 10)
        self.assertEqual(result.messages[0].session_total, 10)
        self.assertEqual(result.messages[1].message_total, 100)
        self.assertEqual(result.messages[1].session_total, 110)  # cumulative
        self.assertEqual(result.session_total, 110)

    def test_partial_usage_absent_cache_is_zero(self):
        # Same usage as the Ticket 1 usage_partial case: input 100, output 50,
        # both cache fields absent -> total 150, cache components shown as 0.
        rec = TranscriptRecord(
            type="assistant",
            message_id="msg_partial",
            role="assistant",
            usage=Usage(input_tokens=100, output_tokens=50),  # cache fields None
            stop_reason="end_turn",
        )
        result = account_usage([rec])

        mc = result.messages[0]
        self.assertEqual(mc.input_tokens, 100)
        self.assertEqual(mc.output_tokens, 50)
        self.assertEqual(mc.cache_creation_input_tokens, 0)  # absent -> 0
        self.assertEqual(mc.cache_read_input_tokens, 0)      # absent -> 0
        self.assertEqual(mc.message_total, 150)
        self.assertEqual(result.session_total, 150)

    def test_mixed_input_only_assistant_usage_counted(self):
        # None (malformed) + a user/no-usage record are skipped; only the two
        # assistant usage records are counted.
        none_result = None
        user_no_usage = TranscriptRecord(type="user", role="user", usage=None)
        a1 = assistant("msg_x", input_tokens=5, output_tokens=5,
                       cache_creation=0, cache_read=0)   # total 10
        a2 = assistant("msg_y", input_tokens=1, output_tokens=1,
                       cache_creation=0, cache_read=0)   # total 2
        result = account_usage([none_result, user_no_usage, a1, a2])

        self.assertEqual([mc.message_id for mc in result.messages], ["msg_x", "msg_y"])
        self.assertEqual(result.session_total, 12)

    def test_two_none_id_records_not_merged(self):
        # Two usage-bearing records both with message_id None must stay separate
        # -- a None id is not a shared key, so they are NOT collapsed into one.
        n1 = assistant(None, input_tokens=1, output_tokens=1,
                       cache_creation=0, cache_read=0)   # total 2
        n2 = assistant(None, input_tokens=3, output_tokens=4,
                       cache_creation=0, cache_read=0)   # total 7
        result = account_usage([n1, n2])

        self.assertEqual(len(result.messages), 2)  # not merged into 1
        self.assertEqual([mc.message_id for mc in result.messages], [None, None])
        self.assertEqual(result.messages[0].message_total, 2)
        self.assertEqual(result.messages[1].message_total, 7)
        self.assertEqual(result.messages[1].session_total, 9)
        self.assertEqual(result.session_total, 9)

    def test_empty_input(self):
        result = account_usage([])
        self.assertEqual(result.messages, [])
        self.assertEqual(result.session_total, 0)


if __name__ == "__main__":
    unittest.main()
