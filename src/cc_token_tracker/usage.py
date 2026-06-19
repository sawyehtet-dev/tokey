"""Account-level Claude subscription usage (opt-in).

Tokey's per-session blocks answer "what did this prompt cost"; this module
answers the companion question "how much of my plan allowance is left". It reads
the same windows the claude.ai Usage panel and Claude Code's ``/usage`` command
show: the 5-hour session window and the 7-day weekly window, each an opaque
server-side *utilization percentage* plus a reset time.

Two things the data is NOT: it is not denominated in dollars (the subscription
windows are percentages only -- real dollars exist solely in the usage-credits
add-on, surfaced here when a user enables it), and it is not per-session (the
endpoint is account-level, so the roster's per-session bars stay context-%).

Privacy and trust: this is OFF by default and gated behind one env var. Tokey is
a local CLI, so when enabled it reads the OAuth token Claude Code already stored
in ``~/.claude/.credentials.json`` and sends it ONLY to ``api.anthropic.com``
(the same destination Claude Code uses). The token never reaches the tool author
or any third party; there is no server in the loop. The credentials file is read
only, never written, so token refresh stays Claude Code's job.

The endpoint (``/api/oauth/usage``) is undocumented and may change, so nothing
here raises: a missing creds file, an expired token (a non-200), a network
error, or a malformed body all degrade to "no usage to show" and the rest of
tokey is unaffected. :class:`UsageProvider` holds the last good reading so the
render path never blocks on the network -- a background driver in the roster
loop calls :meth:`UsageProvider.refresh` on an interval while the panel reads
:meth:`UsageProvider.current` instantly.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "USAGE_ENV_VAR",
    "USAGE_ENDPOINT",
    "DEFAULT_CREDENTIALS_PATH",
    "FETCH_TIMEOUT_SECONDS",
    "UsageWindow",
    "Credits",
    "AccountUsage",
    "Credentials",
    "usage_enabled",
    "read_credentials",
    "fetch_usage_blob",
    "parse_usage",
    "UsageProvider",
]

# The single opt-in switch. Off unless this is set to a truthy value; when unset
# tokey never reads the creds file and never touches the network.
USAGE_ENV_VAR = "TOKEY_ACCOUNT_USAGE"

# The account-usage endpoint and the headers Claude Code itself sends. Verified
# against /api/oauth/usage; the beta header is required for the OAuth token.
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA = "oauth-2025-04-20"
_ANTHROPIC_VERSION = "2023-06-01"
_USER_AGENT = "tokey"

# Where Claude Code stores the OAuth token (and the plan name for the badge).
DEFAULT_CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")

# A short cap on the network read so a slow/hung endpoint cannot stall the
# background refresh for long. The render path never waits on this regardless.
FETCH_TIMEOUT_SECONDS = 6.0

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class UsageWindow:
    """One usage window: a utilization percent and when it resets.

    ``utilization`` is 0..100 (a server-side weighted metric, not tokens or
    dollars). ``resets_at`` is epoch seconds, or None when the endpoint omitted
    a parseable reset time. A window the endpoint reports as null (e.g.
    ``seven_day_opus`` on a Pro plan) is absent entirely, not a zeroed window.
    """

    utilization: float
    resets_at: float | None


@dataclass(frozen=True)
class Credits:
    """The usage-credits add-on -- the ONE place real dollars appear.

    Present in the response always, but meaningful only when ``enabled``; the
    renderer shows it then. ``used``/``limit`` are in ``currency`` (e.g. USD);
    any may be None when the add-on is off.
    """

    enabled: bool
    used: float | None
    limit: float | None
    utilization: float | None
    currency: str | None


@dataclass(frozen=True)
class AccountUsage:
    """One reading of the account's plan usage.

    ``plan`` is the subscription name for the header badge ("pro", "max", ...),
    read from the credentials, not the usage body. ``session`` is the 5-hour
    window, ``weekly`` the 7-day all-models window. ``weekly_opus`` /
    ``weekly_sonnet`` are the per-model weekly windows that populate on higher
    plans and stay None on Pro. ``credits`` carries the usage-credits add-on.
    Any window may be None; the renderer skips what is absent.
    """

    plan: str | None
    session: UsageWindow | None
    weekly: UsageWindow | None
    weekly_opus: UsageWindow | None = None
    weekly_sonnet: UsageWindow | None = None
    credits: Credits | None = None


@dataclass(frozen=True)
class Credentials:
    """The bits of Claude Code's credential file this feature needs.

    ``token`` is the OAuth access token sent (only) to Anthropic; ``plan`` is the
    subscription type for the badge. Nothing else is read, and nothing is ever
    written back.
    """

    token: str
    plan: str | None


def usage_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the opt-in account-usage feature is switched on.

    True only when :data:`USAGE_ENV_VAR` is set to a truthy value (``1``,
    ``true``, ``yes``, ``on``; case-insensitive). Unset or anything else is off,
    so the default install never reads credentials or makes a network call.
    """
    if env is None:
        env = os.environ
    return env.get(USAGE_ENV_VAR, "").strip().lower() in _TRUTHY


def read_credentials(path: str | None = None) -> Credentials | None:
    """Read the OAuth token and plan from Claude Code's creds file. Never raises.

    Returns None when the file is missing, unreadable, malformed, or carries no
    access token -- every one of which simply means "no usage to show". Read
    only: this never modifies the file, so token refresh remains Claude Code's
    responsibility.
    """
    if path is None:
        path = DEFAULT_CREDENTIALS_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    oauth = blob.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    plan = oauth.get("subscriptionType")
    return Credentials(token=token, plan=plan if isinstance(plan, str) else None)


