# Changelog

All notable changes to this project are documented here.

## [0.2.0] - 2026-06-10

History view and panel polish.

### Added
- **RECENT list** — the prompts behind the most recent one, newest-first (the
  hero turn excluded), each shown with its token cost and a short snippet of the
  typed prompt text.
- **"+N more" overflow line** — when more completed prompts exist than the RECENT
  list shows, a dim line reports how many are hidden.

### Changed
- **Panel polish** — thin vertical separators between the hero's IN / OUT /
  CACHE READ fields, a purple/magenta accent on the RECENT cost figures, and the
  session total reflowed to a left-aligned "TOTAL TOKENS" label with the figure
  right-aligned.
- **Width cap** — the panel caps at a maximum width on wide terminals instead of
  stretching edge to edge; snippets truncate against the panel's inner width.

## [0.1.0] - 2026-06-09

Initial release: per-prompt token cost (the delta) and the running session total.
