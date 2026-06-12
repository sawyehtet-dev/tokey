"""Tests for cc_token_tracker.pricing and the COST render path.

Pure fixtures throughout: known token counts against the published per-MTok
rates, the unknown-model None contract, and the "$?" render path. The model
plumbing (parser field, last-usage-bearing-record-wins on TurnCost) is pinned
here too since pricing is its only consumer.
"""

import unittest

from rich.console import Console

from cc_token_tracker import display
from cc_token_tracker.parser import TranscriptRecord, Usage, parse_line
from cc_token_tracker.pricing import normalize_model, turn_cost_usd
from cc_token_tracker.reader import ReadResult
from cc_token_tracker.segmentation import segment_turns
from cc_token_tracker.turn_cost import turn_costs


def typed(mid, text):
    return TranscriptRecord(type="user", message_id=mid, role="user", text=text)


def assistant(mid, input_tokens, output_tokens, cache_creation, cache_read,
              stop_reason="end_turn", model=None):
    return TranscriptRecord(
        type="assistant", message_id=mid, role="assistant", stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                    cache_creation_input_tokens=cache_creation,
                    cache_read_input_tokens=cache_read),
        model=model,
    )


class KnownModels(unittest.TestCase):
    """One per model: 1M tokens of each component prices to the four per-MTok
    rates summed, so a wrong rate in any single cell cannot pass."""

    def test_fable_5(self):
        cost = turn_cost_usd("claude-fable-5",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 73.50)  # 10 + 50 + 12.50 + 1.00

    def test_fable_5_dated_id_prices_via_normalized_form(self):
        cost = turn_cost_usd("claude-fable-5-20260601",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 73.50)

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

    def test_sonnet_4_6(self):
        cost = turn_cost_usd("claude-sonnet-4-6",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 22.05)  # 3 + 15 + 3.75 + 0.30

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


