"""Tests for cc_token_tracker.roster (the v0.5 multi-session roster view).

Snapshot-style layout tests render to plain text through a non-terminal rich
Console and assert on substrings, so they pin figures and markers without
chasing exact box-drawing geometry. Auto-follow is tested through the real
SessionCache over a temp projects tree.
"""

import io
import os
import tempfile
import time
import unittest

from rich.console import Console

from cc_token_tracker.accounting import SessionAccounting
from cc_token_tracker.display import Frame, RecentEntry
from cc_token_tracker.roster import (
    ROSTER_LIMIT,
    age_figure,
    build_roster_view,
    cost_figure,
    percent_figure,
    render_roster,
)
from cc_token_tracker.sessions import SessionCache, SessionSummary
from cc_token_tracker.turn_cost import TurnCost

NOW = 1_780_000_000.0

PROMPT = '{"type":"user","message":{"role":"user","content":"hi"}}'

WAITING_FRAME = Frame(delta=None, session_total=0, transcript_path=None)


def make_summary(**overrides):
    fields = dict(
        project="proj-a",
        file_name="s1.jsonl",
        total_tokens=123_456,
        total_cost_usd=1.2345,
        unpriced=False,
        context_used=98_304,
        context_limit=200_000,
        context_percent=49.152,
        last_write=NOW - 240,
        is_active=False,
    )
    fields.update(overrides)
    return SessionSummary(**fields)


def make_turn_cost(model="claude-opus-4-8", complete=True, input_tokens=1200,
                   cache_creation=300, cache_read=4000, output_tokens=800):
    total = input_tokens + cache_creation + cache_read + output_tokens
    return TurnCost(
        complete=complete,
        input_tokens=input_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        output_tokens=output_tokens,
        turn_total=total,
        accounting=SessionAccounting(),
        model=model,
    )


def make_frame():
    return Frame(
        delta=make_turn_cost(),
        session_total=6_300,
        transcript_path="/tmp/s1.jsonl",
        recent=(RecentEntry(cost=make_turn_cost(), text="fix the tests"),),
        recent_omitted=2,
        session_cost=0.1234,
        session_unpriced=False,
    )


def render_text(summaries, frame, **kwargs):
    kwargs.setdefault("now", NOW)
    panel = render_roster(summaries, frame, width=100, **kwargs)
    console = Console(width=100, file=io.StringIO(), force_terminal=False)
    console.print(panel)
    return console.file.getvalue()


def line_with(text, needle):
    matches = [line for line in text.splitlines() if needle in line]
    return matches


class FigureHelpers(unittest.TestCase):
    def test_percent_figure(self):
        self.assertEqual(percent_figure(None), "?")
        self.assertEqual(percent_figure(64.2), "64%")
        self.assertEqual(percent_figure(100.0), "100%")
        # Over 100: the number stays, the trailing ? marks the overflow.
        self.assertEqual(percent_figure(104.0), "104%?")
        self.assertEqual(percent_figure(100.4), "100%?")

    def test_cost_figure(self):
        self.assertEqual(cost_figure(1.234, False), "$1.23")
        self.assertEqual(cost_figure(0.0, False), "$0.00")
        # Nothing priceable: never a fake $0.00.
        self.assertEqual(cost_figure(0.0, True), "$?")
        # Partial: the figure is a floor, marked as such.
        self.assertEqual(cost_figure(1.234, True), "$1.23?")

    def test_age_figure(self):
        self.assertEqual(age_figure(30), "now")
        self.assertEqual(age_figure(240), "4m ago")
        self.assertEqual(age_figure(3_700), "1h ago")
        self.assertEqual(age_figure(90_000), "1d ago")


