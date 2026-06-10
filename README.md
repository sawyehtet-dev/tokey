# Tokey

A tiny live panel that shows what each Claude Code prompt actually costs in
tokens. I built it because the built-in statusline tells you how full your
context is but never what the last turn spent, and that per-prompt number is the
thing I kept wanting to see.

## What it shows

Claude Code's built-in statusline shows how full your context window is. It does
not show what the prompt you just sent actually cost. This shows that, plus a
little history around it.

The panel has three sections:

- **Last prompt** — the most recent turn, broken into IN (input plus cache
  creation), OUT, and CACHE READ.
- **Recent** — the prompts behind it, newest first (the last prompt itself
  excluded), each shown with its token cost and a short snippet of the text you
  typed. When more have scrolled past than fit, a dim "+N more" line says how
  many are hidden.
- **Session total** — the running token total for the whole current session.

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

This installs two commands on your PATH: `tokey` (the panel) and
`tokey-shim` (the statusline hook).

If `tokey` is not found after install, your `~/.local/bin` is not on your
PATH. Add it (e.g. `export PATH="$HOME/.local/bin:$PATH"` in your shell rc) and
reopen the terminal.

## Set up the statusline hook

Claude Code needs to tell the tracker which session is live. Add this to your
Claude Code `settings.json` (usually `~/.claude/settings.json`):

    {
      "statusLine": {
        "type": "command",
        "command": "tokey-shim"
      }
    }

If your `~/.local/bin` is not on PATH and you cannot change that, use the
absolute path instead (replace YOURNAME):

    "command": "/home/YOURNAME/.local/bin/tokey-shim"

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

A couple of things to know: the numbers are token counts, not dollars, so read
them as relative cost between prompts rather than a billing figure. The panel
also reflects the transcript on disk, so a brand-new session shows nothing until
its first prompt completes.
