# Tokey

A tiny live panel that shows what each Claude Code prompt actually costs, in
tokens and in dollars. I built it because the built-in statusline tells you how
full your context is but never what the last turn spent, and that per-prompt
number is the thing I kept wanting to see.

## What it shows

Claude Code's built-in statusline shows how full your context window is. It does
not show what the prompt you just sent actually cost. This shows that, for every
live session at once.

The view is a roster: one compact block per Claude Code session from the last 7
days, newest first. Every block stacks the same shape, so a newly-started
session just adds another block within a refresh (no restart). Each block is:

- a **header line**: the project name and the session's liveness (`active`, or
  `closing` as it winds down), with `▶` marking the session tokey is
  auto-following (the most recently active one).
- **Context**: `NN% ·· bar · ~Nk left`, an estimate of how full the window is,
  derived from the last prompt's token figures (input plus cache read plus
  cache creation). Treat it as a gauge rather than an exact meter; an estimate
  that overflows the window renders like `104%?` instead of clamping to a clean
  100%, and a model the limit table does not know shows `context limit unknown`.
  The model the window belongs to is shown right-aligned on this line (e.g.
  `opus-4-8`), so you can see which model's limit the percentage is against.
- **Last Prompt**: the session's most recent turn, broken into IN (input plus
  cache creation), OUT, CACHE (cache read, shown only when the turn read cache),
  and the turn's dollar cost. This updates in real time *while a prompt runs* —
  the in-flight turn's figures climb as the response streams, not only once it
  finishes. An unpriceable model shows `$?`; a session that has not produced a
  turn yet shows `no completed turn yet`.
- **Total**: the same breakdown totalled across the whole session (every turn's
  IN / OUT / CACHE and dollars). A `+` on the dollar figure (`$1.234+`) flags a
  partial total when the session contains a turn that could not be priced.

With more than 10 live sessions the newest 10 render and a "+N more" line counts
the rest. A footer shows the active total, `active: $X · Nk tok`, summed over the
sessions currently active (the same scope as the header's active count); a
`(+ unpriced)` flag appears whenever any of them contains turns that could not
be priced.

Each turn is priced with its own model before summing, so sessions that mix
models add up correctly.

The Last Prompt figure is the one I watch: it tells me which prompts are
expensive while I can still change how I am asking, instead of finding out at
the end.

## Requirements

- Python 3.11+
- Claude Code

## Install

Clone the repo, then from inside it:

    pip install -e .

This installs two commands on your PATH: `tokey` (the panel) and `tokey-hook`
(the optional session hook below). Tokey auto-detects your active Claude Code
session by reading the most recently modified transcript under
`~/.claude/projects`. No configuration needed.

If `tokey` is not found after install, your `~/.local/bin` is not on your
PATH. Add it (e.g. `export PATH="$HOME/.local/bin:$PATH"` in your shell rc) and
reopen the terminal.

## Live session tracking (optional)

By default a session's liveness is inferred from its transcript file's
modification time, which has two rough edges: a brand-new session does not show
until its first prompt creates the transcript, and a session you have exited
keeps reading `active` for several minutes (an idle-but-open session and a
closed one look identical on disk).

Installing a pair of Claude Code hooks fixes both: a session appears the instant
it opens (before the first prompt) and disappears the instant you exit it. Add
these to your `~/.claude/settings.json` (the `tokey-hook` command is the entry
point `pip install -e .` put on your PATH):

```json
"hooks": {
  "SessionStart": [
    { "hooks": [ { "type": "command", "command": "tokey-hook" } ] }
  ],
  "SessionEnd": [
    { "hooks": [ { "type": "command", "command": "tokey-hook" } ] }
  ]
}
```

The hook writes a tiny per-session marker under `~/.claude/cc_token_tracker/`
and never prints or blocks. Without the hooks, tokey falls back to the
transcript-mtime behavior, so this is purely an upgrade — nothing breaks if you
skip it (and Claude Code on Windows, which does not run all hooks the same way,
just keeps the fallback).

## Account-level usage (optional)

Tokey's per-session blocks answer "what did this prompt cost". This optional
feature adds the companion question "how much of my plan allowance is left": the
same Session (5-hour) and Weekly windows the claude.ai Usage panel and Claude
Code's `/usage` show, as a block above the sessions, plus a plan badge in the
header:

```
Account-level Claude usage
Session limit  ████░░░░░░░░░░░░░░░░░░░░░░░░░░  15%  resets in 4h 20m
Weekly limit   ███████░░░░░░░░░░░░░░░░░░░░░░░  25%  resets Fri 13:59
```

It is **off by default**. Turn it on by launching the panel with the `cc`
subcommand:

    tokey cc

(For scripts or cron, setting `TOKEY_ACCOUNT_USAGE=1` does the same thing.)

These windows are an opaque server-side **percentage**, not dollars: there is no
dollar cap on a subscription, so tokey shows the percent and reset time only.
(Real dollars appear in one place: the usage-credits add-on, shown only if you
have enabled it.) The bar is tinted by how close you are to the cap — green,
then yellow past 50%, red past 80%.

**How it works and why it is safe.** Tokey is a local CLI. When you enable this,
it reads the OAuth token Claude Code already stored in
`~/.claude/.credentials.json` and sends it **only** to `api.anthropic.com` — the
exact same destination Claude Code itself talks to. There is no server in the
loop: the token never reaches tokey's author or any third party, and tokey never
writes to your credentials file (token refresh stays Claude Code's job).

The lookup runs off the render path so it never stalls the panel, and it
refreshes only every few minutes: the endpoint rate-limits aggressively (the
web Usage panel itself refreshes manually), and the windows are 5-hour and
7-day, so they barely move minute to minute. The endpoint is undocumented and
may change; if a fetch fails (no login, an expired token, no network, or the
endpoint rate-limiting you) the block shows a short `Account-level usage:
unavailable` line and retries on the next refresh, while the rest of tokey is
unaffected. If you have been hitting the endpoint a lot it may rate-limit you
for a while; it clears on its own.

## Companion (optional)

Off by default. Pass `--buddy` (or set `TOKEY_BUDDY=1`) and a small pixel-art cat
is parked in the bottom-right of the footer band:

    tokey --buddy
    tokey cc --buddy

It is a round-headed orange cat with a big cream belly, drawn in colour with
Unicode half-blocks (`▀`): each text cell stacks two pixels, foreground over
background, so the sprite is true pixel art rather than ASCII line art. The grid
is authored wider than tall so the terminal's tall half-pixels stretch it back to
round. Transparent pixels show the panel through, so the cat blends into the box.
The palette is a warm orange body with a darker orange base, a cream belly and
muzzle, a dark outline on every edge, pink-inner ears with dark-brown tips, a
dark-brown forehead cap that dips to a point between the ears, big dark eyes with
a white catch-light, a pink nose, and a short tail at the lower-right.

It parks rather than walks: anchored to the right edge beside the left-aligned
`active:` total, never widening the box, and its rows are reserved so the panel
height never jitters as the roster changes. Its eyes carry the state:

- **idle / working**: eyes open, with a one-tick **blink** every few seconds,
- **stressed** (any session near its context window, or with `tokey cc` an
  account-usage window near its limit): eyes go **wide**.

The blink rides tokey's existing 1-second tick (the frame is just the integer
second), so it costs nothing beyond the redraw already happening; the refresh
rate is unchanged. The sprite data and palette live in their own module
(`mascot.py`), the state brain in `companion.py`; leaving the flag off renders no
sprite and changes nothing.

## Windows

After `pip install -e .`, Windows often reports that `tokey` is "not
recognized". pip dropped it in your Python `Scripts` directory (something like
`...\PythonXX\Scripts`, or `...\Scripts` inside your venv) and that directory is
not on your PATH. Two ways to fix it:

**Option A: put Scripts on PATH (GUI editor).** Open the System Properties
environment-variable editor: press Win+R, run `sysdm.cpl`, go to the *Advanced*
tab, click *Environment Variables*, select `Path`, then *Edit* → *New* and add
your Python `Scripts` directory as its own entry. Reopen the terminal and
`tokey` will resolve.

Do NOT run `setx PATH "%PATH%;C:\...\Scripts"` to do this. `setx` re-expands
`%PATH%`, can fuse your user and system PATH together, and silently truncates
anything past its length limit; it corrupted a real PATH during testing here.
Always edit PATH through the GUI editor above.

**Option B: skip PATH entirely with `python -m`.** You do not have to touch
PATH at all; run the panel directly with:

    python -m cc_token_tracker.roster

If `python` isn't the launcher on your box, `py -m cc_token_tracker.roster`
does the same thing. Either way, the `-m` form must use the *same* interpreter
where you ran `pip install -e .`. If you installed into a venv, that venv's
`python` / `py` is the only one that can import `cc_token_tracker`.

## Run it

Open a second terminal pane next to Claude Code and run:

    tokey

The panel updates once a second. Keep Claude Code in one pane, the tracker in
the other. That two-pane setup is the intended way to use it.

To also show your subscription Session/Weekly usage, run `tokey cc` instead (see
*Account-level usage* above).

Press Ctrl-C to quit the panel.

## Notes

- The tracker reads Claude Code's transcript files; it never scrapes your
  terminal. It runs entirely on your machine and makes no network calls — the
  one exception being the opt-in *Account-level usage* feature above, which when
  you enable it requests your usage summary directly from Anthropic's API (the
  same destination Claude Code uses) and nowhere else.
- It shows every live session at once and follows you across projects
  automatically. Start a new Claude Code session in any folder and it appears as
  a new block within a refresh, auto-followed (▶) as the newest.

A couple of things to know about the dollar figures: they are computed from a
built-in rate table (API list prices as of 2026-06-12), so treat them as close
estimates rather than a billing statement. Cache writes are priced at the
5-minute TTL rate; turns that carry 1-hour cache writes will undercount
slightly. A model the table does not know shows `$?` instead of a price, and
the session total then carries a "(+ unpriced)" marker so you know the figure
is partial rather than silently low.

The context column works the same way: limits come from a built-in per-model
table (documented context windows as of 2026-06-12), so it needs the same kind
of occasional refresh as the rate table when new models ship. A model the
table does not know shows `?` for context rather than a guessed limit, and an
estimate that exceeds the documented window keeps its number with a trailing
`?` (like `104%?`) instead of pretending to be full.

The panel reflects the transcript on disk: without the optional hooks a
brand-new session appears as a block as soon as its transcript exists, showing
`no completed turn yet` until its first prompt completes. With the hooks
installed (see *Live session tracking*) it appears the moment the session opens
and leaves the moment you exit it.