class CollapsedRows(unittest.TestCase):
    def test_columns_and_marker(self):
        active = make_summary(project="proj-live", is_active=True,
                              last_write=NOW - 5)
        idle = make_summary(project="proj-idle", file_name="s2.jsonl",
                            total_tokens=45_678, total_cost_usd=0.5,
                            context_percent=12.0, last_write=NOW - 240)
        text = render_text([active, idle], make_frame())

        for header in ("PROJECT", "TOTAL TOK", "COST", "CONTEXT", "LAST"):
            self.assertIn(header, text)
        (active_line,) = line_with(text, "▶")
        self.assertIn("proj-live", active_line)
        self.assertIn("active", active_line)
        (idle_line,) = line_with(text, "proj-idle")
        self.assertIn("45,678", idle_line)
        self.assertIn("$0.50", idle_line)
        self.assertIn("12%", idle_line)
        self.assertIn("4m ago", idle_line)

    def test_unknown_context_renders_question_mark(self):
        idle = make_summary(project="proj-odd", context_used=1_000,
                            context_limit=None, context_percent=None)
        active = make_summary(project="proj-live", is_active=True)
        text = render_text([active, idle], make_frame())
        (idle_line,) = line_with(text, "proj-odd")
        self.assertIn("?", idle_line)

    def test_overflow_percent_marker_survives_into_row(self):
        active = make_summary(project="proj-live", is_active=True,
                              context_used=208_000, context_limit=200_000,
                              context_percent=104.0)
        text = render_text([active], make_frame())
        (active_line,) = line_with(text, "▶")
        self.assertIn("104%?", active_line)

    def test_unpriced_cost_marker_survives_into_row(self):
        active = make_summary(project="proj-live", is_active=True)
        odd = make_summary(project="proj-odd", total_cost_usd=0.0,
                           unpriced=True)
        text = render_text([active, odd], make_frame())
        (odd_line,) = line_with(text, "proj-odd")
        self.assertIn("$?", odd_line)


class ActiveExpansion(unittest.TestCase):
    def test_context_gauge_and_reused_panel_sections(self):
        active = make_summary(project="proj-live", is_active=True)
        text = render_text([active], make_frame())

        self.assertIn("CONTEXT · 98,304 / 200,000 tokens", text)
        self.assertIn("█", text)  # the bar
        self.assertIn("49% · ~101k left", text)  # (200,000-98,304)//1000
        # The reused panel sections, with their own figures intact.
        self.assertIn("LAST PROMPT", text)
        self.assertIn("IN", text)
        self.assertIn("CACHE READ", text)
        self.assertIn("1,500", text)  # IN folds input + cache creation
        # RECENT strip removed product-wide in v0.6.0: the frame still carries
        # recent data (text + recent_omitted), but the roster no longer renders
        # any of it.
        self.assertNotIn("RECENT", text)
        self.assertNotIn("fix the tests", text)
        self.assertNotIn("+2 more", text)

    def test_unknown_limit_expansion_is_honest(self):
        active = make_summary(project="proj-live", is_active=True,
                              context_used=98_304, context_limit=None,
                              context_percent=None)
        text = render_text([active], make_frame())
        self.assertIn("CONTEXT · 98,304 / ? tokens", text)
        self.assertIn("context limit unknown", text)
        self.assertNotIn("█", text)  # no bar invented without a limit

    def test_overflow_expansion(self):
        active = make_summary(project="proj-live", is_active=True,
                              context_used=208_000, context_limit=200_000,
                              context_percent=104.0)
        text = render_text([active], make_frame())
        self.assertIn("104%? · ~0k left", text)

    def test_waiting_frame_expands_to_waiting_text(self):
        active = make_summary(project="proj-live", is_active=True)
        text = render_text([active], WAITING_FRAME)
        self.assertIn("waiting for first command", text)


