#!/usr/bin/env python3
"""prompt-stamp.py - UserPromptSubmit hook: stamp the time the owner last typed a prompt in a session, for stackshot to read.
stackshot's reader uses the stamp to know when the owner was last at a pane, so it can tell a pane they are working in from one
nobody is watching.

The file contract (agreed with the reader; change it only together with stackshot):
  path     $XDG_STATE_HOME/stackshot/prompts/<session_id>   (default ~/.local/state/stackshot/prompts/<session_id>); folder created if missing
  content  the current epoch seconds as an integer in plain text, no newline
  write    atomic: a temp file in the same folder, then os.replace, so the reader never sees a half-written stamp
  session  <session_id> comes from the hook's stdin JSON; one with a slash, a backslash, a NUL, `..` or a leading dot is skipped, as is none

What counts as the owner's prompt: everything the harness hands this hook except the shapes below. The hook's stdin carries
session_id, transcript_path, cwd, permission_mode, hook_event_name and prompt; none of them marks the origin (the transcript entry has
`origin` and `promptSource`, the hook input does not), so the filter is the text prefix. If a Claude Code release adds an `origin`
object or string to the input, a kind other than "human" is skipped as well. Skipped, measured on recent transcripts:
  Another Claude session sent a message   a message relayed from another session; stackshot and sala notices arrive in this wrapper
  <...                                   task-notification, local-command-caveat / -stdout, system-reminder, `<!-- sala -->` prompts typed into a
                                          pane by sala, the artifact and compaction wrappers
  [for <owner>] [anchor] [stackshot] [stackshot message] [sala] [Cross-session   router, anchor, board and idle notices; <owner> is the
                                          setting owner_name (default "the owner"), the name stackshot's [instance] owner holds
  Caveat:                                 the local-command caveat without its tag
  Stop hook feedback:  This session is being continued  Base directory for this skill:
  (Re-invocation of  Continue from where you left off  Your response above was stopped  [Request interrupted  [Image:
  User: Observation date
                                          harness-written turns: hook feedback, a compaction summary, a skill's loaded text, an interrupt
                                          marker, an attachment note, a headless sdk run
  the setting prompt_skip_prefixes        further prefixes of notices another plugin writes into the prompt (a list; default none)
A slash command or skill the owner types themselves counts as theirs (`/clear`, `/spawn ...`, and the `<command-message>` wrapper a
typed skill leaves in the transcript, which is human-origin there); `<command-name>` wrappers are local-command echoes and are
skipped. A prompt that is empty or not text is skipped too: a stamp means a prompt was seen.

Exit 0 always, nothing printed (never a block, never an ask); any error, bad JSON or unreadable input is silent. Stdlib only, no
transcript read, so it costs one small write per prompt."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402

# first characters / words of a prompt that is not the owner's (see the docstring); matched after a leading-whitespace strip
SKIP_PREFIXES = (
    "Another Claude session sent a message",
    "[anchor]", "[stackshot]", "[stackshot message]", "[sala]", "[Cross-session",
    "Caveat:",
    "Stop hook feedback:", "This session is being continued", "Base directory for this skill:",
    "(Re-invocation of", "Continue from where you left off", "Your response above was stopped",
    "[Request interrupted", "[Image:", "User: Observation date",
)
OWNER_WRAPPERS = ("<command-message>",)   # a skill the owner typed, as the transcript wraps it; every other "<" is a notice or a wrapper


def skip_prefixes():
    owner = _config.get_str("owner_name", "the owner") or "the owner"
    return SKIP_PREFIXES + (f"[for {owner}]",) + tuple(_config.get_list("prompt_skip_prefixes"))


def is_owners(ev):
    origin = ev.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else origin
    if isinstance(kind, str) and kind != "human":
        return False
    prompt = ev.get("prompt")
    if not isinstance(prompt, str):
        return False
    text = prompt.lstrip()
    if not text:
        return False
    if text.startswith(OWNER_WRAPPERS):
        return True
    return not (text.startswith("<") or text.startswith(skip_prefixes()))


def valid_session_id(sid):
    return (isinstance(sid, str) and 0 < len(sid) <= 200 and "/" not in sid and "\\" not in sid and "\0" not in sid
            and ".." not in sid and not sid.startswith("."))


def stamp(sid):
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    folder = os.path.join(base, "stackshot", "prompts")
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(str(int(time.time())))
        os.chmod(tmp, 0o644)   # mkstemp makes it 0600; the reader may be another user's process
        os.replace(tmp, os.path.join(folder, sid))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    try:
        ev = json.load(sys.stdin)
        if isinstance(ev, dict) and valid_session_id(ev.get("session_id")) and is_owners(ev):
            stamp(ev["session_id"])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        sys.exit(0)
