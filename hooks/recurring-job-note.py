#!/usr/bin/env python3
"""recurring-job-note.py - PreToolUse hook (matcher `CronCreate|Skill|Bash|Write|Edit|MultiEdit`): a NOTE (never a block, never an
ask) when a session sets up a recurring job. The rule: a session never holds a recurring job; recurring work goes to a scheduler
that outlives sessions: a systemd user timer (added with `sala job add` where sala is installed) when it needs the machine, a Claude
Code routine (/schedule) when it does not. A one-shot reminder tied to the session's own work may stay in the session.

Matches
  CronCreate  with `recurring` true, or with no `recurring` at all (the tool's default is recurring) unless the cron pins the minute, hour,
              day of month and month to numbers, which fires once (a one-shot form); `recurring` false never gets the note
  Skill       the `loop` skill (also `<plugin>:loop`), which is how `/loop` runs

No-poll hosts leg, off unless the setting no_poll_hosts lists hosts (domain names, subdomains included; default none). A second note,
the text of the setting no_poll_note, when a call sets up the polling of such a host. A single fetch gets nothing.
  Bash        a fetch (curl, wget, httpie, a python one-liner ...) of such a host inside a loop (while, until, for ... do, watch), or a
              `sala job add` / `crontab` / systemd timer command that names one
  CronCreate  a recurring job whose prompt names one
  Write, Edit, MultiEdit  a *.timer / *.service / script file (not a test file) whose new text names one together with a repeat
              (a timer directive, cron, a loop, a sleep)
Output: hookSpecificOutput.additionalContext, exit 0; the permission flow is not touched. Any error: silent exit 0. Stdlib only."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402

NOTE = ("A session never holds a recurring job: use a scheduler that outlives sessions: a systemd user timer (via `sala job add` where "
        "sala is installed) when it needs the machine, or /schedule (a routine) when it does not. "
        "A one-shot reminder tied to this session's own work may stay. (recurring-job-note.py)")
DEFAULT_NO_POLL_NOTE = "Never poll {hosts}: fetch single pages on request only. (a note from recurring-job-note.py)"
LOOP_SKILL = re.compile(r"^(?:[\w.-]+:)?loop$", re.I)


def one_shot_cron(cron):
    """True when the five fields pin minute, hour, day of month and month to plain numbers: it fires once a year at most."""
    f = str(cron or "").split()
    return len(f) == 5 and all(re.fullmatch(r"\d+", x) for x in f[:4])


# ------------------------------------------------------------------------------------------------ the no-poll hosts leg
def no_poll_hosts():
    return [h.strip().lower().lstrip(".") for h in _config.get_list("no_poll_hosts") if h.strip()]


def host_regex(hosts):
    """A regex for the hosts and their subdomains, or None for no host."""
    if not hosts:
        return None
    return re.compile(r"(?<![\w-])(?:[\w-]+\.)*(?:" + "|".join(re.escape(h) for h in hosts) + r")(?![\w-])", re.I)


def no_poll_note(hosts):
    text = _config.get_str("no_poll_note") or DEFAULT_NO_POLL_NOTE
    return text.replace("{hosts}", ", ".join(hosts))


FETCH = re.compile(r"(?:^|[\s;&|(`])(?:curl|wget|xh|aria2c|lynx|w3m|http)\s|urllib|requests\.|httpx|aiohttp|fetch\(", re.I)
SHELL_LOOP = re.compile(r"(?:^|[\s;&|(])(?:while|until)\s|\bfor\b[^\n]*\bin\b[^\n]*(?:;|\n)\s*do\b|(?:^|[;&|(\n]\s*)watch\s", re.I)
BASH_TIMER = re.compile(r"sala\s+job\s+add\b|\bcrontab\b|systemd-run\b|OnCalendar|OnUnitActiveSec|OnBootSec", re.I)
FILE_REPEAT = re.compile(r"OnCalendar|OnUnitActiveSec|OnUnitInactiveSec|OnBootSec|OnStartupSec|\bcrontab\b|\bcron\b|\bsleep\b|setInterval|"
                         r"schedule\.every|(?:^|\s)(?:while|until)\s|\bfor\b[^\n]*\bin\b[^\n]*(?:;|\n)\s*do\b|(?:^|[;&|(\n]\s*)watch\s", re.I)
SCRIPT_EXT = (".timer", ".service", ".sh", ".bash", ".zsh", ".py", ".pl", ".rb", ".js", ".mjs", ".ts", ".cron")


def polling_hosts(tool, inp, rx):
    """True when the call sets up repeated fetching of a host `rx` matches."""
    if rx is None:
        return False
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        if not rx.search(cmd):
            return False
        return bool(BASH_TIMER.search(cmd) or (FETCH.search(cmd) and SHELL_LOOP.search(cmd)))
    if tool == "CronCreate":
        text = str(inp.get("prompt") or "") + " " + str(inp.get("cron") or "")
        return bool(rx.search(text)) and needs_note(tool, inp)
    if tool in ("Write", "Edit", "MultiEdit"):
        path = str(inp.get("file_path") or "")
        base = os.path.basename(path).lower()
        if not (base.endswith(SCRIPT_EXT) or base in ("crontab", "cron")) or base.startswith("test_") or base.endswith(("_test.py", ".test.js", ".test.ts")) \
                or "/tests/" in path or "/test/" in path:
            return False
        parts = [str(inp.get("content") or ""), str(inp.get("new_string") or "")]
        edits = inp.get("edits")
        if isinstance(edits, list):
            parts += [str(e.get("new_string") or "") for e in edits if isinstance(e, dict)]
        text = "\n".join(parts)
        return bool(rx.search(text) and FILE_REPEAT.search(text))
    return False


def needs_note(tool, inp):
    if tool == "CronCreate":
        rec = inp.get("recurring")
        if rec is False or (isinstance(rec, str) and rec.strip().lower() == "false"):
            return False
        if rec is None:
            return not one_shot_cron(inp.get("cron"))
        return True
    if tool == "Skill":
        return bool(LOOP_SKILL.match(str(inp.get("skill") or "").strip().lstrip("/")))
    return False


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(ev, dict):
        return 0
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    tool = str(ev.get("tool_name") or "")
    notes = []
    if needs_note(tool, inp):
        notes.append(NOTE)
    hosts = no_poll_hosts()
    if hosts and polling_hosts(tool, inp, host_regex(hosts)):
        notes.append(no_poll_note(hosts))
    if notes:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": " ".join(notes)}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
