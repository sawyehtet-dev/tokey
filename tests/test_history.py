"""Tests for the durable spend history store.

Every test injects a temp ``db_path``; nothing here touches the real
``~/.claude/tokey/history.db``.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from cc_token_tracker.history import (
    backfill,
    backfill_on_disk,
    daily_totals,
    default_db_path,
    project_totals,
    record_session,
    session_id_of,
)
from cc_token_tracker.sessions import SessionSummary


def summary(
    session_id="a1b2",
    project="proj",
    ended_at=1_780_000_000.0,
    total_tokens=1000,
    cost=1.25,
    unpriced=False,
    cwd="/home/u/proj",
):
    """A SessionSummary shaped exactly as summarize_session yields one."""
    return SessionSummary(
        project=project,
        file_name=f"{session_id}.jsonl",
        total_tokens=total_tokens,
        total_cost_usd=cost,
        unpriced=unpriced,
        context_used=None,
        context_limit=None,
        context_percent=None,
        context_model=None,
        last_write=ended_at,
        is_active=False,
        cwd=cwd,
        sum_input_tokens=600,
        sum_output_tokens=300,
        sum_cache_read_tokens=100,
    )


class TempDB(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        # A nested path so the parent-creation branch is exercised every test.
        self.db = os.path.join(self._dir.name, "nested", "history.db")
        self.addCleanup(self._dir.cleanup)


def _named(file_name):
    """A summary with an odd transcript name (SessionSummary is frozen)."""
    return SessionSummary(**{**summary().__dict__, "file_name": file_name})


class SessionIdDerivation(unittest.TestCase):
    def test_strips_jsonl_suffix(self):
        self.assertEqual(session_id_of(summary(session_id="uuid-1")), "uuid-1")

    def test_non_jsonl_name_yields_none(self):
        self.assertIsNone(session_id_of(_named("notatranscript.txt")))

    def test_bare_suffix_yields_none(self):
        self.assertIsNone(session_id_of(_named(".jsonl")))


class Recording(TempDB):
    def test_records_a_row(self):
        self.assertTrue(record_session(summary(), db_path=self.db))
        days = daily_totals(db_path=self.db)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0].sessions, 1)
        self.assertAlmostEqual(days[0].cost_usd, 1.25)

    def test_figures_are_stored_verbatim_from_the_summary(self):
        record_session(
            summary(total_tokens=4242, cost=9.5), db_path=self.db
        )
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                "SELECT total_tokens, input_tokens, output_tokens,"
                " cache_read_tokens, cost_usd FROM sessions"
            ).fetchone()
        self.assertEqual(row[:4], (4242, 600, 300, 100))
        self.assertAlmostEqual(row[4], 9.5)

    def test_same_session_upserts_rather_than_duplicating(self):
        record_session(summary(cost=1.0), db_path=self.db)
        record_session(summary(cost=7.0), db_path=self.db)
        days = daily_totals(db_path=self.db)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0].sessions, 1)
        # The later write wins: a mid-flight row self-corrects.
        self.assertAlmostEqual(days[0].cost_usd, 7.0)

    def test_distinct_sessions_accumulate(self):
        record_session(summary(session_id="s1", cost=1.0), db_path=self.db)
        record_session(summary(session_id="s2", cost=2.0), db_path=self.db)
        days = daily_totals(db_path=self.db)
        self.assertEqual(days[0].sessions, 2)
        self.assertAlmostEqual(days[0].cost_usd, 3.0)

    def test_summary_without_session_id_is_skipped(self):
        self.assertFalse(record_session(_named("bad.txt"), db_path=self.db))


class UnpricedPropagation(TempDB):
    def test_unpriced_session_flags_the_day(self):
        record_session(summary(session_id="s1", unpriced=True), db_path=self.db)
        record_session(summary(session_id="s2", unpriced=False), db_path=self.db)
        self.assertTrue(daily_totals(db_path=self.db)[0].unpriced)

    def test_all_priced_day_is_not_flagged(self):
        record_session(summary(session_id="s1"), db_path=self.db)
        self.assertFalse(daily_totals(db_path=self.db)[0].unpriced)


class DailyBucketing(TempDB):
    def test_separate_days_are_separate_rows_newest_first(self):
        day_seconds = 86400
        record_session(
            summary(session_id="old", ended_at=1_780_000_000.0), db_path=self.db
        )
        record_session(
            summary(session_id="new", ended_at=1_780_000_000.0 + 2 * day_seconds),
            db_path=self.db,
        )
        days = daily_totals(db_path=self.db)
        self.assertEqual(len(days), 2)
        self.assertGreater(days[0].day, days[1].day)

    def test_limit_caps_the_rows(self):
        for i in range(5):
            record_session(
                summary(session_id=f"s{i}", ended_at=1_780_000_000.0 + i * 86400),
                db_path=self.db,
            )
        self.assertEqual(len(daily_totals(limit=3, db_path=self.db)), 3)


class ProjectRollup(TempDB):
    def test_orders_by_cost_descending(self):
        record_session(
            summary(session_id="a", project="cheap", cost=1.0), db_path=self.db
        )
        record_session(
            summary(session_id="b", project="pricey", cost=50.0), db_path=self.db
        )
        totals = project_totals(db_path=self.db)
        self.assertEqual([t.project for t in totals], ["pricey", "cheap"])

    def test_sessions_in_one_project_combine(self):
        record_session(
            summary(session_id="a", project="p", cost=1.0), db_path=self.db
        )
        record_session(
            summary(session_id="b", project="p", cost=2.0), db_path=self.db
        )
        totals = project_totals(db_path=self.db)
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0].sessions, 2)
        self.assertAlmostEqual(totals[0].cost_usd, 3.0)


class Backfill(TempDB):
    def test_records_every_summary_and_counts_them(self):
        written = backfill(
            [summary(session_id="s1"), summary(session_id="s2")], db_path=self.db
        )
        self.assertEqual(written, 2)

    def test_is_idempotent_across_runs(self):
        items = [summary(session_id="s1"), summary(session_id="s2")]
        backfill(items, db_path=self.db)
        backfill(items, db_path=self.db)
        self.assertEqual(daily_totals(db_path=self.db)[0].sessions, 2)

    def test_unrecordable_summaries_do_not_stop_the_rest(self):
        written = backfill(
            [_named("bad.txt"), summary(session_id="good")], db_path=self.db
        )
        self.assertEqual(written, 1)


class BackfillOnDisk(TempDB):
    """Every transcript on disk is recorded, however old: no 7-day window."""

    def _transcript(self, projects, name, age_days):
        project = os.path.join(projects, "-home-u-proj")
        os.makedirs(project, exist_ok=True)
        path = os.path.join(project, name)
        with open(path, "w") as fh:
            fh.write('{"type":"user","message":{"role":"user","content":"hi"}}\n')
            fh.write(
                '{"type":"assistant","message":{"id":"m1","role":"assistant",'
                '"model":"claude-opus-5","usage":{"input_tokens":1000000,'
                '"output_tokens":0}}}\n'
            )
        old = os.path.getmtime(path) - age_days * 86400
        os.utime(path, (old, old))

    def test_records_transcripts_older_than_the_roster_window(self):
        with tempfile.TemporaryDirectory() as projects:
            self._transcript(projects, "fresh.jsonl", age_days=0)
            self._transcript(projects, "old.jsonl", age_days=25)
            written = backfill_on_disk(projects, db_path=self.db)
        self.assertEqual(written, 2)
        totals = project_totals(db_path=self.db)
        self.assertEqual(totals[0].sessions, 2)
        self.assertAlmostEqual(totals[0].cost_usd, 10.0)  # 2 x 1M opus-5 input

    def test_missing_projects_dir_writes_nothing(self):
        missing = os.path.join(self._dir.name, "nope")
        self.assertEqual(backfill_on_disk(missing, db_path=self.db), 0)


class NeverRaises(unittest.TestCase):
    """History is a side effect of watching; it may never take tokey down."""

    def test_unwritable_path_returns_false(self):
        # A path whose parent is a FILE: makedirs fails, so the whole write does.
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "blocker")
            with open(blocker, "w") as handle:
                handle.write("")
            db = os.path.join(blocker, "nested", "history.db")
            self.assertFalse(record_session(summary(), db_path=db))

    def test_queries_on_an_unreadable_db_read_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "blocker")
            with open(blocker, "w") as handle:
                handle.write("")
            db = os.path.join(blocker, "nested", "history.db")
            self.assertEqual(daily_totals(db_path=db), [])
            self.assertEqual(project_totals(db_path=db), [])

    def test_corrupt_database_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "history.db")
            with open(db, "w") as handle:
                handle.write("this is not a sqlite database")
            self.assertEqual(daily_totals(db_path=db), [])
            self.assertFalse(record_session(summary(), db_path=db))


class DefaultLocation(unittest.TestCase):
    def test_lives_under_the_claude_tokey_directory(self):
        path = default_db_path()
        self.assertTrue(path.endswith(os.path.join(".claude", "tokey", "history.db")))
        self.assertNotIn("~", path)


if __name__ == "__main__":
    unittest.main()


class ProjectLabels(TempDB):
    """The raw project key is the grouping identity; the label is what a human
    reads. The roster already prefers cwd for the same reason."""

    def test_label_is_the_cwd_base_name(self):
        record_session(
            summary(project="-home-u-Mood-Palette", cwd="/home/u/Mood Palette"),
            db_path=self.db,
        )
        total = project_totals(db_path=self.db)[0]
        self.assertEqual(total.label, "Mood Palette")
        self.assertEqual(total.project, "-home-u-Mood-Palette")

    def test_label_falls_back_to_the_project_key_without_a_cwd(self):
        record_session(summary(project="-home-u-thing", cwd=None), db_path=self.db)
        self.assertEqual(project_totals(db_path=self.db)[0].label, "-home-u-thing")

    def test_trailing_slash_does_not_blank_the_label(self):
        record_session(summary(cwd="/home/u/proj/"), db_path=self.db)
        self.assertEqual(project_totals(db_path=self.db)[0].label, "proj")