class RecentAndSessionCost(unittest.TestCase):
    """RECENT rows lead with the turn's own dollar cost ("$?" when unpriceable,
    same rule as the hero COST cell); SESSION TOTAL gains a TOTAL COST row
    summing per-turn dollars, marked "(+ unpriced)" when a token-bearing turn
    could not be priced. Structure only."""

    @staticmethod
    def _text(records, width=80):
        frame = display.compute_frame(
            ReadResult(records=records, transcript_path="/x/t.jsonl"))
        console = Console(width=width)
        with console.capture() as cap:
            console.print(display.render_panel(frame))
        return cap.get()

    def test_recent_turn_renders_its_own_cost(self):
        # haiku turn behind an opus hero: the RECENT row prices with ITS model
        # ($1.0000 for 1M input on haiku), not the hero's.
        out = self._text([
            typed("p1", "old prompt"),
            assistant("a1", 1_000_000, 0, 0, 0, model="claude-haiku-4-5"),
            typed("p2", "hero prompt"),
            assistant("a2", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
        ])
        self.assertIn("RECENT", out)
        self.assertIn("$1.0000", out)   # the recent haiku turn's own cost
        self.assertNotIn("$?", out)     # everything priceable

    def test_unknown_model_recent_turn_renders_dollar_question(self):
        # The recent turn has no model -> "$?" in its row, never $0.00. The
        # hero IS priced, so the only "$?" on screen is the recent row's.
        out = self._text([
            typed("p1", "old prompt"),
            assistant("a1", 100, 50, 0, 0),  # model None: unpriceable
            typed("p2", "hero prompt"),
            assistant("a2", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
        ])
        self.assertIn("RECENT", out)
        self.assertIn("$?", out)
        self.assertNotIn("$0.0000", out)  # unpriceable never renders as zero

    def test_mixed_model_session_sums_per_turn_costs(self):
        # haiku 1M input ($1) + opus 1M input ($5) = $6.0000. Aggregate tokens
        # times a single rate would give $10 (opus) or $2 (haiku) -- the figure
        # only comes out $6 when each turn is priced with its own model.
        out = self._text([
            typed("p1", "haiku turn"),
            assistant("a1", 1_000_000, 0, 0, 0, model="claude-haiku-4-5"),
            typed("p2", "opus turn"),
            assistant("a2", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
        ])
        self.assertIn("TOTAL COST", out)
        self.assertIn("$6.0000", out)
        self.assertNotIn("(+ unpriced)", out)

    def test_unpriceable_turn_marks_total_partial(self):
        # One token-bearing turn with no model: the total is the priceable
        # turns only ($5.0000) and carries the marker -- never a silent
        # undercount presented as complete.
        out = self._text([
            typed("p1", "mystery turn"),
            assistant("a1", 100, 50, 0, 0),  # model None, tokens > 0
            typed("p2", "opus turn"),
            assistant("a2", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
        ])
        self.assertIn("TOTAL COST", out)
        self.assertIn("$5.0000", out)
        self.assertIn("(+ unpriced)", out)


class ModelTags(unittest.TestCase):
    """The RECENT model tag: _model_tag abbreviates every known model to a
    <=6 char tag, "?" for unknown/absent, and the tag renders between the cost
    figure and the snippet."""

    @staticmethod
    def _text(records, width=80):
        frame = display.compute_frame(
            ReadResult(records=records, transcript_path="/x/t.jsonl"))
        console = Console(width=width)
        with console.capture() as cap:
            console.print(display.render_panel(frame))
        return cap.get()

    def test_known_model_tags(self):
        for model, tag in [
            ("claude-fable-5", "fab5"),
            ("claude-opus-4-8", "op4.8"),
            ("claude-opus-4-7", "op4.7"),
            ("claude-opus-4-6", "op4.6"),
            ("claude-opus-4-5", "op4.5"),
            ("claude-sonnet-4-6", "sn4.6"),
            ("claude-haiku-4-5", "hk4.5"),
        ]:
            self.assertEqual(display._model_tag(model), tag)
            self.assertLessEqual(len(tag), 6)

    def test_unknown_or_absent_model_tags_question(self):
        self.assertEqual(display._model_tag(None), "?")
        self.assertEqual(display._model_tag("gpt-99-turbo"), "?")

    def test_dated_id_tags_via_normalized_form(self):
        self.assertEqual(display._model_tag("claude-haiku-4-5-20251001"),
                         "hk4.5")

    def test_recent_row_renders_tag_between_cost_and_snippet(self):
        out = self._text([
            typed("p1", "old prompt"),
            assistant("a1", 1_000_000, 0, 0, 0, model="claude-haiku-4-5"),
            typed("p2", "hero prompt"),
            assistant("a2", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
        ])
        line = next(ln for ln in out.splitlines() if "old prompt" in ln)
        self.assertIn("hk4.5", line)
        self.assertLess(line.index("$1.0000"), line.index("hk4.5"))
        self.assertLess(line.index("hk4.5"), line.index("old prompt"))

    def test_unknown_model_recent_row_renders_question_tag(self):
        # The row reads "$?  ?  snippet": the cost cell's "$?" plus the tag's
        # own standalone "?" on the same line.
        out = self._text([
            typed("p1", "old prompt"),
            assistant("a1", 100, 50, 0, 0),  # model None
            typed("p2", "hero prompt"),
            assistant("a2", 1_000_000, 0, 0, 0, model="claude-opus-4-8"),
        ])
        line = next(ln for ln in out.splitlines() if "old prompt" in ln)
        self.assertIn("$?", line)
        self.assertGreaterEqual(line.count("?"), 2)  # cost "$?" AND tag "?"


class RenderCost(unittest.TestCase):
    """The hero line's COST cell: a dollar figure for a known model, "$?" for
    an unknown one, token figures unchanged either way. Structure only."""

    @staticmethod
    def _text(records, width=80):
        frame = display.compute_frame(
            ReadResult(records=records, transcript_path="/x/t.jsonl"))
        console = Console(width=width)
        with console.capture() as cap:
            console.print(display.render_panel(frame))
        return cap.get()

    def test_unknown_model_renders_dollar_question(self):
        # model defaults to None on the fixture record -> unpriceable -> "$?",
        # while the token figures render exactly as before.
        out = self._text([typed("p1", "go"), assistant("a1", 100, 50, 7, 3)])
        self.assertIn("$?", out)
        self.assertIn("COST", out)
        self.assertIn("107", out)  # IN = input + cache_creation, unchanged
        self.assertIn("50", out)   # OUT unchanged

    def test_known_model_renders_dollar_figure(self):
        # opus-4-8: (100*5 + 50*25 + 7*6.25 + 3*0.50) / 1e6 = 0.00179525
        out = self._text([
            typed("p1", "go"),
            assistant("a1", 100, 50, 7, 3, model="claude-opus-4-8"),
        ])
        self.assertIn("$0.0018", out)
        self.assertNotIn("$?", out)

    def test_waiting_frame_renders_no_cost_cell(self):
        # No delta -> no hero figures at all; the COST label must not appear.
        out = self._text([])
        self.assertNotIn("COST", out)
        self.assertIn("waiting for first command", out)


if __name__ == "__main__":
    unittest.main()
