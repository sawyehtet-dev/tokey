"""Tests for cc_token_tracker.usage: the opt-in account-level usage feature.

These pin the never-raise IO boundary (env gate, creds read, network fetch) and
the pure parse, all with injected fakes so nothing touches the real credentials
file or the network. The fetch is exercised through an injected ``opener`` and
the provider through an injected creds-reader and fetcher.
"""

import json
import os
import tempfile
import unittest
import urllib.error

from cc_token_tracker.usage import (
    USAGE_ENV_VAR,
    AccountUsage,
    Credentials,
    UsageProvider,
    UsageWindow,
    fetch_usage_blob,
    parse_usage,
    read_credentials,
    usage_enabled,
)

# A real-shaped /api/oauth/usage body (Pro plan): only the two windows populate,
# the per-model and add-on fields are null/disabled.
LIVE_BLOB = {
    "five_hour": {"utilization": 4.0, "resets_at": "2026-06-14T18:30:00.84+00:00"},
    "seven_day": {"utilization": 24.0, "resets_at": "2026-06-19T06:00:00.84+00:00"},
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "extra_usage": {
        "is_enabled": False,
        "monthly_limit": None,
        "used_credits": None,
        "utilization": None,
        "currency": None,
        "disabled_reason": None,
    },
}


class _FakeResponse:
    """Minimal stand-in for an urlopen response/context manager."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class UsageEnabled(unittest.TestCase):
    def test_truthy_values_enable(self):
        for value in ("1", "true", "TRUE", "Yes", " on "):
            self.assertTrue(usage_enabled({USAGE_ENV_VAR: value}), value)

    def test_unset_or_falsey_disables(self):
        self.assertFalse(usage_enabled({}))
        for value in ("", "0", "false", "no", "off", "nope"):
            self.assertFalse(usage_enabled({USAGE_ENV_VAR: value}), value)


class ReadCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, ".credentials.json")

    def _write(self, blob):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(blob, handle)

    def test_reads_token_and_plan(self):
        self._write(
            {"claudeAiOauth": {"accessToken": "tok-123", "subscriptionType": "pro"}}
        )
        creds = read_credentials(self.path)
        self.assertEqual(creds, Credentials(token="tok-123", plan="pro"))

    def test_missing_file_is_none(self):
        self.assertIsNone(read_credentials(self.path))

    def test_malformed_json_is_none(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertIsNone(read_credentials(self.path))

    def test_no_token_is_none(self):
        self._write({"claudeAiOauth": {"subscriptionType": "pro"}})
        self.assertIsNone(read_credentials(self.path))

    def test_plan_optional(self):
        self._write({"claudeAiOauth": {"accessToken": "tok"}})
        self.assertEqual(read_credentials(self.path), Credentials("tok", None))


class FetchUsageBlob(unittest.TestCase):
    def test_success_returns_dict(self):
        captured = {}

        def opener(request, timeout):
            captured["auth"] = request.headers.get("Authorization")
            captured["beta"] = request.headers.get("Anthropic-beta")
            return _FakeResponse(json.dumps(LIVE_BLOB).encode())

        blob = fetch_usage_blob("tok-xyz", opener=opener)
        self.assertEqual(blob, LIVE_BLOB)
        self.assertEqual(captured["auth"], "Bearer tok-xyz")
        self.assertEqual(captured["beta"], "oauth-2025-04-20")

    def test_non_200_is_none(self):
        def opener(request, timeout):
            return _FakeResponse(b"{}", status=401)

        self.assertIsNone(fetch_usage_blob("tok", opener=opener))

    def test_network_error_is_none(self):
        def opener(request, timeout):
            raise urllib.error.URLError("boom")

        self.assertIsNone(fetch_usage_blob("tok", opener=opener))

    def test_bad_body_is_none(self):
        def opener(request, timeout):
            return _FakeResponse(b"not json at all")

        self.assertIsNone(fetch_usage_blob("tok", opener=opener))

    def test_non_object_body_is_none(self):
        def opener(request, timeout):
            return _FakeResponse(b"[1, 2, 3]")

        self.assertIsNone(fetch_usage_blob("tok", opener=opener))


class ParseUsage(unittest.TestCase):
    def test_parses_live_shape(self):
        usage = parse_usage(LIVE_BLOB, plan="pro")
        self.assertEqual(usage.plan, "pro")
        self.assertEqual(usage.session.utilization, 4.0)
        self.assertEqual(usage.weekly.utilization, 24.0)
        self.assertIsNotNone(usage.session.resets_at)  # ISO -> epoch
        # Null windows are absent, not zeroed.
        self.assertIsNone(usage.weekly_opus)
        self.assertIsNone(usage.weekly_sonnet)
        # Add-on present but disabled: kept, flagged off, no dollars invented.
        self.assertFalse(usage.credits.enabled)
        self.assertIsNone(usage.credits.used)

    def test_missing_keys_tolerated(self):
        usage = parse_usage({}, plan=None)
        self.assertEqual(
            usage,
            AccountUsage(
                plan=None, session=None, weekly=None,
                weekly_opus=None, weekly_sonnet=None, credits=None,
            ),
        )

    def test_null_utilization_drops_window(self):
        usage = parse_usage({"five_hour": {"utilization": None}}, plan="pro")
        self.assertIsNone(usage.session)

    def test_bad_reset_time_keeps_window_without_reset(self):
        usage = parse_usage(
            {"five_hour": {"utilization": 7.0, "resets_at": "nonsense"}}, plan="pro"
        )
        self.assertEqual(usage.session.utilization, 7.0)
        self.assertIsNone(usage.session.resets_at)

    def test_enabled_credits_parsed(self):
        blob = {
            "extra_usage": {
                "is_enabled": True,
                "monthly_limit": 10.0,
                "used_credits": 1.2,
                "utilization": 12.0,
                "currency": "USD",
            }
        }
        usage = parse_usage(blob, plan="pro")
        self.assertTrue(usage.credits.enabled)
        self.assertEqual(usage.credits.used, 1.2)
        self.assertEqual(usage.credits.limit, 10.0)
        self.assertEqual(usage.credits.currency, "USD")


class Provider(unittest.TestCase):
    def test_disabled_is_inert(self):
        calls = []
        provider = UsageProvider(
            enabled=False,
            creds_reader=lambda: calls.append("creds") or None,
            fetcher=lambda *a, **k: calls.append("fetch") or {},
        )
        provider.refresh()
        self.assertIsNone(provider.current())
        self.assertEqual(calls, [])  # neither creds nor network touched

    def test_enabled_refresh_populates(self):
        provider = UsageProvider(
            enabled=True,
            creds_reader=lambda: Credentials("tok", "pro"),
            fetcher=lambda token, timeout: LIVE_BLOB,
        )
        provider.refresh()
        usage = provider.current()
        self.assertIsInstance(usage, AccountUsage)
        self.assertEqual(usage.plan, "pro")
        self.assertEqual(usage.weekly.utilization, 24.0)

    def test_failed_fetch_keeps_last_good(self):
        outcomes = [LIVE_BLOB, None]
        provider = UsageProvider(
            enabled=True,
            creds_reader=lambda: Credentials("tok", "pro"),
            fetcher=lambda token, timeout: outcomes.pop(0),
        )
        provider.refresh()  # good
        good = provider.current()
        provider.refresh()  # fetch fails
        self.assertIs(provider.current(), good)  # last good kept, not blanked

    def test_no_creds_keeps_last_good(self):
        creds_outcomes = [Credentials("tok", "pro"), None]
        provider = UsageProvider(
            enabled=True,
            creds_reader=lambda: creds_outcomes.pop(0),
            fetcher=lambda token, timeout: LIVE_BLOB,
        )
        provider.refresh()
        good = provider.current()
        provider.refresh()  # no creds this time
        self.assertIs(provider.current(), good)

    def test_status_message_lifecycle(self):
        outcomes = [None, LIVE_BLOB]
        provider = UsageProvider(
            enabled=True,
            creds_reader=lambda: Credentials("tok", "pro"),
            fetcher=lambda token, timeout: outcomes.pop(0),
        )
        # Before any refresh: loading.
        self.assertIn("loading", provider.status_message())
        provider.refresh()  # fails
        self.assertIn("unavailable", provider.status_message())
        provider.refresh()  # succeeds
        self.assertIsNone(provider.status_message())  # block shows instead

    def test_disabled_has_no_status_message(self):
        provider = UsageProvider(enabled=False)
        self.assertIsNone(provider.status_message())

    def test_token_passed_to_fetcher(self):
        seen = {}
        provider = UsageProvider(
            enabled=True,
            creds_reader=lambda: Credentials("tok-secret", "pro"),
            fetcher=lambda token, timeout: seen.update(token=token) or LIVE_BLOB,
        )
        provider.refresh()
        self.assertEqual(seen["token"], "tok-secret")


if __name__ == "__main__":
    unittest.main()
