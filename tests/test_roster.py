"""Tests for cc_token_tracker.roster (the v0.6 all-expanded roster view).

Snapshot-style layout tests render to plain text through a non-terminal rich
Console and assert on substrings, so they pin figures and markers without
chasing exact box-drawing geometry. Auto-follow is tested through the real
SessionCache over a temp projects tree.
"""

import contextlib
import functools
import io
import os
import tempfile
import time
import unittest
from unittest import mock

from rich.cells import cell_len
from rich.console import Console

from cc_token_tracker import __version__, roster
from cc_token_tracker import markers as markers_mod
from cc_token_tracker.roster import (
    ROSTER_LIMIT,
    _bar,
    _context_model_label,
    _credits_row,
    _project_title,
    _reset_text,
    account_usage_requested,
    build_roster_view,
    main,
    percent_figure,
    render_roster,
    run,
    version_requested,
)
from cc_token_tracker.sessions import SessionCache, SessionSummary
from cc_token_tracker.usage import USAGE_ENV_VAR, AccountUsage, Credits, UsageWindow

NOW = 1_780_000_000.0

PROMPT = '{"type":"user","message":{"role":"user","content":"hi"}}'


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
        context_model="claude-opus-4-8",
        last_write=NOW - 240,
        is_active=False,
        last_cost_usd=0.142,
        last_input_tokens=12_400,
        last_output_tokens=3_200,
        last_cache_read_tokens=8_100,
    )
    fields.update(overrides)
    return SessionSummary(**fields)


def render_text(summaries, **kwargs):
    kwargs.setdefault("now", NOW)
    panel = render_roster(summaries, width=100, **kwargs)
    console = Console(width=100, file=io.StringIO(), force_terminal=False)
    console.print(panel)
    return console.file.getvalue()


def line_with(text, needle):
    return [line for line in text.splitlines() if needle in line]


class FigureHelpers(unittest.TestCase):
    def test_percent_figure(self):
        self.assertEqual(percent_figure(None), "?")
        self.assertEqual(percent_figure(64.2), "64%")
        self.assertEqual(percent_figure(100.0), "100%")
        # Over 100: the number stays, the trailing ? marks the overflow.
        self.assertEqual(percent_figure(104.0), "104%?")
        self.assertEqual(percent_figure(100.4), "100%?")

    def test_context_model_label(self):
        # claude- family prefix and any date suffix are stripped.
        self.assertEqual(_context_model_label("claude-opus-4-8"), "opus-4-8")
        self.assertEqual(
            _context_model_label("claude-haiku-4-5-20251001"), "haiku-4-5"
        )
        # No model known -> empty so the caller omits the label.
        self.assertEqual(_context_model_label(None), "")
        self.assertEqual(_context_model_label(""), "")
        # A non-claude id is left as-is (after date normalization).
        self.assertEqual(_context_model_label("some-model"), "some-model")


class BarGauge(unittest.TestCase):
    """The one bar helper behind both the context gauge and the account rows.

    It is the only place a percent becomes a number of lit cells, so the
    clamping and rounding are pinned here rather than at each call site.
    """

    @staticmethod
    def _lit(percent, width=10):
        """Cells in the coloured (filled) run. The bar is one solid string whose
        UNLIT tail carries the dim-grey span, so the lit run is what precedes
        it, and a bar with no grey span at all is completely full."""
        bar = _bar(percent, width, "yellow")
        return bar.spans[0].start if bar.spans else width

    def test_width_is_constant_whatever_the_percent(self):
        for percent in (-50.0, 0.0, 33.3, 99.9, 100.0, 250.0):
            self.assertEqual(cell_len(_bar(percent, 20, "yellow").plain), 20)

    def test_fill_tracks_the_percent(self):
        self.assertEqual(self._lit(0.0), 0)
        self.assertEqual(self._lit(50.0), 5)
        self.assertEqual(self._lit(100.0), 10)

    def test_overflow_clamps_to_full_never_wider(self):
        # A context estimate can exceed 100% (the panel marks it "104%?"); the
        # bar must saturate rather than render past its own width.
        self.assertEqual(cell_len(_bar(250.0, 10, "yellow").plain), 10)
        self.assertEqual(self._lit(250.0), 10)

    def test_negative_clamps_to_empty(self):
        self.assertEqual(self._lit(-5.0), 0)


