"""Tests for cc_token_tracker.pricing and the COST render path.

Pure fixtures throughout: known token counts against the published per-MTok
rates, the unknown-model None contract, and the "$?" render path. The model
plumbing (parser field, last-usage-bearing-record-wins on TurnCost) is pinned
here too since pricing is its only consumer.
"""

import math
import unittest

from cc_token_tracker.parser import parse_line
from cc_token_tracker.pricing import normalize_model, turn_cost_usd
from cc_token_tracker.segmentation import segment_turns
from cc_token_tracker.turn_cost import session_cost, turn_costs, turn_usd
from conftest import assistant, typed


class KnownModels(unittest.TestCase):
    """One per model: 1M tokens of each component prices to the four per-MTok
    rates summed, so a wrong rate in any single cell cannot pass."""

    def test_fable_5_1(self):
        cost = turn_cost_usd("claude-fable-5-1",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 72.75)  # 10 + 50 + 12.50 + 0.25

    def test_mythos_5_1(self):
        cost = turn_cost_usd("claude-mythos-5-1",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 72.75)  # 10 + 50 + 12.50 + 0.25

    def test_fable_5(self):
        cost = turn_cost_usd("claude-fable-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 73.50)  # 10 + 50 + 12.50 + 1.00

    def test_fable_5_dated_id_prices_via_normalized_form(self):
        cost = turn_cost_usd("claude-fable-5-20260601",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 73.50)

    def test_mythos_5(self):
        cost = turn_cost_usd("claude-mythos-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 73.50)  # 10 + 50 + 12.50 + 1.00

    def test_opus_5_5(self):
        cost = turn_cost_usd("claude-opus-5-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 29.20)  # 4 + 20 + 5.00 + 0.20

    def test_opus_5(self):
        cost = turn_cost_usd("claude-opus-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 36.75)  # 5 + 25 + 6.25 + 0.50

    def test_opus_4_8(self):
        cost = turn_cost_usd("claude-opus-4-8",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 36.75)  # 5 + 25 + 6.25 + 0.50

    def test_opus_4_7(self):
        cost = turn_cost_usd("claude-opus-4-7",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 36.75)  # 5 + 25 + 6.25 + 0.50

    def test_opus_4_6(self):
        cost = turn_cost_usd("claude-opus-4-6",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 36.75)  # 5 + 25 + 6.25 + 0.50

    def test_opus_4_5(self):
        cost = turn_cost_usd("claude-opus-4-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 36.75)  # 5 + 25 + 6.25 + 0.50

    def test_sonnet_5_5(self):
        cost = turn_cost_usd("claude-sonnet-5-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 14.60)  # 2 + 10 + 2.50 + 0.10 (0.05x read)

    def test_sonnet_5(self):
        cost = turn_cost_usd("claude-sonnet-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 14.70)  # 2 + 10 + 2.50 + 0.20 (intro pricing)

    def test_sonnet_4_6(self):
        cost = turn_cost_usd("claude-sonnet-4-6",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 22.05)  # 3 + 15 + 3.75 + 0.30

    def test_haiku_5_5(self):
        # 3M-token prompt, so the long-prompt card: see LongPromptPricing.
        cost = turn_cost_usd("claude-haiku-5-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 3.675)  # 0.50 + 2.50 + 0.625 + 0.05

    def test_haiku_4_5(self):
        cost = turn_cost_usd("claude-haiku-4-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 7.35)   # 1 + 5 + 1.25 + 0.10

    def test_realistic_turn_counts(self):
        # Non-round counts so component/rate pairing is exercised, not just sums:
        # 1000*5 + 500*25 + 200*6.25 + 2000*0.50 = 19_750 per-MTok dollars.
        cost = turn_cost_usd("claude-opus-4-8", 1000, 500, 200, 2000)
        self.assertAlmostEqual(cost, 0.01975)

    def test_zero_tokens_is_zero_dollars(self):
        # A known model with no tokens prices to 0.0, not None.
        self.assertEqual(turn_cost_usd("claude-opus-4-8", 0, 0, 0, 0), 0.0)

    def test_very_large_token_counts_stay_finite(self):
        # A trillion tokens of each component is absurd but must not overflow to
        # inf/nan or lose precision: the float math stays finite and exact here.
        cost = turn_cost_usd("claude-opus-4-8", 10**12, 10**12, 10**12, 10**12)
        self.assertTrue(math.isfinite(cost))
        self.assertAlmostEqual(cost, 36_750_000.0)  # 36.75 * 10**12 / 10**6


class LongPromptPricing(unittest.TestCase):
    """Haiku 5.5 bills a request on one of two rate cards, picked by whether its
    prompt (input + cache write + cache read) is over 100,000 tokens."""

    def test_short_prompt_bills_every_component_at_the_short_card(self):
        # Prompt 25k + 25k + 50k = exactly 100k: not over, so the short card.
        # The 1M output does not count toward the prompt.
        # 25k*0.10 + 1M*0.50 + 25k*0.125 + 50k*0.01 = 506_125 per-MTok dollars.
        cost = turn_cost_usd("claude-haiku-5-5", 25_000, 1_000_000, 25_000, 50_000)
        self.assertAlmostEqual(cost, 0.506125)

    def test_threshold_is_over_100k_not_at_it(self):
        at = turn_cost_usd("claude-haiku-5-5", 100_000, 0, 0, 0)
        over = turn_cost_usd("claude-haiku-5-5", 100_001, 0, 0, 0)
        self.assertAlmostEqual(at, 0.01)           # 100k * $0.10
        self.assertAlmostEqual(over, 0.0500005)    # 100,001 * $0.50

    def test_cached_prompt_tokens_count_toward_the_threshold(self):
        # Only 1k uncached input, but 100.5k more read from or written to cache:
        # a 101.5k prompt, so input, output, and both cache sides go long.
        # 1k*0.50 + 1k*2.50 + 500*0.625 + 100k*0.05 = 8_312.5 per-MTok dollars.
        cost = turn_cost_usd("claude-haiku-5-5", 1_000, 1_000, 500, 100_000)
        self.assertAlmostEqual(cost, 0.0083125)

    def test_one_hour_write_on_the_long_card_is_twice_long_input(self):
        cost = turn_cost_usd("claude-haiku-5-5", 0, 0, 200_000, 0,
                             cache_write_1h_tokens=200_000)
        self.assertAlmostEqual(cost, 0.20)  # 200k * 2 * $0.50

    def test_dated_id_prices_via_normalized_form(self):
        cost = turn_cost_usd("claude-haiku-5-5-20261007", 50_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 0.005)  # 50k * $0.10

    def test_flat_rate_models_ignore_the_threshold(self):
        # Every other model bills its whole window at one rate.
        self.assertAlmostEqual(
            turn_cost_usd("claude-sonnet-5-5", 1_000_000, 0, 0, 0), 2.00)

    def test_tool_loop_turn_picks_the_card_per_request(self):
        # Two 60k-prompt requests in one turn: 120k summed, but each request is
        # under 100k, so both bill short ($0.0066), not long ($0.033).
        records = [
            typed("p1", "go"),
            assistant("a1", 60_000, 0, 0, 0, "tool_use", model="claude-haiku-5-5"),
            assistant("a2", 0, 0, 0, 60_000, "end_turn", model="claude-haiku-5-5"),
        ]
        (cost,) = turn_costs(segment_turns(records))
        self.assertAlmostEqual(turn_usd(cost), 0.0066)  # 60k*0.10 + 60k*0.01

    def test_mixed_card_turn_prices_each_request_on_its_own_card(self):
        # a1 is 150k (long, $0.075); a2 is 50k (short, $0.005).
        records = [
            typed("p1", "go"),
            assistant("a1", 150_000, 0, 0, 0, "tool_use", model="claude-haiku-5-5"),
            assistant("a2", 50_000, 0, 0, 0, "end_turn", model="claude-haiku-5-5"),
        ]
        (cost,) = turn_costs(segment_turns(records))
        self.assertAlmostEqual(turn_usd(cost), 0.08)


class OneHourCacheWrites(unittest.TestCase):
    """1-hour-TTL cache writes bill at 2x input, not the 5-minute 1.25x rate."""

    def test_one_hour_share_bills_at_twice_input(self):
        # opus-5: 1M written, all 1h -> $10.00 (2 x $5), not $6.25.
        cost = turn_cost_usd("claude-opus-5", 0, 0, 1_000_000, 0,
                             cache_write_1h_tokens=1_000_000)
        self.assertAlmostEqual(cost, 10.00)

    def test_split_write_prices_each_share_at_its_own_rate(self):
        # opus-5-5: 600k at 5m ($5/MTok) + 400k at 1h ($8/MTok) = $3.00 + $3.20.
        cost = turn_cost_usd("claude-opus-5-5", 0, 0, 1_000_000, 0,
                             cache_write_1h_tokens=400_000)
        self.assertAlmostEqual(cost, 6.20)

    def test_one_hour_share_is_clamped_to_the_total_write(self):
        # A malformed split larger than the write never prices extra tokens.
        cost = turn_cost_usd("claude-opus-5", 0, 0, 1_000_000, 0,
                             cache_write_1h_tokens=5_000_000)
        self.assertAlmostEqual(cost, 10.00)

    def test_transcript_split_reaches_the_turn_price(self):
        # End to end through parse_line: the nested cache_creation split must
        # survive accounting (deduped per message) and reach turn_usd.
        line = (
            '{"type":"assistant","message":{"id":"a1","role":"assistant",'
            '"model":"claude-opus-5","usage":{"input_tokens":0,"output_tokens":0,'
            '"cache_creation_input_tokens":1000000,"cache_read_input_tokens":0,'
            '"cache_creation":{"ephemeral_1h_input_tokens":1000000,'
            '"ephemeral_5m_input_tokens":0}}}}'
        )
        records = [typed("p1", "go"), parse_line(line), parse_line(line)]
        costs = turn_costs(segment_turns(records))
        self.assertEqual(costs[0].cache_creation_1h_input_tokens, 1_000_000)
        self.assertEqual(costs[0].turn_total, 1_000_000)  # a split, not extra
        self.assertAlmostEqual(turn_usd(costs[0]), 10.00)


class FastMode(unittest.TestCase):
    """``usage.speed == "fast"`` bills the whole row at the model's premium."""

    def test_fast_opus_5_5_is_twice_standard(self):
        cost = turn_cost_usd("claude-opus-5-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000,
                             fast=True)
        self.assertAlmostEqual(cost, 58.40)  # 2 x (4 + 20 + 5.00 + 0.20)

    def test_fast_opus_4_8_is_twice_standard(self):
        cost = turn_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000, 0, 0,
                             fast=True)
        self.assertAlmostEqual(cost, 60.00)  # $10 in + $50 out

    def test_fast_dated_opus_5_prices_via_normalized_form(self):
        cost = turn_cost_usd("claude-opus-5-20260101", 1_000_000, 1_000_000, 0, 0,
                             fast=True)
        self.assertAlmostEqual(cost, 60.00)  # $10 in + $50 out

    def test_fast_on_a_model_without_a_known_fast_rate_is_none(self):
        # Never the standard rate: that would under-report by the premium.
        self.assertIsNone(
            turn_cost_usd("claude-sonnet-5-5", 1_000_000, 0, 0, 0, fast=True))

    def test_transcript_speed_reaches_the_turn_price(self):
        line = (
            '{"type":"assistant","message":{"id":"a1","role":"assistant",'
            '"model":"claude-opus-5","stop_reason":"end_turn","usage":'
            '{"input_tokens":1000000,"output_tokens":0,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
            '"speed":"fast"}}}'
        )
        costs = turn_costs(segment_turns([typed("p1", "go"), parse_line(line)]))
        self.assertTrue(costs[0].fast)
        self.assertAlmostEqual(turn_usd(costs[0]), 10.00)


class UnknownModel(unittest.TestCase):
    def test_unknown_model_returns_none(self):
        self.assertIsNone(
            turn_cost_usd("gpt-99-turbo", 1_000_000, 1_000_000, 0, 0))

    def test_none_model_returns_none(self):
        # A turn with no usage-bearing record surfaces model=None: unpriceable.
        self.assertIsNone(turn_cost_usd(None, 100, 50, 0, 0))


class Normalization(unittest.TestCase):
    """A trailing -YYYYMMDD date suffix is stripped before the second lookup;
    $?/None only after the normalized form also misses."""

    def test_dated_model_id_prices_via_normalized_form(self):
        cost = turn_cost_usd("claude-haiku-4-5-20251001",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 7.35)

    def test_normalize_strips_only_a_date_suffix(self):
        self.assertEqual(normalize_model("claude-haiku-4-5-20251001"),
                         "claude-haiku-4-5")
        # No date suffix: unchanged (digits short of YYYYMMDD are not a date).
        self.assertEqual(normalize_model("claude-opus-4-8"), "claude-opus-4-8")

    def test_unknown_after_normalization_still_none(self):
        self.assertIsNone(turn_cost_usd("claude-mystery-9-20260101",
                                        1_000_000, 0, 0, 0))


class CostUsdPassthrough(unittest.TestCase):
    """An authoritative costUSD on the record wins over the table compute; it
    is never assumed to exist (absent -> table)."""

    def test_cost_usd_returned_verbatim(self):
        cost = turn_cost_usd("claude-opus-4-8", 1_000_000, 0, 0, 0,
                             cost_usd=1.23)
        self.assertEqual(cost, 1.23)  # not the table's 5.00

    def test_cost_usd_wins_even_for_unknown_model(self):
        self.assertEqual(
            turn_cost_usd("gpt-99-turbo", 0, 0, 0, 0, cost_usd=0.5), 0.5)

    def test_absent_cost_usd_falls_back_to_table(self):
        cost = turn_cost_usd("claude-opus-4-8", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 5.00)

    def test_cost_usd_passthrough_with_none_model(self):
        # cost_usd wins even when the model is None (otherwise unpriceable): an
        # authoritative costUSD is honored regardless of model knowledge.
        self.assertEqual(turn_cost_usd(None, 0, 0, 0, 0, cost_usd=1.23), 1.23)


class ModelThreading(unittest.TestCase):
    """The additive plumbing pricing depends on: parse_line retains
    message.model, and TurnCost.model is the LAST usage-bearing record's."""

    def test_parse_line_retains_model(self):
        line = (
            '{"type":"assistant","message":{"role":"assistant",'
            '"model":"claude-opus-4-8","content":[{"type":"text","text":"hi"}],'
            '"stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":2}}}'
        )
        rec = parse_line(line)
        self.assertEqual(rec.model, "claude-opus-4-8")

    def test_parse_line_model_none_when_absent(self):
        line = '{"type":"user","message":{"role":"user","content":"hello"}}'
        self.assertIsNone(parse_line(line).model)

    def test_turn_cost_carries_last_usage_bearing_records_model(self):
        # Mixed-model turn (e.g. a mid-turn model switch): the LAST
        # usage-bearing record's model wins; the model-less typed prompt and
        # the earlier assistant record do not.
        records = [
            typed("p1", "go"),
            assistant("a1", 10, 5, 0, 0, "tool_use", model="claude-haiku-4-5"),
            assistant("a2", 20, 7, 0, 0, "end_turn", model="claude-opus-4-8"),
        ]
        costs = turn_costs(segment_turns(records))
        self.assertEqual(len(costs), 1)
        self.assertEqual(costs[0].model, "claude-opus-4-8")

    def test_turn_with_no_usage_bearing_record_has_model_none(self):
        costs = turn_costs(segment_turns([typed("p1", "just a prompt")]))
        self.assertEqual(len(costs), 1)
        self.assertIsNone(costs[0].model)


class TurnUsd(unittest.TestCase):
    """turn_usd prices ONE turn off its own model, reusing the frozen table.

    This is the helper every dollar figure on screen goes through, so the
    unknown-model None contract is pinned here rather than at a render surface.
    """

    @staticmethod
    def _only_turn(records):
        (cost,) = turn_costs(segment_turns(records))
        return cost

    def test_prices_the_turns_four_components(self):
        # opus-4-8: (100*5 + 50*25 + 7*6.25 + 3*0.50) / 1e6 = 0.00179525
        cost = self._only_turn([
            typed("p1", "go"),
            assistant("a1", 100, 50, 7, 3, model="claude-opus-4-8"),
        ])
        self.assertAlmostEqual(turn_usd(cost), 0.00179525)

    def test_unknown_model_is_none_never_zero(self):
        # The fixture record leaves model None -> unpriceable. None is the
        # honest answer the renderer turns into "$?"; 0.0 would be a lie.
        cost = self._only_turn([typed("p1", "go"), assistant("a1", 100, 50, 7, 3)])
        self.assertIsNone(turn_usd(cost))

    def test_dated_model_id_still_prices(self):
        cost = self._only_turn([
            typed("p1", "go"),
            assistant("a1", 1_000_000, 0, 0, 0, model="claude-haiku-4-5-20251001"),
        ])
        self.assertAlmostEqual(turn_usd(cost), 1.00)


class SessionCostSummation(unittest.TestCase):
    """session_cost: per-turn dollars summed, with the partial-total flag.

    The single source of truth for a session's money, consumed verbatim by
    ``summarize_session`` and rendered as the block's ``Total:`` line.
    """

    @staticmethod
    def _cost(records):
        return session_cost(turn_costs(segment_turns(records)))

    def test_mixed_model_session_sums_per_turn_costs(self):
        # haiku 1M input ($1) + opus 1M input ($5) = $6. Aggregate tokens times
        # a single rate would give $10 (opus) or $2 (haiku) -- the figure only
        # comes out $6 when each turn is priced with its own model.
        total, unpriced = self._cost([
            typed("p1", "haiku turn"),
            assistant("a1", 1_000_000, 0, 0, 0, model="claude-haiku-4-5"),
            typed("p2", "opus turn"),
            assistant("a2", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
        ])
        self.assertAlmostEqual(total, 6.00)
        self.assertFalse(unpriced)

    def test_unpriceable_turn_is_excluded_and_flags_the_total_partial(self):
        # One token-bearing turn with no model: the total covers the priceable
        # turns only ($5) and carries the flag -- never a silent undercount
        # presented as complete.
        total, unpriced = self._cost([
            typed("p1", "mystery turn"),
            assistant("a1", 100, 50, 0, 0),  # model None, tokens > 0
            typed("p2", "opus turn"),
            assistant("a2", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
        ])
        self.assertAlmostEqual(total, 5.00)
        self.assertTrue(unpriced)

    def test_zero_token_in_flight_turn_never_flags_unpriced(self):
        # The just-opened prompt has no usage yet and no model. It costs $0 on
        # every model, so it must not raise the flag -- otherwise the marker
        # would flash on screen at the start of every single prompt.
        total, unpriced = self._cost([
            typed("p1", "opus turn"),
            assistant("a1", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
            typed("p2", "just started"),
        ])
        self.assertAlmostEqual(total, 5.00)
        self.assertFalse(unpriced)

    def test_trailing_synthetic_notice_keeps_the_turn_priced(self):
        # A real opus turn ends in Claude Code's zero-token "<synthetic>" notice.
        # Parsed end to end, the notice must not become the turn's model: the
        # turn prices at $5 and the total is not flagged partial.
        lines = [
            '{"type":"user","message":{"role":"user","content":"go"}}',
            '{"type":"assistant","message":{"id":"a1","role":"assistant",'
            '"model":"claude-opus-4-8","usage":{"input_tokens":1000000,'
            '"output_tokens":0}}}',
            '{"type":"assistant","message":{"id":"s1","role":"assistant",'
            '"model":"<synthetic>","usage":{"input_tokens":0,"output_tokens":0,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}',
        ]
        total, unpriced = self._cost([parse_line(line) for line in lines])
        self.assertAlmostEqual(total, 5.00)
        self.assertFalse(unpriced)

    def test_no_turns_is_zero_and_unflagged(self):
        self.assertEqual(session_cost([]), (0.0, False))


if __name__ == "__main__":
    unittest.main()
