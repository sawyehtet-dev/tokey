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

from rich.console import Console

from cc_token_tracker import display
from cc_token_tracker.accounting import account_usage
from cc_token_tracker.segmentation import segment_turns
from cc_token_tracker.turn_cost import turn_costs
from conftest import assistant, prompt, read_result, typed


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


class RunLoop(unittest.TestCase):
    def test_run_resolves_active_transcript_and_exits_clean(self):
        # run takes no pointer/config: each tick resolves the active transcript
        # directly by recency (find_active_transcript). We do not run the real
        # loop: a stubbed find_active_transcript records the call and raises
        # KeyboardInterrupt to stop after the first tick, which also exercises
        # the clean exit-0 path. stdout is swallowed so the terminal control
        # codes do not leak into test output.
        captured = {}

        def stub_find_active_transcript():
            captured["called"] = True
            raise KeyboardInterrupt

        with mock.patch.object(display, "find_active_transcript",
                               stub_find_active_transcript):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = display.run()

        self.assertTrue(captured.get("called"))
        self.assertEqual(rc, 0)


class RenderPanel(unittest.TestCase):
    # Render layer only: assert render_panel returns a non-None renderable and
    # does not raise. Text and styling are cosmetic and deliberately not pinned.
    def test_completed_delta(self):
        records = [prompt("p1"), assistant("a1", 100, 50, 7, 3)]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        self.assertTrue(frame.delta.complete)
        self.assertIsNotNone(display.render_panel(frame))

    def test_completed_delta_with_flash(self):
        records = [prompt("p1"), assistant("a1", 100, 50, 7, 3)]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        self.assertIsNotNone(display.render_panel(frame, flash=True))

    def test_in_flight_delta(self):
        records = [prompt("p1"),
                   assistant("a1", 10, 5, 0, 0, stop_reason="tool_use")]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        self.assertFalse(frame.delta.complete)
        self.assertIsNotNone(display.render_panel(frame))

    def test_none_delta(self):
        records = [assistant("a1", 100, 50, 0, 0)]  # no prompt opens a turn
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        self.assertIsNone(frame.delta)
        self.assertIsNotNone(display.render_panel(frame))

    def test_waiting_frame_no_path(self):
        # None delta and None transcript_path (no subtitle) still renders.
        frame = display.compute_frame(read_result([], None))
        self.assertIsNone(frame.delta)
        self.assertIsNone(frame.transcript_path)
        self.assertIsNotNone(display.render_panel(frame))


class FrameRecentShape(unittest.TestCase):
    # Data-shape only (v0.2 history view): Frame carries a `recent` tuple of
    # RecentEntry, each wrapping the existing TurnCost. Nothing populates or
    # renders it yet; these tests pin the shape and its immutable default.
    def test_recent_defaults_to_empty(self):
        frame = display.compute_frame(
            read_result([prompt("p1"), assistant("a1", 100, 50, 0, 0)],
                        "/x/t.jsonl"))
        self.assertEqual(frame.recent, ())

    def test_recent_carries_entry_wrapping_turncost(self):
        # Build a real TurnCost via the pipeline, wrap it in a RecentEntry, and
        # confirm both the snippet text and the wrapped cost survive on Frame.
        records = [prompt("p1"), assistant("a1", 100, 50, 7, 3)]
        cost = display.compute_frame(read_result(records, "/x/t.jsonl")).delta
        self.assertIsNotNone(cost)

        entry = display.RecentEntry(cost=cost, text="hi")
        frame = display.Frame(delta=None, session_total=0,
                              transcript_path=None, recent=(entry,))

        self.assertEqual(frame.recent[0].text, "hi")
        self.assertIs(frame.recent[0].cost, cost)
        self.assertEqual(frame.recent[0].cost.turn_total, cost.turn_total)


