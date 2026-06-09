"""Claude Code statusline shim.

Claude Code runs this module as the statusline command. Each tick it pipes a
single small JSON object to stdin (session_id, cwd, model, workspace, version,
cost, and a top-level transcript_path), and whatever this prints to stdout
becomes the statusline.

The real job is the side effect: write the current transcript path to a pointer
file the reader polls. The printed statusline text is cosmetic and intentionally
minimal; the rich per-command view is a separate Display process.

json.loads is correct here: the stdin blob is one small JSON object, not
transcript lines. This module deliberately does not touch parse_line, which is
for transcript records.

Nothing in this module raises. A statusline command that crashes or exits
non-zero would break Claude Code's status bar, so every entry point swallows its
failures and still returns a usable string.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

__all__ = [
    "DEFAULT_POINTER_PATH",
    "extract_transcript_path",
    "write_pointer_atomic",
    "run_shim",
    "main",
]

# Where the shim writes and the reader polls. The two must agree on this path.
DEFAULT_POINTER_PATH = os.path.expanduser("~/.claude/cc_token_tracker/pointer")

# Static fallback so the statusline is never empty, even on garbage stdin.
_FALLBACK_STATUS = "cc-token-tracker"


def extract_transcript_path(stdin_text: str) -> str | None:
    """Return the top-level transcript_path from the statusline blob, or None.

    Pure: json.loads the text and return transcript_path only when it is a
    non-empty string. Empty input, invalid JSON, a non-object top level, a
    missing key, a null value, or a non-string value all return None. Never
    raises.
    """
    try:
        blob = json.loads(stdin_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(blob, dict):
        return None
    value = blob.get("transcript_path")
    if isinstance(value, str) and value:
        return value
    return None


def write_pointer_atomic(path: str, pointer_path: str) -> bool:
    """Atomically write `path` into the pointer file. Return True on success.

    The temp file is created with mkstemp in the pointer's own directory so the
    final os.replace stays on one filesystem and cannot raise cross-device. The
    replace is atomic, so a polling reader never sees a half-written pointer.

    On a fresh install the pointer's parent directory may not exist yet, so it is
    created first. Without that, mkstemp would raise, the OSError swallow would
    return False, nothing would be written, and the whole tracker chain would go
    silently dark.

    On any OSError (bad permissions, an uncreatable directory, full disk) any
    temp file is removed and the function returns False. Never raises.
    """
    directory = os.path.dirname(pointer_path) or "."
    fd: int | None = None
    temp_path: str | None = None
    try:
        # Create the pointer's parent dir before mkstemp, which would otherwise
        # raise FileNotFoundError on a fresh install. This stays inside the
        # OSError swallow on purpose: makedirs can itself raise (FileExistsError
        # when a regular file sits at the path, NotADirectoryError when a path
        # component is a file), and the returns-False contract must hold there
        # too. A bare filename with no dir component skips makedirs rather than
        # calling os.makedirs("").
        parent = os.path.dirname(pointer_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None  # the file object owns the descriptor now
            handle.write(path)
            handle.flush()
        os.replace(temp_path, pointer_path)
        return True
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def _statusline(stdin_text: str) -> str:
    """Build a minimal, non-empty cosmetic statusline. Never raises.

    Uses the model display name and the cwd basename when available, and falls
    back to a static label otherwise. The exact text is not a contract.
    """
    try:
        blob = json.loads(stdin_text)
        if not isinstance(blob, dict):
            return _FALLBACK_STATUS
        model = blob.get("model")
        name = model.get("display_name") if isinstance(model, dict) else None
        cwd = blob.get("cwd")
        base = os.path.basename(cwd) if isinstance(cwd, str) and cwd else None
        parts = [p for p in (name, base) if isinstance(p, str) and p]
        return " ".join(parts) if parts else _FALLBACK_STATUS
    except (ValueError, TypeError):
        return _FALLBACK_STATUS


def run_shim(stdin_text: str, pointer_path: str) -> str:
    """Process one statusline tick: record the transcript path, return a status.

    Extract the transcript path; when present, write it to the pointer
    atomically (its success bool is not part of the return). Always returns a
    non-empty statusline string. Never raises.
    """
    path = extract_transcript_path(stdin_text)
    if path is not None:
        write_pointer_atomic(path, pointer_path)
    return _statusline(stdin_text)


def main() -> int:
    """Console-script entry point: process one statusline tick from stdin.

    Mirrors the __main__ behavior exactly so the console_scripts mapping and
    ``python -m`` share ONE path: read stdin once, never block, never exit
    non-zero. A statusline command must not be able to break Claude Code's status
    bar, so any failure is swallowed and the static fallback is printed. Always
    returns 0.
    """
    try:
        text = sys.stdin.read()
        print(run_shim(text, DEFAULT_POINTER_PATH))
    except Exception:
        print(_FALLBACK_STATUS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
