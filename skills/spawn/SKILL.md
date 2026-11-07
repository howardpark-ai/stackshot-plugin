---
name: spawn
description: Spawn a new tmux + Claude Code session via `sala spawn` (salamander), named, Remote-Control visible, optionally briefed from a file, and immediately protected by the pane-revive sidecar. Use when the user asks to spawn / create / start a new session, a dedicated session for some work, or a throwaway/scratch session. Also handles listing the live sessions. Needs sala installed on the box that runs the tmux server and the long-lived Claude panes.
model: sonnet
effort: low
---

Spawn a new tmux + Claude session. The mechanics live in **salamander** (`sala spawn`): call it, don't reimplement it. It encodes two
gotchas that cost time when this was done by hand: the `/rename` requirement (because `--remote-control <name>` does not reliably set
the name), and the separate-Enter + copy-mode handling for `send-keys`.

## Before anything

Run `command -v sala`. If sala is not installed, say so in one line and stop: this skill does nothing without it (sala is optional for
the rest of the plugin). Do not fall back to raw `tmux` commands.

## Steps

1. **Name.** If the user gave no name, **do not invent one**: run the script bare and let it generate `scratch-<YYYYMMDD>-<word>`
   (date plus a random word, so same-day throwaways stay distinct). A bare `/spawn` means "throwaway", so skip the brief too and just
   report. If the user named it, or the work is clearly a dedicated thread, follow the naming convention of the setup: a project's
   sessions follow its project name (`<project name>-<suffix>`), as the project's page lists them in its `agents:` map. Lowercase,
   hyphenated, no dots or colons. Say which name you chose and why in one clause.

2. **Decide whether it needs a brief.** A dedicated session for real work almost always does, a throwaway does not. If it does,
   write the brief to a file that survives the session (in the project's notes or context folder), commit it, then pass `--brief`.
   A file can be re-read after a crash; a `--prompt` cannot. A good brief carries: the hard constraints, why the work exists, what
   is already done and where it lives, what is outstanding in priority order, and the conventions that apply.

3. **Run it:**
   ```bash
   sala spawn                                   # throwaway, auto-named
   sala spawn <name> [--brief <path>] [--prompt <text>] [--fork <uuid>]
   sala spawn --list                            # live panes + versions + RC ids
   ```
   Flags work without a name (`sala spawn --brief foo.md` auto-names and briefs).

4. **Report** the name, the claude.ai/code URL it prints, and, if a brief was passed, that the new session is reading it. Relay any
   warning line verbatim.

## Notes

- Default cwd is `$HOME` and should stay that way: every pane shares it, which is exactly why `sala revive` resumes by session UUID
  and never `claude -c`.
- `sala spawn` takes a full tmux-resurrect snapshot (which refreshes the sidecar) on exit, so the new pane is mappable to its
  conversation immediately.
- **Model / effort follow the global `~/.claude/settings.json`** for new spawns: `sala spawn` pins nothing. `--model` / `--effort`
  remain as per-spawn overrides; `/effort` in the pane also works.
- **Revived panes keep what they were on.** Effort does not survive a plain `claude --resume` (model does), so `sala revive` reads the
  live value off the transcript and passes `--effort` back; do not "simplify" that away or every recycle silently resets spawned panes.
- **"Bring back X" (a pane whose claude exited: a feedback-modal /exit, a crash, a kill):** the tmux session is still there at a bare
  shell, so do NOT `--fork` or spawn a twin (spawn refuses: "tmux session already exists"). `sala spawn --list` shows such panes as
  `(shell) claude exited - revive: ...`; run `sala revive --only X`: it resumes that pane's own conversation from the sidecar and
  touches nothing else.
- Ending a session: `sala retire NAME` (the exact tmux session name). Do this only when asked, and never on a pane whose Claude is
  busy (`retire` refuses a busy pane). It also refuses a session that is the mover of an open board item unless `--leave-items`, so
  before retiring, check the stackshot board (`brief`) for items owned by that session and reassign (`update` owner) or `complete`
  them; the board flags a dead owner as offline. A tmux session with no Claude in it is closed with `sala close-shell NAME`.
