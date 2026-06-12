# Tokey

A tiny live panel that shows what each Claude Code prompt actually costs, in
tokens and in dollars. I built it because the built-in statusline tells you how
full your context is but never what the last turn spent, and that per-prompt
number is the thing I kept wanting to see.

## What it shows

Claude Code's built-in statusline shows how full your context window is. It does
not show what the prompt you just sent actually cost. This shows that, for every
recent session at once.

The view is a roster: one row per Claude Code session from the last 7 days,
newest first, with PROJECT, TOTAL TOK, COST, CONTEXT, and LAST (how long ago
the session last wrote, or `active`). With more than 10 sessions the newest 10
render and a "+N more" line counts the rest. A footer sums everything: session
count on the left, all-sessions dollar and token totals on the right, marked
"(+ unpriced)" whenever any session contains turns that could not be priced.

The active session is marked ▶ and auto-expands inline with:

- **Context**: used / limit tokens, a bar, and `NN% · ~Nk left`. The percent
  is an estimate derived from the last prompt's token figures (input plus
  cache read plus cache creation), so treat it as a gauge rather than an
  exact meter; an estimate that overflows the window renders like `104%?`
  instead of clamping to a clean 100%.
- **Last prompt**: the most recent turn, broken into IN (input plus cache
  creation), OUT, CACHE READ, and COST (the turn's dollar figure).
- **Recent**: the prompts behind it, newest first (the last prompt itself
  excluded), each shown with its dollar cost, a short model tag (`fab5`,
  `op4.8`, `sn4.6`, `hk4.5`), and a snippet of the text you typed. When more
  have scrolled past than fit, a dim "+N more" line says how many are hidden.

Each turn is priced with its own model before summing, so sessions that mix
models add up correctly. With a single session the roster is simply that row
expanded plus the footer.

The per-prompt delta is the one I watch: it tells me which prompts are expensive
while I can still change how I am asking, instead of finding out at the end.

One privacy note: the Recent list prints a snippet of your typed prompt text on
screen, so it is visible to anyone who can see that terminal pane.

## Requirements

- Python 3.11+
- Claude Code

## Install

Clone the repo, then from inside it:

    pip install -e .

This installs one command on your PATH: `tokey` (the panel). Tokey auto-detects
your active Claude Code session by reading the most recently modified transcript
under `~/.claude/projects`. No configuration needed.

If `tokey` is not found after install, your `~/.local/bin` is not on your
PATH. Add it (e.g. `export PATH="$HOME/.local/bin:$PATH"` in your shell rc) and
reopen the terminal.

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

Press Ctrl-C to quit the panel.

## Notes

- The tracker reads Claude Code's transcript files; it never scrapes your
  terminal and sends nothing anywhere. It runs entirely on your machine.
- It follows you across projects automatically. Start a new Claude Code session
  in any folder and the panel switches to it.

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

The panel reflects the transcript on disk, so a brand-new session shows nothing
until its first prompt completes.
