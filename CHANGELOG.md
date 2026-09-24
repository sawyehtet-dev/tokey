# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- **Pricing and context entries for `claude-opus-5-5`, `claude-mythos-5-1`, and
  `claude-mythos-5`** (all 1M context window), verified against the live docs
  on 2026-09-24. Opus 5.5 is $4/$20 with cache reads at 0.05x input ($0.20/MTok),
  so it is not a copy of the Opus 5 row. Opus 5.5 turns were previously
  unpriceable, which left every Opus 5.5 session rendering `$X+` with a `?`
  context. The Mythos rows mirror their Fable twins.
- **Durable spend history (`tokey log`)**: one row per finished session in a
  stdlib-`sqlite3` database at `~/.claude/tokey/history.db`, plus a `tokey log`
  screen showing recent days and all-time spend per project. The live roster
  only ever saw a 7-day window of transcripts that Claude Code eventually
  rotates away, so monthly and per-project spend were unanswerable; they now
  survive the transcripts they came from. `history.py` consumes the frozen
  `SessionSummary` and recomputes nothing, and the `unpriced` partial-total flag
  is carried per row so an aggregate renders `$123.45+` rather than a clean lie.
  Two writers, both funnelling through one UPSERT on `session_id`: `tokey-hook`
  on SessionEnd (primary), and `tokey` backfilling once at startup to catch
  sessions whose SessionEnd never fired (crash, `kill -9`, closed terminal).
  No new dependency and no hook re-registration: the already-registered
  `tokey-hook` gained the behaviour.
- **Pricing and context entries for `claude-fable-5-1`** (1M context window).
  Its cache reads bill at 0.025x input ($0.25/MTok), off the standard 0.1x
  multiplier, so it is not a copy of the Fable 5 row.
- **`tokey --version` (`-V`)**: prints the version and exits without entering
  the render loop. The number is read straight from the package, so it is
  correct regardless of when tokey was last reinstalled.
- **Pricing and context entries for `claude-opus-5`** (1M context window). Both
  hand-maintained tables move together, as the per-model tables require.

### Fixed
- **A `<synthetic>` notice no longer unprices the turn it ends.** Claude Code
  writes a zero-token usage block on notices such as "You've hit your session
  limit". It counted as the turn's last usage-bearing record, so its
  unpriceable model dropped the turn's real tokens from the dollar sum and
  flagged the session `$X+`; the context estimate also fell to `?`. The parser
  now treats an all-zero usage block as absent. This fix re-priced all 13
  flagged sessions in a real history.

- **1-hour cache writes are priced at the 1-hour rate.** Claude Code writes
  nearly all of its prompt cache with the 1-hour TTL, billed at 2x input, but
  every cache write was priced at the 5-minute 1.25x rate, undercounting the
  cache-write share of every session by 37.5%. The parser now reads the
  `cache_creation.ephemeral_1h_input_tokens` split and pricing bills that share
  at 2x input. Token totals are unchanged: the split only moves the rate.

### Changed
- **Startup backfill covers every transcript on disk**, not just the roster's
  7-day window. Claude Code keeps transcripts for weeks, and the sessions the
  live view has already dropped are the ones history exists for. The scan runs
  on a background thread so the first frame is not delayed.
- **Pricing and context tables re-verified against the live docs (2026-09-15).**
  Claude Sonnet 5's `$2/$10` launch pricing is now the standard price: the
  increase to `$3/$15` that the table's comment warned was coming on
  2026-09-01 was cancelled and will not occur. The rates were already correct;
  the stale comment was the hazard.

- **Version is now single-sourced** from `cc_token_tracker.__version__`;
  `pyproject.toml` derives it via setuptools' dynamic-version `attr`, so a
  release bumps the number in one place instead of two.
- **The two per-turn dollar helpers moved to `turn_cost.py`** and are now public
  (`turn_usd`, `session_cost`). They previously lived in the render layer as
  `display._turn_usd` / `display._session_cost`, which forced `sessions.py` to
  import from a module further down the render path than itself. Pricing rules
  are unchanged: each turn is priced by its own model, an unpriceable
  token-bearing turn is excluded and flips `unpriced`, and a zero-token
  in-flight turn never flips it.
- **Lint is enforced, not advisory**: `pyproject.toml` now selects
  `E,W,F,I,UP,B,SIM,RUF` at 88 columns, which CI already runs as a gate.

### Removed
- **The single-session panel (`display.py`)** and its 562-line test file. It was
  superseded as `tokey`'s view by the roster in v0.5.0 and its RECENT strip was
  dropped product-wide in v0.6.0, leaving roughly 1,100 lines that no entry
  point could reach: `render_panel`, `compute_frame`, `Frame`, `RecentEntry`,
  `DisplayState`, the flash state, the model tags, and its own poll loop. The
  four symbols still in use moved to where they belong (above). `tokey`'s
  rendered output is byte-identical.
- **`reader.find_active_transcript`**, the single-session recency resolver, whose
  only caller was the deleted panel. Discovery has run through
  `sessions.discover_sessions` since v0.5.0.
- **`mood.CLAUDE_CORAL`**, an unused constant left behind when the footer
  companion went light blue in v0.7.5.