class RecentPopulation(unittest.TestCase):
    """compute_frame populates Frame.recent: completed turns behind the hero,
    newest-first, capped, each carrying that turn's TurnCost and snippet."""

    # Distinct per-turn costs (turn_total differs each turn) and distinct texts,
    # so a wrong slice or off-by-one cannot accidentally pass. Token n*10/n gives
    # turn_total 11, 22, 33, 44 for the four completed turns. The trailing turn
    # is in-flight (no end_turn).
    def _records(self):
        return [
            typed("p1", "alpha one"), assistant("a1", 10, 1, 0, 0),
            typed("p2", "  beta\n\ttwo   three  "), assistant("a2", 20, 2, 0, 0),
            typed("p3", "gamma"), assistant("a3", 30, 3, 0, 0),
            typed("p4", "delta four"), assistant("a4", 40, 4, 0, 0),   # newest completed
            typed("p5", "echo running"),
            assistant("a5", 5, 0, 0, 0, stop_reason="tool_use"),       # in-flight (the hero)
        ]

    def test_completed_prompt_in_recent_while_next_prompt_in_flight(self):
        # 0.3.1 regression: prompt 1 COMPLETE, prompt 2 IN-FLIGHT (no end_turn).
        # Prompt 1 must already be in RECENT -- it appears the instant prompt 2
        # starts, not when prompt 2 finishes. Before the fix recent was empty
        # here: the newest completed turn was dropped as "the hero" even though
        # the in-flight turn is the actual hero/delta.
        records = [
            typed("p1", "first done"), assistant("a1", 100, 50, 0, 0),
            typed("p2", "second running"),
            assistant("a2", 10, 5, 0, 0, stop_reason="tool_use"),
        ]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))

        self.assertFalse(frame.delta.complete)  # hero is the in-flight prompt 2
        self.assertEqual([e.text for e in frame.recent], ["first done"])
        self.assertEqual(frame.recent_omitted, 0)

    def test_hero_excluded_and_newest_first(self):
        frame = display.compute_frame(read_result(self._records(), "/x/t.jsonl"))
        texts = [e.text for e in frame.recent]

        # Hero is the in-flight trailing turn ("echo running", the delta), so it
        # is NOT a recent entry. Every COMPLETED turn -- including the newest,
        # "delta four" -- is in recent the instant the next prompt starts.
        self.assertNotIn("echo running", texts)
        # Completed turns, newest-first. Note "beta two three" proves
        # whitespace/newlines collapsed to single spaces.
        self.assertEqual(texts, ["delta four", "gamma", "beta two three", "alpha one"])

    def test_in_flight_is_the_delta_not_a_recent_entry(self):
        # The trailing in-flight turn is the delta (costs[-1]) and stays incomplete;
        # because IT -- not the newest completed turn -- is the hero, every
        # completed turn is in recent.
        frame = display.compute_frame(read_result(self._records(), "/x/t.jsonl"))
        self.assertIsNotNone(frame.delta)
        self.assertFalse(frame.delta.complete)
        self.assertEqual(len(frame.recent), 4)  # T4, T3, T2, T1 (all completed)

    def test_cost_equals_turn_costs_for_that_turn(self):
        # Each entry's cost equals the pipeline's TurnCost for that turn (reused,
        # not a hand-recomputed number). costs index: T1=0, T2=1, T3=2, T4=3.
        records = self._records()
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        costs = turn_costs(segment_turns(records))

        self.assertEqual(frame.recent[0].cost, costs[3])  # delta four -> turn_total 44
        self.assertEqual(frame.recent[1].cost, costs[2])  # gamma      -> turn_total 33
        self.assertEqual(frame.recent[2].cost, costs[1])  # beta       -> turn_total 22
        self.assertEqual(frame.recent[3].cost, costs[0])  # alpha      -> turn_total 11
        self.assertEqual(
            [e.cost.turn_total for e in frame.recent], [44, 33, 22, 11]
        )

    def test_capped_at_recent_limit_newest_first(self):
        # More completed turns than the cap: keep exactly RECENT_LIMIT, the
        # newest behind the hero, newest-first. Build 7 completed turns -> hero
        # is turn 7; recent must be turns 6,5,4,3,2 (5 of them), turn 1 dropped.
        records = []
        for i in range(1, 8):  # 7 completed turns
            records += [typed(f"p{i}", f"turn {i}"),
                        assistant(f"a{i}", i, 0, 0, 0)]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))

        self.assertEqual(len(frame.recent), display.RECENT_LIMIT)
        self.assertEqual(display.RECENT_LIMIT, 5)
        self.assertEqual(
            [e.text for e in frame.recent],
            ["turn 6", "turn 5", "turn 4", "turn 3", "turn 2"],
        )

    def test_fewer_than_two_completed_turns_is_empty(self):
        # One completed turn = hero only, nothing behind it.
        records = [typed("p1", "solo"), assistant("a1", 10, 1, 0, 0)]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        self.assertEqual(frame.recent, ())