class ResetText(unittest.TestCase):
    """The account rows' reset phrasing, which mirrors the Claude Usage panel."""

    def test_under_a_day_counts_down(self):
        self.assertEqual(_reset_text(NOW + 4 * 3600 + 52 * 60, NOW), "resets in 4h 52m")

    def test_minutes_are_zero_padded(self):
        self.assertEqual(_reset_text(NOW + 3 * 3600 + 5 * 60, NOW), "resets in 3h 05m")

    def test_a_day_or_more_names_the_weekday(self):
        text = _reset_text(NOW + 4 * 86400, NOW)
        self.assertTrue(text.startswith("resets "))
        self.assertNotIn("resets in", text)  # absolute, not a countdown

    def test_absent_or_past_reset_renders_nothing(self):
        # Never show a stale or negative countdown: the row just omits it.
        self.assertEqual(_reset_text(None, NOW), "")
        self.assertEqual(_reset_text(NOW - 60, NOW), "")
        self.assertEqual(_reset_text(NOW, NOW), "")


class CreditsRow(unittest.TestCase):
    """Usage credits are the ONE place real dollars belong; the percent falls
    back to used/limit when the endpoint leaves utilization null."""

    @staticmethod
    def _cells(credits):
        return [cell.plain for cell in _credits_row(credits)]

    def test_reported_utilization_wins(self):
        cells = self._cells(Credits(enabled=True, used=1.0, limit=10.0,
                                    utilization=42.0, currency="USD"))
        self.assertIn("42%", cells)
        self.assertIn("($1.00 / $10.00)", cells)

    def test_null_utilization_falls_back_to_used_over_limit(self):
        cells = self._cells(Credits(enabled=True, used=2.5, limit=10.0,
                                    utilization=None, currency="USD"))
        self.assertIn("25%", cells)

    def test_null_utilization_and_no_limit_is_zero_not_a_crash(self):
        # No limit means nothing to divide by: 0% and a bare spend figure,
        # never a ZeroDivisionError on the render path.
        cells = self._cells(Credits(enabled=True, used=3.0, limit=None,
                                    utilization=None, currency="USD"))
        self.assertIn("0%", cells)
        self.assertIn("($3.00)", cells)

    def test_non_usd_currency_is_named_not_dollar_signed(self):
        cells = self._cells(Credits(enabled=True, used=1.0, limit=5.0,
                                    utilization=20.0, currency="eur"))
        self.assertIn("(1.00 / 5.00 EUR)", cells)


class Header(unittest.TestCase):
    def test_title_active_count_and_no_refresh_tag(self):
        active = make_summary(project="proj-live", is_active=True)
        idle = make_summary(project="proj-idle", file_name="s2.jsonl")
        text = render_text([active, idle])
        self.assertIn("tokey", text)
        self.assertIn("2 active sessions", text)
        self.assertNotIn("[1.0s]", text)  # the poll interval is not user-facing

    def test_today_sums_every_session_written_since_local_midnight(self):
        midnight = time.mktime((*time.localtime(NOW)[:3], 0, 0, 0, 0, 0, -1))
        today = make_summary(file_name="a.jsonl", total_cost_usd=1.25,
                             last_write=NOW - 60)
        # Dropped from the roster (idle for hours) yet still spent today.
        earlier = make_summary(file_name="b.jsonl", total_cost_usd=2.0,
                               last_write=midnight + 1)
        yesterday = make_summary(file_name="c.jsonl", total_cost_usd=40.0,
                                 last_write=midnight - 1)
        (header,) = line_with(render_text([today, earlier, yesterday]), "tokey")
        self.assertIn("today $3.25", header)

    def test_today_carries_the_partial_marker(self):
        flagged = make_summary(total_cost_usd=1.0, unpriced=True,
                               last_write=NOW - 60)
        (header,) = line_with(render_text([flagged]), "tokey")
        self.assertIn("today $1.00+", header)

    def test_singular_active_session(self):
        active = make_summary(project="proj-only", is_active=True)
        text = render_text([active])
        self.assertIn("1 active session", text)
        self.assertNotIn("1 active sessions", text)


