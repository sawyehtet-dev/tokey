"""Per-model context-window limits and used-context estimation.

Pure logic: no IO, no clock. The limits table mirrors the pricing table's
honesty contract: a model the table does not know yields ``None``, and the
caller renders the unknown (``?``), never a fabricated default limit.

Used context is an ESTIMATE: the input + cache_read + cache_creation token
counts of the session's most recent usage-bearing assistant record -- i.e.
what the last prompt actually sent. Compaction, tool results landing after
that record, and system overhead are not modeled; the percent can therefore
exceed 100, and the renderer marks that overflow honestly instead of clamping.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cc_token_tracker.parser import TranscriptRecord
from cc_token_tracker.pricing import normalize_model

__all__ = ["context_limit", "ContextEstimate", "estimate_context"]

# limits as of 2026-06-12, source: platform.claude.com/docs (Models overview,
# /docs/en/about-claude/models/overview, "Context window" per model). Verified
# against the live page on 2026-06-12. claude-opus-4-8 is 200k on Microsoft
# Foundry only; transcripts here come from Claude Code on the Claude API
# surface, so the documented API window (1M) applies. A model absent from this
# table yields None -- the renderer shows "?", never a guessed 200k.
_CONTEXT_LIMITS: dict[str, int] = {
    "claude-fable-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
}


def context_limit(model: str | None) -> int | None:
    """Documented context window for a transcript model string, or ``None``.

    Same lookup discipline as pricing: the model is tried verbatim first, then
    with the trailing ``-YYYYMMDD`` date suffix normalized away (reusing
    pricing's :func:`normalize_model`). Both missing -- or ``model`` itself
    ``None`` -- yields ``None``; no limit is ever invented.
    """
    if model is None:
        return None
    limit = _CONTEXT_LIMITS.get(model)
    if limit is None:
        limit = _CONTEXT_LIMITS.get(normalize_model(model))
    return limit


@dataclass(frozen=True)
class ContextEstimate:
    """Estimated context occupancy of one session.

    ``used`` is the last prompt's input-side token total (see module
    docstring), ``None`` when the session has no usage-bearing assistant
    record yet. ``limit`` is the documented window for that record's model,
    ``None`` when the model is absent from the table (or absent from the
    record). ``percent`` is ``used / limit * 100`` and may exceed 100 (the
    estimate overflowed); it is ``None`` whenever either input is.
    """

    used: int | None
    limit: int | None
    percent: float | None


_NO_ESTIMATE = ContextEstimate(used=None, limit=None, percent=None)


def estimate_context(
    records: Iterable[TranscriptRecord | None],
) -> ContextEstimate:
    """Estimate context occupancy from parsed records, in transcript order.

    Scans for the MOST RECENT assistant record carrying a usage block and sums
    its input, cache_read, and cache_creation counts (absent counts coalesce
    to 0, matching accounting). No such record -- an empty or prompt-only
    transcript -- yields the all-``None`` estimate rather than a fake 0/limit.
    """
    last: TranscriptRecord | None = None
    for record in records:
        if record is None:
            continue
        if record.type == "assistant" and record.usage is not None:
            last = record

    if last is None or last.usage is None:
        return _NO_ESTIMATE

    usage = last.usage
    used = (
        (usage.input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
        + (usage.cache_creation_input_tokens or 0)
    )
    limit = context_limit(last.model)
    percent = used / limit * 100.0 if limit else None
    return ContextEstimate(used=used, limit=limit, percent=percent)