class RecentOmitted(unittest.TestCase):
    """compute_frame exposes recent_omitted: completed prompts that are neither
    the hero nor in the capped recent tuple. A pure count off the SAME completed
    set the recent slice uses. The boundary -- exactly hero + RECENT_LIMIT shown,
    so 0 hidden (not 1) -- is the one that matters and is pinned hard."""

    @staticmethod
    def _completed(n):
        # n back-to-back COMPLETED turns, with distinct per-turn token totals
        # (turn i has total i) and distinct texts, so an off-by-one in the count
        # cannot pass by accident.
        records = []
        for i in range(1, n + 1):
            records += [typed(f"p{i}", f"turn {i}"),
                        assistant(f"a{i}", i, 0, 0, 0)]
        return records

    def test_more_than_six_completed_counts_remainder(self):
        # 9 completed: hero(1) + recent(RECENT_LIMIT=5) shown -> 9-1-5 = 3 hidden.
        frame = display.compute_frame(read_result(self._completed(9), "/x/t.jsonl"))
        self.assertEqual(len(frame.recent), display.RECENT_LIMIT)
        self.assertEqual(frame.recent_omitted, 3)

    def test_one_over_boundary_omits_one(self):
        # 7 completed: hero(1) + 5 shown -> exactly 1 hidden. The tight off-by-one
        # guard sitting right above the boundary.
        frame = display.compute_frame(read_result(self._completed(7), "/x/t.jsonl"))
        self.assertEqual(len(frame.recent), display.RECENT_LIMIT)
        self.assertEqual(frame.recent_omitted, 1)

    def test_exactly_hero_plus_limit_omits_zero(self):
        # BOUNDARY (the load-bearing case): 6 completed = hero(1) + RECENT_LIMIT(5)
        # shown, nothing behind them. Must be 0, NOT 1.
        frame = display.compute_frame(read_result(self._completed(6), "/x/t.jsonl"))
        self.assertEqual(len(frame.recent), display.RECENT_LIMIT)
        self.assertEqual(display.RECENT_LIMIT, 5)
        self.assertEqual(frame.recent_omitted, 0)

    def test_three_completed_omits_zero(self):
        # 3 completed: hero(1) + 2 shown, none hidden -> 0.
        frame = display.compute_frame(read_result(self._completed(3), "/x/t.jsonl"))
        self.assertEqual(len(frame.recent), 2)
        self.assertEqual(frame.recent_omitted, 0)

    def test_no_completed_turn_omits_zero(self):
        # A single in-flight turn: no hero at all -> recent empty, omitted 0
        # (the field's default also holds).
        records = [typed("p1", "solo running"),
                   assistant("a1", 5, 0, 0, 0, stop_reason="tool_use")]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        self.assertEqual(frame.recent, ())
        self.assertEqual(frame.recent_omitted, 0)


