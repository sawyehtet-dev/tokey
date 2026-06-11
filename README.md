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

- **Last prompt**: the most recent turn, broken into IN (input plus cache
  creation), OUT, and CACHE READ.
- **Recent**: the prompts behind it, newest first (the last prompt itself
  excluded), each shown with its token cost and a short snippet of the text you
  typed. When more have scrolled past than fit, a dim "+N more" line says how
  many are hidden.
- **Session total**: the running token total for the whole current session.

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

If `~/.claude/settings.json` does not exist yet, just create it with exactly the
content above. A fresh Claude Code install often ships no settings file at all,
so there is nothing to merge into. This snippet is the whole file.

If your `~/.local/bin` is not on PATH and you cannot change that, use the
absolute path instead (replace YOURNAME):

    "command": "/home/YOURNAME/.local/bin/tokey-shim"

One reason this snippet lives here in the README instead of being shipped as a
file in the repo: `.claude/` is gitignored on purpose, because it holds
machine-specific absolute paths that would be wrong on anyone else's machine. So
you drop the snippet into your own `~/.claude/settings.json` by hand.

## Windows

After `pip install -e .`, Windows often reports that `tokey` and `tokey-shim`
are "not recognized". pip dropped them in your Python `Scripts` directory
(something like `...\PythonXX\Scripts`, or `...\Scripts` inside your venv) and
that directory is not on your PATH. Two ways to fix it:

**Option A: put Scripts on PATH (GUI editor).** Open the System Properties
environment-variable editor: press Win+R, run `sysdm.cpl`, go to the *Advanced*
tab, click *Environment Variables*, select `Path`, then *Edit* → *New* and add
your Python `Scripts` directory as its own entry. Reopen the terminal and
`tokey` / `tokey-shim` will resolve.

Do NOT run `setx PATH "%PATH%;C:\...\Scripts"` to do this. `setx` re-expands
`%PATH%`, can fuse your user and system PATH together, and silently truncates
anything past its length limit; it corrupted a real PATH during testing here.
Always edit PATH through the GUI editor above.

**Option B: skip PATH entirely with `python -m`.** You do not have to touch
PATH at all; call the modules directly. Use this statusLine command in
`~/.claude/settings.json`:

    {
      "statusLine": {
        "type": "command",
        "command": "python -m cc_token_tracker.shim"
      }
    }

and run the panel with:

    python -m cc_token_tracker.display

If `python` isn't the launcher on your box, `py -m cc_token_tracker.shim` and
`py -m cc_token_tracker.display` do the same thing. Either way, the `-m` form
must use the *same* interpreter where you ran `pip install -e .`. If you
installed into a venv, that venv's `python` / `py` is the only one that can
import `cc_token_tracker`.

If the panel just sits on "waiting for first command" on Windows, the shim is
probably failing silently. Check its error log at
`%USERPROFILE%\.claude\cc_token_tracker\shim_error.log` (it lives under your user
profile, NOT `%TEMP%`). Whatever the shim choked on gets appended there.

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
