"""Tests for cc_token_tracker.context (limits table and used-context estimate).

Pure-logic tests: records are built via the real parser so the estimate sees
exactly what a transcript would produce.
"""

import json
import unittest

from cc_token_tracker.context import context_limit, estimate_context
from cc_token_tracker.parser import parse_line


def assistant_record(model=None, input_tokens=0, output_tokens=0,
                     cache_creation=0, cache_read=0, message_id="m1"):
    message = {
        "id": message_id,
        "role": "assistant",
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    }
    if model is not None:
        message["model"] = model
    return parse_line(json.dumps({"type": "assistant", "message": message}))


def user_record():
    return parse_line('{"type":"user","message":{"role":"user","content":"hi"}}')


class ContextLimitLookup(unittest.TestCase):
    def test_known_models(self):
        # Documented context windows, platform.claude.com/docs Models overview
        # as of 2026-06-12.
        self.assertEqual(context_limit("claude-fable-5"), 1_000_000)
        self.assertEqual(context_limit("claude-opus-4-8"), 1_000_000)
        self.assertEqual(context_limit("claude-opus-4-7"), 1_000_000)
        self.assertEqual(context_limit("claude-opus-4-6"), 1_000_000)
        self.assertEqual(context_limit("claude-opus-4-5"), 200_000)
        self.assertEqual(context_limit("claude-sonnet-4-6"), 1_000_000)
        self.assertEqual(context_limit("claude-haiku-4-5"), 200_000)

    def test_dated_id_normalizes_like_pricing(self):
        self.assertEqual(context_limit("claude-haiku-4-5-20251001"), 200_000)

    def test_unknown_model_yields_none_never_a_guess(self):
        self.assertIsNone(context_limit("totally-unknown-model-9000"))

    def test_none_model_yields_none(self):
        self.assertIsNone(context_limit(None))


class EstimateContext(unittest.TestCase):
    def test_sums_input_side_of_most_recent_usage_bearing_assistant(self):
        records = [
            user_record(),
            assistant_record(model="claude-opus-4-8", input_tokens=10,
                             cache_read=20, cache_creation=5, message_id="m1"),
            user_record(),
            assistant_record(model="claude-opus-4-8", input_tokens=1_000,
                             cache_read=95_000, cache_creation=4_000,
                             output_tokens=500, message_id="m2"),
        ]

        estimate = estimate_context(records)

        # The LAST assistant record wins; output tokens are not context-used.
        self.assertEqual(estimate.used, 100_000)
        self.assertEqual(estimate.limit, 1_000_000)
        self.assertAlmostEqual(estimate.percent, 10.0)

    def test_trailing_non_usage_records_do_not_shadow_the_estimate(self):
        records = [
            user_record(),
            assistant_record(model="claude-haiku-4-5", input_tokens=50_000,
                             message_id="m1"),
            user_record(),  # a new prompt in flight, no usage yet
        ]

        estimate = estimate_context(records)

        self.assertEqual(estimate.used, 50_000)
        self.assertEqual(estimate.limit, 200_000)
        self.assertAlmostEqual(estimate.percent, 25.0)

    def test_percent_can_exceed_100(self):
        records = [
            assistant_record(model="claude-haiku-4-5", input_tokens=8_000,
                             cache_read=200_000, message_id="m1"),
        ]

        estimate = estimate_context(records)

        self.assertEqual(estimate.used, 208_000)
        self.assertAlmostEqual(estimate.percent, 104.0)

    def test_unknown_model_keeps_used_but_no_limit_or_percent(self):
        records = [
            assistant_record(model="mystery-model", input_tokens=1_234,
                             message_id="m1"),
        ]

        estimate = estimate_context(records)

        self.assertEqual(estimate.used, 1_234)
        self.assertIsNone(estimate.limit)
        self.assertIsNone(estimate.percent)

    def test_no_usage_bearing_assistant_yields_all_none(self):
        self.assertEqual(estimate_context([user_record(), None]).used, None)
        self.assertIsNone(estimate_context([]).limit)
        self.assertIsNone(estimate_context([user_record()]).percent)

    def test_absent_usage_counts_coalesce_to_zero(self):
        # A usage block carrying only input_tokens: the missing cache counts
        # are 0, not an error and not None-poisoned.
        line = json.dumps({
            "type": "assistant",
            "message": {
                "id": "m1", "role": "assistant", "model": "claude-fable-5",
                "content": [{"type": "text", "text": "x"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 7},
            },
        })
        estimate = estimate_context([parse_line(line)])
        self.assertEqual(estimate.used, 7)
        self.assertEqual(estimate.limit, 1_000_000)


if __name__ == "__main__":
    unittest.main()