class RenderRecent(unittest.TestCase):
    """Render layer for the RECENT section. We assert STRUCTURE in the rendered
    text (labels present/absent, snippet order, truncation), never pixels. The
    real visual proof is live; these just pin behavior that can regress silently.
    """

    @staticmethod
    def _text(frame, width=80):
        console = Console(width=width)
        with console.capture() as cap:
            console.print(display.render_panel(frame))
        return cap.get()

    def test_three_recent_rows_in_given_order(self):
        # Four completed turns -> hero + three recent (newest-first). The three
        # snippets must appear in recent order; the hero's own prompt text is not
        # rendered as a row (the hero shows figures only).
        records = [
            typed("p1", "alpha snippet"), assistant("a1", 10, 1, 0, 0),
            typed("p2", "bravo snippet"), assistant("a2", 20, 2, 0, 0),
            typed("p3", "charlie snippet"), assistant("a3", 30, 3, 0, 0),
            typed("p4", "hero prompt text"), assistant("a4", 40, 4, 0, 0),
        ]
        out = self._text(display.compute_frame(read_result(records, "/x/t.jsonl")))

        self.assertIn("RECENT", out)
        i_c = out.index("charlie snippet")
        i_b = out.index("bravo snippet")
        i_a = out.index("alpha snippet")
        self.assertLess(i_c, i_b)  # newest-first: charlie, bravo, alpha
        self.assertLess(i_b, i_a)
        self.assertNotIn("hero prompt text", out)  # hero text is not a recent row
        # The leading figure is now the turn's dollar cost; these model-less
        # fixtures are unpriceable, so each row leads with "$?".
        self.assertIn("$?", out)

    def test_empty_recent_renders_no_section(self):
        # One completed turn => recent empty. No RECENT label, no placeholder,
        # and SESSION TOTAL still present (v0.1 layout not regressed).
        records = [typed("p1", "solo"), assistant("a1", 10, 1, 0, 0)]
        out = self._text(display.compute_frame(read_result(records, "/x/t.jsonl")))

        self.assertNotIn("RECENT", out)
        self.assertIn("SESSION TOTAL", out)

    def test_waiting_frame_has_no_recent_section(self):
        # The waiting/no-delta state is unchanged: renders, no RECENT, no raise.
        out = self._text(display.compute_frame(read_result([], None)))
        self.assertNotIn("RECENT", out)
        self.assertIn("waiting for first command", out)

    def test_long_snippet_truncated_not_raised(self):
        # A snippet far wider than the panel is shortened with an ellipsis and
        # never wraps; the cost figure stays fully visible. No exception.
        long = "x" * 300
        records = [
            typed("p1", long), assistant("a1", 10, 1, 0, 0),
            typed("p2", "hero"), assistant("a2", 20, 2, 0, 0),
        ]
        out = self._text(display.compute_frame(read_result(records, "/x/t.jsonl")),
                         width=50)

        self.assertIn("…", out)      # ellipsis -> truncation happened
        self.assertNotIn(long, out)        # full snippet not present
        self.assertIn("$?", out)           # cost figure (unpriceable) still visible


class RenderOmitted(unittest.TestCase):
    """Render layer for the '+N more' overflow line. The count is read straight
    off frame.recent_omitted -- the renderer does no counting. Structure only
    (line present/absent and its text), never pixels."""

    @staticmethod
    def _text(frame, width=80):
        console = Console(width=width)
        with console.capture() as cap:
            console.print(display.render_panel(frame))
        return cap.get()

    def _frame(self, *, omitted, with_recent=True):
        # A coherent frame built through the pipeline (real TurnCost), with the
        # overflow count set directly so we exercise the renderer in isolation.
        records = [prompt("p1"), assistant("a1", 100, 50, 7, 3)]
        cost = display.compute_frame(read_result(records, "/x/t.jsonl")).delta
        recent = ((display.RecentEntry(cost=cost, text="alpha snippet"),)
                  if with_recent else ())
        return display.Frame(delta=cost, session_total=999,
                             transcript_path="/x/t.jsonl",
                             recent=recent, recent_omitted=omitted)

    def test_positive_count_renders_plus_n_more(self):
        out = self._text(self._frame(omitted=12))
        self.assertIn("+12 more", out)

    def test_zero_count_renders_no_line(self):
        # No overflow line at all when the count is 0 (snippet has no 'more').
        out = self._text(self._frame(omitted=0))
        self.assertNotIn("more", out)

    def test_empty_recent_has_no_line_even_if_count_set(self):
        # By construction the line is gated on the RECENT section existing: a
        # frame with no recent rows shows nothing, proving the gate not the count.
        out = self._text(self._frame(omitted=12, with_recent=False))
        self.assertNotIn("+12 more", out)
        self.assertNotIn("RECENT", out)