class SessionBlock(unittest.TestCase):
    def test_block_shows_project_state_context_and_last(self):
        active = make_summary(project="proj-live", is_active=True)
        text = render_text([active])

        (marker_line,) = line_with(text, "▶")
        self.assertIn("proj-live", marker_line)
        self.assertIn("active", marker_line)
        # Context gauge on one line: percent, a bar, and the remainder.
        self.assertIn("49%", text)
        self.assertIn("█", text)
        self.assertIn("~101.7k left", text)  # 200,000 - 98,304
        # Last line: the most recent completed turn; the cache split into
        # write (W) and read (R), a zero side left out.
        self.assertIn("Last Prompt: $0.142 · IN 12.4k · OUT 3.2k · CACHE R 8.1k", text)

    def test_marker_only_on_the_auto_followed_session(self):
        active = make_summary(project="proj-live", is_active=True,
                              last_write=NOW - 5)
        other = make_summary(project="proj-other", file_name="s2.jsonl",
                             is_active=False, last_write=NOW - 60)
        text = render_text([active, other])
        self.assertEqual(text.count("▶"), 1)
        (marker_line,) = line_with(text, "▶")
        self.assertIn("proj-live", marker_line)
        (other_line,) = line_with(text, "proj-other")
        self.assertNotIn("▶", other_line)
        # Both are live, so both carry the "active" label regardless of marker.
        self.assertIn("active", other_line)

    def test_closing_session_is_labeled_and_dim(self):
        active = make_summary(project="proj-live", is_active=True,
                              last_write=NOW - 5)
        # Age in [600, 720): liveness stamps this CLOSING.
        closing = make_summary(project="proj-closing", file_name="s2.jsonl",
                               last_write=NOW - 650)
        text = render_text([active, closing])
        (closing_line,) = line_with(text, "proj-closing")
        self.assertIn("closing", closing_line)

    def test_cache_omitted_when_last_turn_read_no_cache(self):
        active = make_summary(project="proj-live", is_active=True,
                              last_cache_read_tokens=0)
        text = render_text([active])
        self.assertIn("Last Prompt: $0.142 · IN 12.4k · OUT 3.2k", text)
        self.assertNotIn("CACHE", text)

    def test_unknown_context_limit_is_honest(self):
        active = make_summary(project="proj-live", is_active=True,
                              context_used=98_304, context_limit=None,
                              context_percent=None)
        text = render_text([active])
        self.assertIn("context limit unknown", text)
        self.assertNotIn("█", text)  # no bar invented without a limit

    def test_model_label_shares_context_row_aligned_under_active(self):
        active = make_summary(project="proj-live", is_active=True,
                              context_model="claude-opus-4-8")
        text = render_text([active])
        (marker_line,) = line_with(text, "▶")
        (ctx_line,) = line_with(text, "~101.7k left")  # the context gauge row
        # Same row as the gauge, not a line of its own.
        self.assertIn("opus-4-8", ctx_line)
        # Right edge lines up under the header's "active" label.
        self.assertEqual(
            marker_line.rindex("active") + len("active"),
            ctx_line.rindex("opus-4-8") + len("opus-4-8"),
        )

    def test_model_label_drops_date_suffix(self):
        active = make_summary(project="proj-live", is_active=True,
                              context_model="claude-haiku-4-5-20251001")
        text = render_text([active])
        (ctx_line,) = line_with(text, "~101.7k left")
        self.assertIn("haiku-4-5", ctx_line)
        self.assertNotIn("20251001", ctx_line)

    def test_model_label_absent_when_no_model(self):
        active = make_summary(project="proj-live", is_active=True,
                              context_model=None)
        text = render_text([active])
        (ctx_line,) = line_with(text, "~101.7k left")
        # No model known -> bare gauge, no trailing label leaked.
        self.assertNotIn("opus", ctx_line)

    def test_overflow_percent_marker_and_zero_left(self):
        active = make_summary(project="proj-live", is_active=True,
                              context_used=208_000, context_limit=200_000,
                              context_percent=104.0)
        text = render_text([active])
        self.assertIn("104%?", text)
        self.assertIn("~0 left", text)

    def test_unpriceable_last_turn_shows_question_mark(self):
        active = make_summary(project="proj-live", is_active=True,
                              last_cost_usd=None)
        text = render_text([active])
        self.assertIn("Last Prompt: $? · IN 12.4k", text)

    def test_no_completed_turn_is_honest(self):
        active = make_summary(project="proj-live", is_active=True,
                              last_cost_usd=None, last_input_tokens=None,
                              last_output_tokens=None,
                              last_cache_read_tokens=None)
        text = render_text([active])
        # The LAST line is honest: it says so and fabricates no IN/OUT figures.
        # (The Sum line below it still renders the session totals; see
        # test_sum_line_shows_session_totals.)
        (last_line,) = line_with(text, "Last Prompt:")
        self.assertIn("no completed turn yet", last_line)
        self.assertNotIn("IN", last_line)
        self.assertNotIn("OUT", last_line)

    def test_sum_line_shows_session_totals(self):
        active = make_summary(project="proj-live", is_active=True,
                              total_cost_usd=1.2345, sum_input_tokens=120_000,
                              sum_output_tokens=15_000,
                              sum_cache_write_tokens=40_000,
                              sum_cache_read_tokens=2_500_000)
        text = render_text([active])
        (sum_line,) = line_with(text, "Total:")
        self.assertIn(
            "Total: $1.234 · IN 120.0k · OUT 15.0k · CACHE W 40.0k R 2.5M", sum_line
        )

    def test_cache_write_shown_even_without_reads(self):
        active = make_summary(project="proj-live", is_active=True,
                              last_cache_write_tokens=5_000,
                              last_cache_read_tokens=0)
        (last_line,) = line_with(render_text([active]), "Last Prompt:")
        self.assertIn("CACHE W 5.0k", last_line)
        self.assertNotIn(" R ", last_line)

    def test_sum_cache_omitted_when_zero(self):
        active = make_summary(project="proj-live", is_active=True,
                              total_cost_usd=0.5, sum_input_tokens=10_000,
                              sum_output_tokens=2_000, sum_cache_read_tokens=0)
        text = render_text([active])
        (sum_line,) = line_with(text, "Total:")
        self.assertIn("Total: $0.500 · IN 10.0k · OUT 2.0k", sum_line)
        self.assertNotIn("CACHE", sum_line)

    def test_sum_line_flags_partial_total_when_unpriced(self):
        active = make_summary(project="proj-live", is_active=True,
                              total_cost_usd=1.2345, unpriced=True,
                              sum_input_tokens=10_000, sum_output_tokens=2_000)
        text = render_text([active])
        (sum_line,) = line_with(text, "Total:")
        self.assertIn("Total: $1.234+", sum_line)  # + flags the partial total