def fetch_usage_blob(
    token: str,
    *,
    opener=urllib.request.urlopen,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> dict | None:
    """GET the usage endpoint with the OAuth token. Returns the JSON, or None.

    Sends the token (only) to Anthropic with the same beta/version headers
    Claude Code uses. Any failure -- a non-200 (e.g. a 401 from an expired
    token), a network error, a timeout, or an unparseable body -- returns None
    so the caller keeps its last good reading. ``opener`` is injectable for
    tests; in production it is :func:`urllib.request.urlopen`. Never raises.
    """
    request = urllib.request.Request(
        USAGE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _OAUTH_BETA,
            "anthropic-version": _ANTHROPIC_VERSION,
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status is not None and status != 200:
                return None
            data = response.read()
        blob = json.loads(data)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return blob if isinstance(blob, dict) else None


def _parse_iso(value: object) -> float | None:
    """An ISO-8601 timestamp (with offset) to epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _num_or_none(value: object) -> float | None:
    """A real number to float, or None (bools and non-numbers are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _window(obj: object) -> UsageWindow | None:
    """One window object to a :class:`UsageWindow`, or None when unusable.

    A null window (utilization absent or non-numeric) yields None so it is
    skipped rather than rendered as a misleading zero.
    """
    if not isinstance(obj, dict):
        return None
    utilization = _num_or_none(obj.get("utilization"))
    if utilization is None:
        return None
    return UsageWindow(
        utilization=utilization, resets_at=_parse_iso(obj.get("resets_at"))
    )


def _credits(obj: object) -> Credits | None:
    """The ``extra_usage`` object to :class:`Credits`, or None when absent."""
    if not isinstance(obj, dict):
        return None
    currency = obj.get("currency")
    return Credits(
        enabled=bool(obj.get("is_enabled")),
        used=_num_or_none(obj.get("used_credits")),
        limit=_num_or_none(obj.get("monthly_limit")),
        utilization=_num_or_none(obj.get("utilization")),
        currency=currency if isinstance(currency, str) else None,
    )


def parse_usage(blob: dict, *, plan: str | None) -> AccountUsage:
    """Shape the ``/api/oauth/usage`` body into an :class:`AccountUsage`. Pure.

    Tolerant by construction: each window is parsed independently and a missing
    or null one becomes None (skipped at render), never a fabricated zero. The
    plan badge comes from the credentials, passed in, not from the body.
    """
    return AccountUsage(
        plan=plan,
        session=_window(blob.get("five_hour")),
        weekly=_window(blob.get("seven_day")),
        weekly_opus=_window(blob.get("seven_day_opus")),
        weekly_sonnet=_window(blob.get("seven_day_sonnet")),
        credits=_credits(blob.get("extra_usage")),
    )


class UsageProvider:
    """Holds the last good :class:`AccountUsage` so render never blocks on IO.

    :meth:`current` returns the latest reading instantly (the render path calls
    this every tick). :meth:`refresh` does the creds read + network fetch and
    swaps in a new reading; the roster loop drives it from a background thread on
    an interval. A disabled provider is inert: :meth:`current` is always None and
    :meth:`refresh` is a no-op, so the default install pays nothing.

    A failed refresh (no creds, expired token, network error) KEEPS the previous
    reading rather than blanking it, so a transient blip does not flicker the
    block away. The single reference swap is atomic under the GIL, so the reader
    thread never sees a half-built value. The creds reader and fetcher are
    injectable for tests.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        creds_reader=read_credentials,
        fetcher=fetch_usage_blob,
        timeout: float = FETCH_TIMEOUT_SECONDS,
    ) -> None:
        self.enabled = enabled
        self._creds_reader = creds_reader
        self._fetcher = fetcher
        self._timeout = timeout
        self._current: AccountUsage | None = None
        # Only meaningful while enabled with no reading yet: "loading" before the
        # first refresh, "unavailable" once one has failed. Once a reading lands
        # the block shows and this is moot (a later failure keeps the last good).
        self._status = "loading"

    def current(self) -> AccountUsage | None:
        """The last good reading, or None if none yet. Instant; never blocks."""
        return self._current

    def status_message(self) -> str | None:
        """A dim status line for the panel, or None when nothing should show.

        None when the feature is off (no opt-in) or a reading exists (the block
        renders instead). Otherwise a short message so an enabled-but-empty panel
        explains itself rather than silently omitting the block: "loading" before
        the first fetch, "unavailable" after one has failed (offline, the
        endpoint rate-limiting us, or an expired login).
        """
        if not self.enabled or self._current is not None:
            return None
        if self._status == "loading":
            return "Account-level usage: loading..."
        return (
            "Account-level usage: unavailable "
            "(offline, rate-limited, or login expired)"
        )

    def refresh(self) -> None:
        """Read creds, fetch, and swap in a fresh reading. Never raises.

        A no-op when disabled. On any failure (no creds, token rejected, network
        error, bad body) the previous reading is kept untouched and the status is
        marked "unavailable" so the panel can say so until a fetch succeeds.
        """
        if not self.enabled:
            return
        creds = self._creds_reader()
        if creds is None:
            self._status = "unavailable"
            return
        blob = self._fetcher(creds.token, timeout=self._timeout)
        if blob is None:
            self._status = "unavailable"
            return
        self._current = parse_usage(blob, plan=creds.plan)
        self._status = "ok"
