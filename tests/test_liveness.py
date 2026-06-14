"""Tests for cc_token_tracker.liveness and the roster's liveness scope.

The classifier boundaries are exact and authoritative, so they are pinned at
the second on each side. The roster-scope tests drive build_roster_view (the
seam render_roster uses) with a fixed now, so membership and the active count
are deterministic without rendering.
"""

import unittest

from cc_token_tracker.liveness import (
    ACTIVE,
    CLOSING,
    DROPPED,
    classify_liveness,
    classify_with_marker,
)
from cc_token_tracker.markers import (
    CLOSED,
    MARKER_STALE_AFTER_SECONDS,
    OPEN,
)
from cc_token_tracker.roster import build_roster_view
from cc_token_tracker.sessions import SessionSummary

NOW = 1_780_000_000.0


def make_summary(**overrides):
    fields = dict(
        project="proj",
        file_name="s.jsonl",
        total_tokens=1_000,
        total_cost_usd=0.1,
        unpriced=False,
        context_used=1_000,
        context_limit=200_000,
        context_percent=0.5,
        last_write=NOW,
        is_active=False,
    )
    fields.update(overrides)
    return SessionSummary(**fields)


class ClassifyLiveness(unittest.TestCase):
    def test_exact_boundaries(self):
        # age = now - last_write; boundaries are exact and half-open on the low
        # side: 599 -> active, 600 -> closing, 719 -> closing, 720 -> dropped.
        self.assertEqual(classify_liveness(NOW, NOW - 599), ACTIVE)
        self.assertEqual(classify_liveness(NOW, NOW - 600), CLOSING)
        self.assertEqual(classify_liveness(NOW, NOW - 719), CLOSING)
        self.assertEqual(classify_liveness(NOW, NOW - 720), DROPPED)

    def test_label_strings(self):
        self.assertEqual((ACTIVE, CLOSING, DROPPED),
                         ("active", "closing", "dropped"))

    def test_future_dated_reads_active(self):
        # Clock skew: a file newer than now is younger than every boundary.
        self.assertEqual(classify_liveness(NOW, NOW + 5), ACTIVE)


class ClassifyWithMarker(unittest.TestCase):
    def test_closed_marker_drops_even_with_fresh_transcript(self):
        # The fix for the 10-minute lingering: an exited session (SessionEnd)
        # drops at once even though its transcript was just written.
        self.assertEqual(
            classify_with_marker(NOW, NOW - 1, CLOSED, NOW - 1), DROPPED
        )

    def test_open_marker_is_active_even_when_transcript_is_stale(self):
        # An idle-but-open session: transcript untouched for an hour, but the
        # open marker keeps it active (mtime alone would call it dropped).
        self.assertEqual(
            classify_with_marker(NOW, NOW - 3_600, OPEN, NOW - 3_600), ACTIVE
        )

    def test_open_marker_past_crash_bound_drops(self):
        stale = MARKER_STALE_AFTER_SECONDS + 1
        self.assertEqual(
            classify_with_marker(NOW, NOW - stale, OPEN, NOW - stale), DROPPED
        )
        # Last activity is the LATER of marker ts and transcript mtime: a fresh
        # transcript keeps an old-started session active.
        self.assertEqual(
            classify_with_marker(NOW, NOW - 1, OPEN, NOW - stale), ACTIVE
        )

    def test_no_marker_falls_back_to_mtime(self):
        self.assertEqual(classify_with_marker(NOW, NOW - 100, None, None), ACTIVE)
        self.assertEqual(classify_with_marker(NOW, NOW - 650, None, None), CLOSING)
        self.assertEqual(classify_with_marker(NOW, NOW - 800, None, None), DROPPED)


class RosterScope(unittest.TestCase):
    def test_closed_marker_session_excluded_open_marker_kept(self):
        # Render scope honours markers: a closed session leaves the roster; an
        # open one stays active despite an old transcript mtime.
        closed = make_summary(file_name="x.jsonl", last_write=NOW - 1,
                              marker_event=CLOSED, marker_ts=NOW - 1)
        open_idle = make_summary(file_name="o.jsonl", last_write=NOW - 5_000,
                                 marker_event=OPEN, marker_ts=NOW - 5_000)
        view = build_roster_view([closed, open_idle], now=NOW)
        self.assertEqual([s.file_name for s in view.sessions], ["o.jsonl"])
        self.assertEqual(view.active_count, 1)


    def test_one_active_one_closing_count_one_len_two(self):
        active = make_summary(file_name="a.jsonl", last_write=NOW - 100)
        closing = make_summary(file_name="c.jsonl", last_write=NOW - 650)

        view = build_roster_view([active, closing], now=NOW)

        self.assertEqual(view.active_count, 1)
        self.assertEqual(len(view.sessions), 2)
        states = {s.file_name: s.state for s in view.sessions}
        self.assertEqual(states["a.jsonl"], ACTIVE)
        self.assertEqual(states["c.jsonl"], CLOSING)

    def test_dropped_session_absent_from_roster(self):
        active = make_summary(file_name="a.jsonl", last_write=NOW - 100)
        dropped = make_summary(file_name="d.jsonl", last_write=NOW - 5_000)

        view = build_roster_view([active, dropped], now=NOW)

        self.assertEqual([s.file_name for s in view.sessions], ["a.jsonl"])
        self.assertEqual(view.active_count, 1)

    def test_order_preserved_and_costs_untouched(self):
        first = make_summary(file_name="first.jsonl", last_write=NOW - 10,
                             total_cost_usd=1.23, total_tokens=42)
        second = make_summary(file_name="second.jsonl", last_write=NOW - 650)

        view = build_roster_view([first, second], now=NOW)

        self.assertEqual([s.file_name for s in view.sessions],
                         ["first.jsonl", "second.jsonl"])
        # The frozen cost/token figures pass through verbatim.
        self.assertEqual(view.sessions[0].total_cost_usd, 1.23)
        self.assertEqual(view.sessions[0].total_tokens, 42)


if __name__ == "__main__":
    unittest.main()