class FooterAndCaps(unittest.TestCase):
    def test_footer_active_only_total(self):
        active = make_summary(project="proj-live", is_active=True,
                              total_cost_usd=1.25, total_tokens=300_000)
        idle = make_summary(project="proj-idle", total_cost_usd=0.5,
                            total_tokens=50_000)
        text = render_text([active, idle])
        # Active-only total; both are active (240s old), so the active total is
        # the two-session sum: $1.75, 350k tok. No session count in the footer.
        self.assertIn("active: $1.750 · 350.0k tok", text)
        self.assertNotIn("2 sessions", text)
        self.assertNotIn("(+ unpriced)", text)

    def test_footer_unpriced_marker(self):
        active = make_summary(project="proj-live", is_active=True)
        odd = make_summary(project="proj-odd", unpriced=True)
        text = render_text([active, odd])
        self.assertIn("(+ unpriced)", text)

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
        text = render_text(summaries)

        self.assertEqual(ROSTER_LIMIT, 10)
        self.assertIn("proj-09", text)
        self.assertNotIn("proj-10", text)  # beyond the cap: hidden blocks
        self.assertIn("+3 more", text)
        # Footer total is ACTIVE-ONLY; all 13 are active, and the blocks hidden
        # beyond the cap are still summed in: 13*0.1 = $1.300, 13*10k = 130.0k.
        self.assertIn("active: $1.300 · 130.0k tok", text)

    def test_dropped_session_excluded_from_roster_and_footer(self):
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
        summaries = [*fresh, dropped]

        # Roster scope: the dropped session leaves; 11 remain, 10 shown.
        view = build_roster_view(summaries, now=NOW)
        self.assertEqual(len(view.sessions), 11)

        text = render_text(summaries)
        self.assertIn("+1 more", text)           # 11 roster blocks, 10 shown
        self.assertNotIn("proj-dropped", text)   # dropped block is gone
        # Footer is ACTIVE-ONLY: 11*0.1 = $1.100, 11*10k = 110.0k; the dropped
        # session's $0.50 / 50k are NOT summed in.
        (footer,) = line_with(text, "active: $")
        self.assertIn("active: $1.100 · 110.0k tok", footer)
        self.assertNotIn("$1.6", footer)  # would be the all-discovered total

    def test_empty_roster(self):
        text = render_text([])
        self.assertIn("no sessions in the last 7 days", text)
        self.assertIn("active: $0.000 · 0 tok", text)  # active-only, no count

    def test_no_keybind_hints(self):
        active = make_summary(project="proj-live", is_active=True)
        text = render_text([active]).lower()
        for hint in ("press", "quit", "[q]", "keys:"):
            self.assertNotIn(hint, text)


