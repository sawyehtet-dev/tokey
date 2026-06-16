# Changelog

All notable changes to this project are documented here.

## [0.7.5] - 2026-06-16

### Added
- **Footer Baymax companion with speech bubble**: the footer now carries a soft
  light-blue Baymax (`#8ECAE6`) parked on the right, emoting through his two
  eyes across 16 moods, with a white comic-style speech bubble above him showing
  a rotating one-line reflection. The mood and the line are a single curated
  pair, so they change together in lockstep every 8 seconds and never drift out
  of sync. A braille spinner trails him only while a prompt is actively
  streaming, which is read from the existing transcript write-recency
  (`SessionSummary.last_write`): a streaming turn appends continuously, an idle
  one goes quiet, so no new parsing or threads are added. On by default; pass
  `--no-mood` for the plain `active: $X · N tok` line. The face set, the curated
  line pool, the state signal, and the bubble live in their own pure module
  (`mood.py`), fully unit-tested, with a guard that keeps the pool clean.

### Removed
- **Footer animations**: the opt-in pixel-art cat companion (`--buddy` /
  `TOKEY_BUDDY`) and the experimental Chrome-dino runner (`--runner` /
  `TOKEY_RUNNER`, never released) are removed, along with their modules
  (`companion.py`, `mascot.py`, `runner.py`). The footer is the plain
  `active: $X · N tok` total again; the default install is unchanged.

## [0.7.1] - 2026-06-15

### Added
- **Pixel-art cat companion (optional, opt-in)**: pass `--buddy` (or set
  `TOKEY_BUDDY=1`) and a colour pixel-art cat is parked in the bottom-right of the
  footer band. It is drawn with Unicode half-blocks (`▀`), two stacked pixels per
  text cell (foreground over background), so it is true pixel art, not ASCII line
  art; transparent pixels show the panel through. The sprite is a round-headed
  orange cat with a big cream belly, drawn to match a reference sprite: a warm
  orange body with a darker-orange base, a cream belly and muzzle, a dark outline
  on every edge, pink-inner ears with dark-brown tips, a dark-brown forehead cap
  that dips to a point between the ears, big dark eyes with a white catch-light, a
  pink nose, and a short tail flicking up at the lower-right. The grid is authored
  wider than tall so the terminal's tall half-pixels render it round. It parks
  rather than walks:
  anchored to the right edge beside the left-aligned `active:` total, never
  widening the box, with its rows reserved so the panel height never jitters. Its
  eyes carry the state: open with a one-tick blink every few seconds, or wide when
  any session is near its context window or an account-usage window is near its
  limit. The blink rides the existing 1-second tick (the frame is the integer
  second), so the refresh rate is unchanged and tokey's CPU is flat with or
  without it. Off by default; the default install renders no sprite and is
  byte-for-byte unchanged. The sprite data and palette live in their own module
  (`mascot.py`), the pure `mood()` brain in `companion.py`, so it stays
  unit-tested and trivially removable. See *Companion* in the README.

## [0.7.0] - 2026-06-15

Real-time Last Prompt, a per-session Total, and hook-driven liveness.

### Added
- **Real-time `Last Prompt:`**: the `Last Prompt:` line now follows the in-flight
  turn, so its IN / OUT / CACHE / cost climb live as a response streams instead
  of only updating once the turn completes. An idle tail (a typed prompt with no
  response yet) still falls back to the last completed turn rather than blanking
  to zeros.
- **`Total:` line**: a new line under `Last Prompt:` in every block, the same
  IN / OUT / CACHE / dollar breakdown totalled across the whole session. A `+`
  on the dollar figure (`$1.234+`) flags a partial total when the session has a
  turn that could not be priced.
- **Context-window model**: each block's context row now shows, right-aligned
  under the liveness label, the model the window belongs to (e.g. `opus-4-8`), so
  you can see which model's limit the percentage is measured against.
- **Hook-driven liveness (optional)**: a new `tokey-hook` entry point and a pair
  of Claude Code `SessionStart` / `SessionEnd` hooks. With them installed a
  session appears the instant it opens (before its first prompt) and leaves the
  instant you exit it, via per-session markers under
  `~/.claude/cc_token_tracker/sessions/`. See *Live session tracking* in the
  README.
- **Account-level usage (optional, opt-in)**: run `tokey cc` and the panel adds
  an account block above the sessions showing the subscription Session (5-hour)
  and Weekly windows, plus a plan badge in the header (the `TOKEY_ACCOUNT_USAGE`
  env var still works, for scripts). These
  are percentages with reset times only (subscription windows are not
  denominated in dollars); the usage-credits add-on is shown with real dollars
  when enabled. Off by default. It reads the OAuth token Claude Code stored
  locally and sends it only to Anthropic's own API (`/api/oauth/usage`, the same
  data Claude Code's `/usage` shows), never to any third party, and never writes
  to the credentials file. The lookup runs off the render path on a 60s refresh
  and degrades silently (block omitted) on any failure. See *Account-level
  usage* in the README.

### Changed
- **Liveness now prefers the session marker** when present: a closed session
  drops from the roster at once instead of lingering `active` for up to ten
  minutes, and an idle-but-open session stays `active`. With no marker (the
  hooks not installed, or a session that predates them) liveness falls back to
  the transcript-mtime classification, unchanged.

