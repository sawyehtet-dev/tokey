# Changelog

All notable changes to this project are documented here.

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