class FooterAndCaps(unittest.TestCase):
    def test_footer_totals(self):
        active = make_summary(project="proj-live", is_active=True,
                              total_cost_usd=1.2345, total_tokens=300_000)
        idle = make_summary(project="proj-idle", total_cost_usd=0.5,
                            total_tokens=50_000)
        text = render_text([active, idle], make_frame())
        # Footer total is ACTIVE-ONLY now; both sessions are active (240s old),
        # so the active total equals the two-session sum. No session count.
        self.assertIn("active: $1.73 · 0.35M tok", text)
        self.assertNotIn("2 sessions", text)  # footer no longer shows a count
        self.assertNotIn("(+ unpriced)", text)

    def test_footer_unpriced_marker(self):
        active = make_summary(project="proj-live", is_active=True)
        odd = make_summary(project="proj-odd", unpriced=True)
        text = render_text([active, odd], make_frame())
        self.assertIn("(+ unpriced)", text)

    def test_single_session_roster_is_expanded_row_plus_footer(self):
        active = make_summary(project="proj-only", is_active=True)
        text = render_text([active], make_frame())
        self.assertIn("proj-only", text)
        self.assertIn("CONTEXT · 98,304 / 200,000 tokens", text)
        self.assertIn("LAST PROMPT", text)
        self.assertNotIn("RECENT", text)  # RECENT strip removed in v0.6.0
        # Footer is the active-only total, no session count (header has it).
        self.assertIn("active: $1.23 · 0.12M tok", text)
        self.assertNotIn("1 session", text)

    def test_more_than_ten_sessions_cap_with_more_line(self):
        # Spacing kept under the 600s active window (index*30, max 360s) so this
        # stays a pure cap test, independent of the liveness boundaries.
        summaries = [
            make_summary(project=f"proj-{index:02d}",
                         file_name=f"s{index:02d}.jsonl",
                         is_active=(index == 0),
                         last_write=NOW - index * 30,
                         total_tokens=10_000, total_cost_usd=0.1)
            for index in range(13)
        ]
        text = render_text(summaries, make_frame())

        self.assertEqual(ROSTER_LIMIT, 10)
        self.assertIn("proj-09", text)
        self.assertNotIn("proj-10", text)  # beyond the cap: hidden rows
        self.assertIn("+3 more", text)
        # Footer total is ACTIVE-ONLY; all 13 sessions are active here, and the
        # active rows hidden beyond the cap are still summed in. No session count.
        self.assertIn("active: $1.30 · 0.13M tok", text)
        self.assertNotIn("13 sessions", text)

    def test_dropped_session_excluded_from_cap_overflow(self):
        # 11 fresh/active sessions inside the 600s window plus one stale session
        # aged past the 720s dropped boundary: 12 discovered. The dropped one is
        # absent from the roster AND excluded from the active-only footer total.
        fresh = [
            make_summary(project=f"proj-{index:02d}",
                         file_name=f"s{index:02d}.jsonl",
                         is_active=(index == 0),
                         last_write=NOW - index * 30,
                         total_tokens=10_000, total_cost_usd=0.1)
            for index in range(11)
        ]
        dropped = make_summary(project="proj-dropped", file_name="dropped.jsonl",
                               last_write=NOW - 800,
                               total_tokens=50_000, total_cost_usd=0.5)
        summaries = fresh + [dropped]

        # Roster scope: the dropped session leaves; 11 remain, 10 shown.
        view = build_roster_view(summaries, now=NOW)
        self.assertEqual(len(view.sessions), 11)

        text = render_text(summaries, make_frame())
        self.assertIn("+1 more", text)           # 11 roster rows, 10 shown
        self.assertNotIn("proj-dropped", text)   # dropped row is gone
        # Footer total is ACTIVE-ONLY: the dropped session is excluded from it
        # too (not just from the roster). 11 active * 0.1 = $1.10, 11 * 10k =
        # 0.11M tok; the dropped session's $0.50 / 50k are NOT summed in.
        self.assertIn("active: $1.10 · 0.11M tok", text)
        self.assertNotIn("12 sessions", text)
        self.assertNotIn("$1.60", text)  # dropped session no longer in the total

    def test_empty_roster(self):
        text = render_text([], WAITING_FRAME)
        self.assertIn("no sessions in the last 7 days", text)
        # Footer: active-only total, no session count, even when empty.
        self.assertIn("active: $0.00 · 0.00M tok", text)

    def test_no_keybind_hints(self):
        active = make_summary(project="proj-live", is_active=True)
        text = render_text([active], make_frame()).lower()
        for hint in ("press", "quit", "[q]", "keys:"):
            self.assertNotIn(hint, text)


class AutoFollow(unittest.TestCase):
    """The ▶ row follows recency through the real cache, matching the live
    path's auto-follow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = self.tmp.name
        self.now = time.time()

    def write_transcript(self, project, name, age_seconds):
        project_dir = os.path.join(self.projects, project)
        os.makedirs(project_dir, exist_ok=True)
        path = os.path.join(project_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(PROMPT + "\n")
        stamp = self.now - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def render(self, cache):
        summaries = cache.summaries(now=self.now)
        panel = render_roster(summaries, WAITING_FRAME, width=100,
                              now=self.now)
        console = Console(width=100, file=io.StringIO(), force_terminal=False)
        console.print(panel)
        return console.file.getvalue()

    def test_marker_moves_when_another_session_becomes_newest(self):
        older = self.write_transcript("proj-a", "a.jsonl", age_seconds=200)
        self.write_transcript("proj-b", "b.jsonl", age_seconds=10)
        cache = SessionCache(self.projects)

        first = self.render(cache)
        (marker_line,) = line_with(first, "▶")
        self.assertIn("proj-b", marker_line)

        # proj-a becomes the most recently modified transcript.
        os.utime(older, (self.now - 1, self.now - 1))
        second = self.render(cache)
        (marker_line,) = line_with(second, "▶")
        self.assertIn("proj-a", marker_line)
        # And proj-b dropped back to a humanized age, not "active".
        (idle_line,) = line_with(second, "proj-b")
        self.assertNotIn("active", idle_line)


if __name__ == "__main__":
    unittest.main()