class AutoFollow(unittest.TestCase):
    """The ▶ marker follows recency through the real cache, matching the live
    path's auto-follow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = self.tmp.name
        self.now = time.time()
        # Isolate the marker store the same way SessionsBase does. Without this
        # the cache reads the real ~/.claude store, and any session open on the
        # developer's machine is synthesized into the pass and steals the ▶.
        markers_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(markers_tmp.cleanup)
        patcher = mock.patch.object(
            markers_mod, "DEFAULT_MARKERS_DIR", markers_tmp.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
        panel = render_roster(summaries, width=100, now=self.now)
        console = Console(width=100, file=io.StringIO(), force_terminal=False)
        console.print(panel)
        return console.file.getvalue()

    def test_marker_moves_when_another_session_becomes_newest(self):
        older = self.write_transcript("proj-a", "a.jsonl", age_seconds=200)
        self.write_transcript("proj-b", "b.jsonl", age_seconds=10)
        cache = SessionCache(self.projects)

        first = self.render(cache)
        (marker_line,) = line_with(first, "▶")
        self.assertIn("proj-b", marker_line)  # newest is the auto-followed one

        # proj-a becomes the most recently modified transcript.
        os.utime(older, (self.now - 1, self.now - 1))
        second = self.render(cache)
        (marker_line,) = line_with(second, "▶")
        self.assertIn("proj-a", marker_line)  # marker followed recency
        # proj-b is no longer the primary: its header line has lost the marker.
        (proj_b_line,) = line_with(second, "proj-b")
        self.assertNotIn("▶", proj_b_line)


class ProjectTitle(unittest.TestCase):
    """The session title prefers the real cwd as a ~-relative path."""

    def setUp(self):
        self.home = os.path.expanduser("~")

    def test_home_relative_path(self):
        summary = make_summary(cwd=os.path.join(self.home, "cc tracker"))
        self.assertEqual(_project_title(summary), "~/cc tracker")

    def test_home_itself_is_tilde(self):
        self.assertEqual(_project_title(make_summary(cwd=self.home)), "~")

    def test_path_outside_home_kept_verbatim(self):
        summary = make_summary(cwd="/srv/app")
        self.assertEqual(_project_title(summary), "/srv/app")

    def test_falls_back_to_project_when_no_cwd(self):
        summary = make_summary(cwd=None, project="proj-x")
        self.assertEqual(_project_title(summary), "proj-x")

    def test_render_shows_tilde_title_not_dash_encoded(self):
        summary = make_summary(cwd=os.path.join(self.home, "cc tracker"))
        text = render_text([summary])
        (line,) = line_with(text, "~/cc tracker")
        self.assertIn("~/cc tracker", line)


class AccountUsageRequested(unittest.TestCase):
    """The launch-time switch for the opt-in account-usage block."""

    def test_cc_subcommand_enables(self):
        self.assertTrue(account_usage_requested(["cc"], env={}))

    def test_default_is_off(self):
        self.assertFalse(account_usage_requested([], env={}))

    def test_env_var_still_enables(self):
        self.assertTrue(account_usage_requested([], env={USAGE_ENV_VAR: "1"}))

    def test_unrelated_args_stay_off(self):
        self.assertFalse(account_usage_requested(["--foo", "bar"], env={}))


class AccountUsageBlock(unittest.TestCase):
    """The opt-in account-usage block and plan badge layered onto the roster."""

    def usage(self, **overrides):
        fields = dict(
            plan="pro",
            session=UsageWindow(utilization=4.0, resets_at=NOW + 3 * 3600 + 720),
            weekly=UsageWindow(utilization=24.0, resets_at=NOW + 4 * 86400),
            weekly_opus=None,
            weekly_sonnet=None,
            credits=None,
        )
        fields.update(overrides)
        return AccountUsage(**fields)

    def test_block_and_badge_render_with_usage(self):
        text = render_text([make_summary()], usage=self.usage())
        self.assertIn("Account-level Claude usage", text)
        self.assertIn("Pro Plan", text)  # header badge
        (session_line,) = line_with(text, "Session limit")
        self.assertIn("4%", session_line)
        self.assertIn("resets in 3h 12m", session_line)  # under a day: countdown
        (weekly_line,) = line_with(text, "Weekly limit")
        self.assertIn("24%", weekly_line)
        self.assertIn("resets", weekly_line)  # over a day: absolute time

    def test_no_dollars_on_subscription_rows(self):
        # The subscription windows are percentages only; no $ figure is invented.
        text = render_text([make_summary()], usage=self.usage())
        self.assertNotIn("$", line_with(text, "Session limit")[0])
        self.assertNotIn("$", line_with(text, "Weekly limit")[0])

    def test_default_roster_has_no_block_or_badge(self):
        text = render_text([make_summary()])
        self.assertNotIn("Account-level Claude usage", text)
        self.assertNotIn("Plan", text)

    def test_empty_usage_omits_block(self):
        text = render_text(
            [make_summary()], usage=self.usage(session=None, weekly=None)
        )
        self.assertNotIn("Account-level Claude usage", text)
        # A plan with no windows still shows the badge (we have a reading).
        self.assertIn("Pro Plan", text)

    def test_enabled_credits_row_shows_dollars(self):
        credits = Credits(
            enabled=True, used=1.2, limit=10.0, utilization=12.0, currency="USD"
        )
        text = render_text([make_summary()], usage=self.usage(credits=credits))
        (line,) = line_with(text, "Usage credits")
        self.assertIn("12%", line)
        self.assertIn("$1.20 / $10.00", line)  # dollars belong here only

    def test_disabled_credits_row_absent(self):
        credits = Credits(
            enabled=False, used=None, limit=None, utilization=None, currency=None
        )
        text = render_text([make_summary()], usage=self.usage(credits=credits))
        self.assertEqual(line_with(text, "Usage credits"), [])

    def test_status_line_when_enabled_but_no_data(self):
        # No usage reading yet, but the feature is on: show the status, not a gap.
        text = render_text(
            [make_summary()], usage=None, usage_status="Account-level usage: loading..."
        )
        self.assertIn("Account-level usage: loading...", text)
        self.assertNotIn("Session limit", text)

    def test_status_line_ignored_once_block_renders(self):
        # A reading exists: the block shows and the status line is not drawn.
        text = render_text(
            [make_summary()],
            usage=self.usage(),
            usage_status="should not appear",
        )
        self.assertIn("Session limit", text)
        self.assertNotIn("should not appear", text)

    def test_per_model_weekly_rows_when_present(self):
        text = render_text(
            [make_summary()],
            usage=self.usage(
                weekly_opus=UsageWindow(utilization=40.0, resets_at=None)
            ),
        )
        (line,) = line_with(text, "Weekly (Opus)")
        self.assertIn("40%", line)


class RunLoop(unittest.TestCase):
    """The poll loop: a bad tick is survivable, Ctrl-C is not a crash.

    The real loop is infinite, so each test drives it through a stubbed
    SessionCache.summaries that raises to end the run. stdout is swallowed so
    the Live control codes stay out of the test output. The startup backfill is
    stubbed: left live it would record the real ~/.claude transcripts into the
    real history database.
    """

    @staticmethod
    def _run(side_effect, **kwargs):
        with mock.patch.object(
            roster.SessionCache, "summaries", side_effect=side_effect
        ), mock.patch.object(roster, "_start_backfill"), contextlib.redirect_stdout(
            io.StringIO()
        ):
            return run(interval=0, **kwargs)

    def test_startup_backfill_runs_once_not_per_tick(self):
        ticks = [RuntimeError("bad tick"), KeyboardInterrupt]
        with mock.patch.object(
            roster.SessionCache, "summaries", side_effect=ticks
        ), mock.patch.object(roster, "_start_backfill") as start, (
            contextlib.redirect_stdout(io.StringIO())
        ), self.assertLogs("cc_token_tracker.roster", level="ERROR"):
            run(interval=0)
        start.assert_called_once_with()

    def test_keyboard_interrupt_exits_zero(self):
        self.assertEqual(self._run(KeyboardInterrupt), 0)

    def test_one_failing_tick_is_logged_and_the_loop_continues(self):
        # The load-bearing promise: a single bad read (a transcript truncated
        # mid-write, a transient stat failure) must not end a long-running
        # panel. The second tick still happens; only Ctrl-C stops it.
        ticks = [RuntimeError("bad tick"), KeyboardInterrupt]
        with self.assertLogs("cc_token_tracker.roster", level="ERROR") as logged:
            self.assertEqual(self._run(ticks), 0)
        self.assertIn("roster tick failed", logged.output[0])

    def test_account_usage_off_never_touches_credentials_or_network(self):
        # The opt-in promise: with the feature off, no refresh thread is
        # started, so neither the credential store nor the endpoint is read.
        with mock.patch.object(roster.UsageProvider, "refresh") as refresh:
            self._run(KeyboardInterrupt, account_usage=False)
        refresh.assert_not_called()


class VersionFlag(unittest.TestCase):
    """``--version``/``-V`` prints the version and exits before the render loop."""

    def test_long_and_short_flags_request_version(self):
        self.assertTrue(version_requested(["--version"]))
        self.assertTrue(version_requested(["-V"]))

    def test_other_args_do_not(self):
        self.assertFalse(version_requested([]))
        self.assertFalse(version_requested(["cc", "--no-mood"]))

    def test_main_prints_version_and_exits_zero(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["--version"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), f"tokey {__version__}")


class ScaledTokens(unittest.TestCase):
    """One formatter for blocks, footer, and log: the unit scales with the
    number instead of rendering 13.9M as ``13890.0k``."""

    def test_millions_render_as_m(self):
        self.assertEqual(roster._tokens(13_890_000), "13.9M")

    def test_thousands_render_as_k(self):
        self.assertEqual(roster._tokens(482_100), "482.1k")

    def test_small_counts_render_bare(self):
        self.assertEqual(roster._tokens(900), "900")
        self.assertEqual(roster._tokens(0), "0")

    def test_boundaries_pick_the_larger_unit(self):
        self.assertEqual(roster._tokens(1_000), "1.0k")
        self.assertEqual(roster._tokens(1_000_000), "1.0M")


class MoneyMarker(unittest.TestCase):
    def test_priced_total_is_plain(self):
        self.assertEqual(roster._money(12.5, False), "$12.50")

    def test_unpriced_total_keeps_the_partial_marker(self):
        # The roster's contract: a flagged total may never look complete.
        self.assertEqual(roster._money(12.5, True), "$12.50+")

    def test_large_totals_group_thousands(self):
        self.assertEqual(roster._money(1234.5, False), "$1,234.50")


class LogSubcommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "history.db")

    def test_log_is_requested_by_the_bare_subcommand(self):
        self.assertTrue(roster.log_requested(["log"]))
        self.assertFalse(roster.log_requested([]))
        self.assertFalse(roster.log_requested(["cc"]))

    def _render(self, db):
        console = Console(width=70, file=io.StringIO())
        console.print(roster.render_log(db_path=db))
        return console.file.getvalue()

    def test_empty_history_says_so_rather_than_showing_zeros(self):
        self.assertIn("no history yet", self._render(self.db))

    def test_recorded_sessions_appear_with_their_totals(self):
        from cc_token_tracker.history import record_session

        record_session(_history_summary(cost=3.5), db_path=self.db)
        out = self._render(self.db)
        self.assertIn("$3.50", out)
        self.assertIn("tokey log", out)

    def test_an_unpriced_session_marks_the_rendered_total(self):
        from cc_token_tracker.history import record_session

        record_session(_history_summary(cost=3.5, unpriced=True), db_path=self.db)
        self.assertIn("$3.50+", self._render(self.db))

    def test_months_bars_and_an_all_time_footer(self):
        from cc_token_tracker.history import record_session

        record_session(_history_summary(cost=3.5), db_path=self.db)
        record_session(
            _history_summary(cost=7.0, file_name="sid-2.jsonl",
                             last_write=1_780_000_000.0 - 86400),
            db_path=self.db,
        )
        out = self._render(self.db)
        self.assertIn("by month", out)
        self.assertIn("last 2 active days", out)
        self.assertIn("all time  ·  2 sessions  ·  $10.50", out)
        # Bars scale to the priciest day: the $7 day fills the full width.
        self.assertIn("█" * roster._LOG_BAR_WIDTH, out)
        self.assertNotIn("day(s)", out)

    def test_a_single_day_and_session_read_singular(self):
        from cc_token_tracker.history import record_session

        record_session(_history_summary(cost=1.0), db_path=self.db)
        out = self._render(self.db)
        self.assertIn("last 1 active day", out)
        self.assertIn("1 session  ·", out)


class FitToHeight(unittest.TestCase):
    """The mood face gives way when the panel would not fit the terminal."""

    @staticmethod
    def _fit(height, *, mood=True):
        console = Console(width=100, height=height, file=io.StringIO())
        build = functools.partial(
            render_roster, [make_summary(is_active=True)], width=100, now=NOW
        )
        panel = roster.fit_to_height(console, 100, build, mood=mood)
        console.print(panel)
        return console.file.getvalue()

    def test_tall_terminal_keeps_the_face(self):
        # With room to spare the face stays: the panel is taller than without it.
        self.assertGreater(
            len(self._fit(60).splitlines()), len(self._fit(60, mood=False).splitlines())
        )

    def test_short_terminal_drops_the_face_but_keeps_the_sessions(self):
        tall, short = self._fit(60), self._fit(14)
        self.assertLess(len(short.splitlines()), len(tall.splitlines()))
        self.assertLessEqual(len(short.splitlines()), 14)
        self.assertIn("Last Prompt:", short)
        self.assertIn("active: $", short)

    def test_no_mood_is_never_overridden(self):
        self.assertEqual(self._fit(60, mood=False), self._fit(14, mood=False))


def _history_summary(cost=1.0, unpriced=False, file_name="sid-1.jsonl",
                     last_write=1_780_000_000.0):
    """A SessionSummary shaped as summarize_session yields one, for history."""
    from cc_token_tracker.sessions import SessionSummary

    return SessionSummary(
        project="proj", file_name=file_name, total_tokens=2_500_000,
        total_cost_usd=cost, unpriced=unpriced, context_used=None,
        context_limit=None, context_percent=None, context_model=None,
        last_write=last_write, is_active=False, cwd="/home/u/proj",
        sum_input_tokens=2_000_000, sum_output_tokens=400_000,
        sum_cache_read_tokens=100_000,
    )


if __name__ == "__main__":
    unittest.main()