class RenderWidthCap(unittest.TestCase):
    """Panel width cap: render_panel(width=W) renders at exactly W regardless of
    a wider console, and snippet truncation measures against the panel, not the
    terminal. Structure (line widths / ellipsis), never pixels."""

    @staticmethod
    def _capture(frame, *, panel_width, console_width):
        console = Console(width=console_width)
        with console.capture() as cap:
            console.print(display.render_panel(frame, width=panel_width))
        return cap.get()

    def test_capped_width_does_not_stretch_to_console(self):
        # Console is 200 wide; the panel asked for 80 must render at 80, not 200.
        records = [
            typed("p1", "alpha"), assistant("a1", 10, 1, 0, 0),
            typed("p2", "bravo"), assistant("a2", 20, 2, 0, 0),
            typed("p3", "hero"), assistant("a3", 30, 3, 0, 0),
        ]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        out = self._capture(frame, panel_width=80, console_width=200)

        widths = [len(ln.rstrip()) for ln in out.splitlines() if ln.strip()]
        self.assertEqual(max(widths), 80)  # bounded by the panel, not the console
        self.assertIn("Tokey", out)        # still a real, populated panel
        self.assertIn("RECENT", out)

    def test_truncation_measures_against_panel_not_console(self):
        # A long snippet on a WIDE console but a NARROW panel must ellipsize at
        # the panel edge -- proves truncation composes with the cap, no hardcoded
        # char count.
        long = "x" * 300
        records = [
            typed("p1", long), assistant("a1", 10, 1, 0, 0),
            typed("p2", "hero"), assistant("a2", 20, 2, 0, 0),
        ]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        out = self._capture(frame, panel_width=40, console_width=200)

        widths = [len(ln.rstrip()) for ln in out.splitlines() if ln.strip()]
        self.assertEqual(max(widths), 40)  # capped at the panel, not 200
        self.assertIn("…", out)            # truncated at the panel edge
        self.assertNotIn(long, out)

    def test_width_none_still_renders(self):
        # The default (no width) path is unchanged: expands to fill, still renders.
        records = [typed("p1", "solo"), assistant("a1", 10, 1, 0, 0)]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        self.assertIsNotNone(display.render_panel(frame))


class RenderPolish(unittest.TestCase):
    """Render-only polish: hero field dividers, magenta RECENT cost figures, and
    the SESSION TOTAL reflowed to a 'TOTAL TOKENS' label + right-aligned value.
    Structure only (labels/values/separators present, order preserved); the look
    itself is verified live, not pinned to pixels."""

    @staticmethod
    def _text(frame, width=80):
        console = Console(width=width)
        with console.capture() as cap:
            console.print(display.render_panel(frame))
        return cap.get()

    def test_session_total_row_has_label_and_value(self):
        # The session-total row carries the 'TOTAL TOKENS' label and the same
        # value the frame exposes; the 'SESSION TOTAL' section header is kept.
        records = [prompt("p1"), assistant("a1", 100, 50, 7, 3)]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        out = self._text(frame)

        self.assertIn("TOTAL TOKENS", out)
        self.assertIn(f"{frame.session_total:,}", out)  # reads off frame value
        self.assertIn("SESSION TOTAL", out)             # section header unchanged

    def test_hero_fields_separated_by_divider(self):
        # A completed delta renders IN/OUT/CACHE READ with a thin divider between
        # them; the labels and figures themselves are untouched.
        records = [prompt("p1"), assistant("a1", 100, 50, 7, 3)]
        frame = display.compute_frame(read_result(records, "/x/t.jsonl"))
        out = self._text(frame)

        self.assertIn("│", out)            # divider between the three fields
        self.assertIn("CACHE READ", out)   # labels unchanged
        self.assertIn("107", out)          # IN = input + cache_creation, unchanged

    def test_recent_rows_render_in_order_with_costs(self):
        # Recent costs still render in the given (newest-first) order alongside
        # their snippets; the magenta restyle does not drop or reorder them.
        records = [
            typed("p1", "alpha snippet"), assistant("a1", 10, 1, 0, 0),
            typed("p2", "bravo snippet"), assistant("a2", 20, 2, 0, 0),
            typed("p3", "charlie snippet"), assistant("a3", 30, 3, 0, 0),
            typed("p4", "hero prompt"), assistant("a4", 40, 4, 0, 0),
        ]
        out = self._text(display.compute_frame(read_result(records, "/x/t.jsonl")))

        self.assertLess(out.index("charlie snippet"), out.index("bravo snippet"))
        self.assertLess(out.index("bravo snippet"), out.index("alpha snippet"))
        # Leading figure is now the per-turn dollar cost ("$?" here: no model).
        self.assertIn("$?", out)


class EntryPoint(unittest.TestCase):
    def test_main_is_callable(self):
        # Pins the console_scripts target (tokey -> display:main) so the
        # mapping cannot silently point at a missing symbol. We do NOT call
        # main(): it runs run()'s infinite poll loop and would hang the suite.
        from cc_token_tracker.display import main

        self.assertTrue(callable(main))


if __name__ == "__main__":
    unittest.main()