- **`graphify-out/` is no longer tracked in git.** It is generated output
  carrying its own hash-keyed cache; it is now ignored, along with `.ruff_cache/`
  and `.vscode/`. The files stay on disk.

### Fixed
- **Malformed transcript scalars could crash a session's panel.** The parser
  carried `message.id`, `model`, `role` and `stop_reason` through verbatim
  whatever their JSON type. A non-string `id` (an object or array) reached
  accounting, where it is used as a dict key, and raised
  `TypeError: unhashable type` mid-pipeline; a non-string `model` reached
  pricing's regex and raised there. Both are now coerced to `None` at the parse
  boundary, matching how token counts and `cwd` were already handled, so the
  never-raises contract every downstream layer documents actually holds.
- **The marker store grew without bound.** Only CLOSED tombstones were pruned on
  read, so a session killed hard (no `SessionEnd`, so no tombstone) left its
  OPEN marker under `~/.claude/cc_token_tracker/sessions/` forever. Markers of
  either event are now unlinked past the same 7-day TTL, which matches the
  roster's discovery window; liveness is unaffected, since a crashed session is
  already classified dropped after two hours.
- **Short speech bubbles rendered lopsided.** The footer bubble's bottom border
  spends three cells on its corners and tail notch, so a phrase one cell wide
  produced a bottom row wider than its top. The inner width is floored so every
  row of a bubble is the same width for any phrase.
- **`run-tokey.bat` reported "Tokey closed" when tokey had failed to start.** It
  now checks the exit code and points at `setup.bat` when the package is not
  importable. `setup.bat` additionally verifies the install by importing the
  package, instead of trusting pip's exit code and leaving a Desktop launcher
  that dies on first use.
- **Documentation corrections**: the README claimed the account-usage bars were
  tinted green/yellow/red by fill level, which they never were (each row has a
  fixed colour). Em dashes were removed from `README.md` and `CLAUDE.md`, which
  had been violating the project's own standing rule, and `CLAUDE.md`'s pipeline
  diagram and money invariant now name the modules that actually exist.

## [0.7.6] - 2026-06-20

### Fixed
- **macOS: account usage (`tokey cc`) now works**: on macOS, Claude Code stores
  its OAuth token in the login Keychain rather than the plaintext
  `~/.claude/.credentials.json` file Linux/WSL use, so the opt-in account-usage
  block and the plan badge silently never appeared on a Mac (the missing file
  degraded to "no usage to show"). `read_credentials` now falls back to the
  Keychain on macOS, shelling out read-only to the built-in `security` tool
  (`find-generic-password -s "Claude Code-credentials" -w`); the file is still
  tried first, so Linux/WSL behaviour is unchanged and never invokes `security`.
  Every failure (not macOS, the item absent, the binary missing, a timeout)
  degrades to the same "no usage" path, so the rest of the panel is unaffected.
- **macOS/Linux: panel crash in a non-UTF-8 locale**: the UTF-8 stdout guard in
  `main()` previously fired on Windows only. A bare `C`/`POSIX` locale on
  macOS/Linux (cron, a GUI-spawned shell, `LANG` unset) also yields an ASCII
  stdout that cannot encode the panel's Unicode bars and box characters, raising
  `UnicodeEncodeError`. The guard now forces UTF-8 whenever stdout is not already
  UTF-8, on any platform, so tokey renders the same everywhere.

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
- **Two-click Windows setup**: `setup.bat` and `run-tokey.bat` ship in the repo
  root so Windows users install and launch by double-clicking, never touching a
  terminal or PATH. `setup.bat` locates Python (`py`, then `python`), enforces
  3.11+ with a friendly pointer to the installer when it is missing, installs
  tokey (a regular install, so the downloaded folder can be deleted afterward),
  and copies a standalone launcher to the Desktop as `Tokey.bat` so it still
  starts once the folder is gone. Both launchers run the panel via
  `py -m cc_token_tracker.roster` (PATH-proof) and pass arguments through (e.g.
  `Tokey.bat cc`). The README's Windows section is now a linear quick start with
  the PATH and `setx` notes demoted to troubleshooting.

### Removed
- **Footer animations**: the opt-in pixel-art cat companion (`--buddy` /
  `TOKEY_BUDDY`) and the experimental Chrome-dino runner (`--runner` /
  `TOKEY_RUNNER`, never released) are removed, along with their modules
  (`companion.py`, `mascot.py`, `runner.py`). The footer is the plain
  `active: $X · N tok` total again; the default install is unchanged.

### Fixed
- **Windows: panel crash on the cp1252 console**: on Windows, Python defaults
  stdout to the locale codepage (cp1252), which cannot encode the panel's Unicode
  bars, arrows, and box characters, so output raised `UnicodeEncodeError` whenever
  it was not attached to a live console (piped, redirected, or some terminals).
  `main()` now forces UTF-8 stdout on Windows, so tokey renders the same in
  Command Prompt and PowerShell, including when redirected.
- **Windows: backslashes in the session title**: the `~`-relative project title
  used the OS path separator, rendering `~\Desktop\tokey` on Windows; it is now
  normalized to forward slashes (`~/Desktop/tokey`) so the title reads the same
  on every platform.

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
