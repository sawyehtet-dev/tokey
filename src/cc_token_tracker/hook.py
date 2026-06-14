"""Claude Code SessionStart/SessionEnd hook: write a session liveness marker.

Claude Code runs this as a hook command, piping one small JSON object to stdin
(``hook_event_name``, ``session_id``, ``transcript_path``, ``cwd``, ...). The job
is a pure side effect: drop or close a per-session marker (see
:mod:`cc_token_tracker.markers`) so the tokey roster knows a session is open or
has just closed -- a signal transcript mtime cannot give.

One command serves both events; it branches on ``hook_event_name``, so the same
``tokey-hook`` is registered under both SessionStart and SessionEnd.

A hook that crashes or exits non-zero can disrupt Claude Code, so this swallows
every failure, prints nothing, and always exits 0. It is intentionally silent:
the marker write is the only effect.
"""

from __future__ import annotations

import json
import sys

from cc_token_tracker.markers import CLOSED, OPEN, write_marker

__all__ = ["run_hook", "main"]


def run_hook(stdin_text: str, *, markers_dir: str | None = None) -> bool:
    """Process one hook payload: write the matching marker. Return whether one
    was written.

    Reads ``hook_event_name`` and writes an OPEN marker on SessionStart, a CLOSED
    tombstone on SessionEnd, and does nothing for any other event or for unusable
    input (bad JSON, a non-object top level, a missing session id or transcript
    path). ``markers_dir`` defaults to the real marker store; tests inject a temp
    dir. Never raises.
    """
    try:
        blob = json.loads(stdin_text)
    except (ValueError, TypeError):
        return False
    if not isinstance(blob, dict):
        return False
    event = blob.get("hook_event_name")
    if event not in (OPEN, CLOSED):
        return False
    session_id = blob.get("session_id")
    transcript_path = blob.get("transcript_path")
    cwd = blob.get("cwd")
    if not isinstance(session_id, str) or not session_id:
        return False
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    return write_marker(
        session_id,
        transcript_path,
        cwd if isinstance(cwd, str) else "",
        event,
        markers_dir=markers_dir,
    )


def main() -> int:
    """Console-script / ``python -m`` entry point: one hook tick from stdin.

    Reads stdin once, writes the marker, prints nothing, and always returns 0 --
    a hook must never be able to break Claude Code's session. Every failure is
    swallowed.
    """
    try:
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        run_hook(text)
    except Exception:  # noqa: BLE001 - a hook must never raise to Claude Code
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