### Fixed
- **Malformed usage values**: token counts of the wrong JSON type in a transcript
  (a string or float where an integer belongs) now coerce to absent at the parse
  boundary instead of risking a `TypeError` that would freeze a session's panel.

## [0.6.0] - 2026-06-13

The all-expanded multi-session panel.

### Added
- **Liveness scope**: each session is classified active / closing / dropped
  from its transcript mtime. Dropped sessions leave the roster; the header
  counts the active ones only (`N active sessions · [1.0s]`); closing sessions
  stay visible but uncounted.
- **All-expanded blocks**: every live session now renders as its own compact
  block instead of a collapsed row: project name and liveness label, a one-line
  context gauge (`NN% ·· bar · ~Nk left`), and a `Last:` line for its most
  recent completed turn (IN folding cache creation, OUT, CACHE shown only when
  the turn read cache, and the turn's dollar cost). Blocks stack, so a
  newly-started session appears within one refresh with no restart. The `▶`
  marks the auto-followed (newest) session.

### Changed
- **Footer is active-only**: the footer now shows `active: $X.XXX · N.Nk tok`
  over the active sessions only (the same scope as the header count), with a
  right-aligned `(+ unpriced)` flag. The all-discovered lifetime total and the
  session count were dropped (the header already states how many are active).
- The roster is now summary-driven and no longer depends on the live frame, so
  each block shows its session's last COMPLETED turn (stable); the panel no
  longer flashes or shows `running...` for an in-flight prompt.

### Removed
- **RECENT strip**: the recent-prompts list (and its on-screen typed-prompt
  snippets) is gone product-wide. The data is still computed; the roster simply
  no longer renders it.

## [0.5.0] - 2026-06-12

The multi-session roster.

### Added
- **Session roster**: `tokey` now lists every Claude Code session from the
  last 7 days, newest first: PROJECT, TOTAL TOK, COST, CONTEXT, and LAST (a
  humanized age, or `active`). The active session is marked ▶ and auto-expands
  inline with a context gauge plus the same LAST PROMPT and RECENT sections
  the single panel showed. With more than 10 sessions, the newest 10 render
  and a "+N more" line counts the rest. A footer totals all sessions:
  `N sessions` on the left, `all: $X.XX · N.NNM tok` on the right, with
  "(+ unpriced)" when any session has turns that could not be priced.
- **Context estimate**: each session shows how full its context window is,
  estimated from the last prompt's token figures (input plus cache read plus
  cache creation) against a built-in per-model context-limit table
  (documented windows as of 2026-06-12). An estimate that overflows the
  window renders with a trailing `?` (`104%?`) instead of clamping; a model
  missing from the table shows `?`, never a guessed limit.

### Changed
- The roster is the default and only `tokey` view, replacing the single
  panel. With one session it renders that row expanded plus the footer, a
  strict superset of the old panel. No keyboard input was added; the view is
  display-only and still auto-follows the most recently active session.

## [0.4.0] - 2026-06-12

Dollar costs everywhere.

### Added
- **Per-prompt dollar cost**: the LAST PROMPT panel gains a COST cell, priced
  from a built-in rate table keyed on the transcript's model string (API list
  prices as of 2026-06-12; cache writes at the 5-minute TTL rate). A model the
  table does not know renders `$?`, never $0.00.
- **RECENT dollar figures and model tags**: each RECENT row now shows its
  dollar cost plus a short model tag (`fab5`, `op4.8`, `sn4.6`, `hk4.5`, or `?`
  when unknown) between the figure and the prompt snippet.
- **Session dollar total**: a TOTAL COST row beneath TOTAL TOKENS. Each turn is
  priced with its own model before summing, so mixed-model sessions add up
  correctly; if any turn could not be priced, the figure carries a
  "(+ unpriced)" marker instead of silently undercounting.

### Fixed
- A completed prompt now appears in RECENT as soon as the next prompt starts,
  instead of waiting for the next prompt to finish.

## [0.3.0] - 2026-06-11

Direct transcript discovery.

### Changed
- **Session discovery**: the panel now finds the active session by reading the
  most recently modified transcript under `~/.claude/projects`, and follows you
  automatically when you start a session in another project. No configuration
  needed.

### Removed
- **Pointer/shim/statusline mechanism**: the statusline shim, its settings.json
  wiring, and the pointer file are gone; discovery replaced them. `tokey` is
  now the only installed command.

## [0.2.0] - 2026-06-10

History view and panel polish.

### Added
- **RECENT list**: the prompts behind the most recent one, newest-first (the
  hero turn excluded), each shown with its token cost and a short snippet of the
  typed prompt text.
- **"+N more" overflow line**: when more completed prompts exist than the RECENT
  list shows, a dim line reports how many are hidden.

### Changed
- **Panel polish**: thin vertical separators between the hero's IN / OUT /
  CACHE READ fields, a purple/magenta accent on the RECENT cost figures, and the
  session total reflowed to a left-aligned "TOTAL TOKENS" label with the figure
  right-aligned.
- **Width cap**: the panel caps at a maximum width on wide terminals instead of
  stretching edge to edge; snippets truncate against the panel's inner width.

## [0.1.0] - 2026-06-09

Initial release: per-prompt token cost (the delta) and the running session total.
