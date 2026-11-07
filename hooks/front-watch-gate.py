#!/usr/bin/env python3
"""front-watch-gate.py - PreToolUse hook with two rules: a front agent does not watch handed-off work itself (front panes only), and
NO agent polls GitHub's check runs faster than once a minute (every agent: the GH API FLOOR below).

RULE 1, front agents only:

The rule: a result the owner asked for reaches them when its board item lands, through stackshot; a front agent does not watch
handed-off work itself (no wake, poll or background check it schedules). If the result must reach the front agent before it lands,
the other end pushes it (the builder's close or message, a server-run `check`); a session watches on its own only when no push
exists, and says why. Why: a self-scheduled watch repeats what the board already delivers, at a time picked for no reason.

In a FRONT agent's pane (main thread only) this hook denies
  - ScheduleWakeup, CronCreate and Monitor, always;
  - Bash with run_in_background true whose command is a wait/poll: a while/until/for loop with a sleep in it (bounded or not:
    wait-loop-guard.py's tokenizer reads the command, so a loop that is only text in an echo/grep/commit argument is not code;
    `bash -c`, `eval`, `$(...)` and the wrappers are looked into), `gh run watch`, `gh pr checks --watch` (any `gh ... --watch`),
    `watch -n ...`, or a leading `sleep N &&`/`sleep N;` before another command. A plain background build or test (no loop, no
    sleep-poll) passes, and so does a foreground Bash.
This rule never denies a background agent, a builder, another project's keeper, a session whose role is unknown, or a subagent (the
hook input carries `agent_id`): the Agent tool is not affected. (The GH API floor below does apply to all of them.)

RULE 2, the GH API FLOOR, EVERY agent: any role, no role entry, a background agent, a builder, a subagent; foreground or background; no
exception. GitHub's API budget is shared: `gh run watch` at its 3 s default costs ~2,400 API calls an hour. A Bash or Monitor command
is denied when, read as code through wait-loop-guard's tokenizer (`bash -c`, `eval`, `$(...)`, the wrappers and `watch ...` are looked
into), it holds
  a. `gh run watch` with no `--interval`/`-i`, or one below 60 (the default is 3 s); or `gh pr checks --watch` with no
     `--interval`/`-i` of 60 or more (the default is 10 s). The forms `--interval 60`, `--interval=60`, `-i 60`, `-i60`, `-i=60` read;
  b. a one-shot read of check runs (`gh run view`, `gh run list`, `gh pr checks` without --watch, `gh workflow view`,
     `gh pr view --json ...statusCheckRollup...`, `gh api .../actions/runs...`, `.../check-runs...`, `.../check-suites...`) inside a while/until loop
     (with or without a sleep: no sleep is worse), inside a for loop WITH a sleep, or under `watch ...`, at any interval. A for loop
     with no sleep, `while read ... < file` with no sleep (finite input) and a single call outside a loop are fan-outs and pass.
The `watch-reason:` exception below does NOT lift the floor. The refusal names the alternatives: the command of the setting
ci_runs_hint, when the setup has one that reads CI results without a GitHub call (default none), then one `gh run watch --interval 60`
run in the background under `timeout`. Log: the same firings.jsonl, rule "gh-api-floor", stop "deny", kind gh-watch-interval |
gh-poll-loop, plus agent_id. The role lookup serves the log only; any error in this rule passes (fails open). A front agent meets
the floor first, then rule 1, unchanged.

EXCEPTION to rule 1 only (never to the floor): `watch-reason: <words>` anywhere in the call's own text lets it through, and the pass is logged. The text read is
ScheduleWakeup `reason` and `prompt`, CronCreate `prompt`, Monitor `description` and `command`, Bash `description` and `command` (as
a comment: `# watch-reason: no push exists, the vendor emails nothing`). The reason is the words after the colon, at least 3
characters. One convention, greppable: `grep -r 'watch-reason:'`.

Who: the same lookup as build-stops.py (its role_entry): the session's sala id from sala's session-registry.json (_sala_identity.py,
optional), the name from the fleet store, the role from stackshot's ~/.local/state/stackshot/roles.json (written from the project
pages' agents: maps); a front agent is a session whose role there is "front". No file, no entry, no name: allow (rule 1 is then off,
and says so in one line on stderr when it would have applied). Env (tests and the probe only): BUILD_STOPS_NAME, BUILD_STOPS_ROLES,
STACKSHOT_FLEET_DIR (build-stops.py's), XDG_STATE_HOME (the log's home).

Log: one JSON line a firing in ~/.local/state/front-watch-gate/firings.jsonl: t (UTC), stop (deny | allowed), session, sala_id, sid,
tool, kind, reason (the exception's words), text (the first 200 characters of the call); the floor's lines add rule and agent_id.

The refusal is a JSON permissionDecision "deny" on stdout with exit 0 and names what to do instead, so the session retries on its
own; it never asks. Any error or unreadable input: silent exit 0 (fails open). Stdlib only.
"""
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _config  # noqa: E402
HOME = os.path.expanduser("~")
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local/state"), "front-watch-gate")
TAG = "(front-watch-gate.py)"

WATCH_TOOLS = ("ScheduleWakeup", "CronCreate", "Monitor")
REASON = re.compile(r"watch-reason:[ \t]*(\S[^\n]{2,})")
INSTEAD = ("Do instead: hand the watch to the work's builder or to its board item: the item's `check=` (a server-run check) or "
           "`wake=` on the item, or a message to the builder asking it to push you the result (its close or a message). The result "
           "reaches the owner when the item lands, through stackshot. If this really is an exception (no push exists), say why in "
           "the call itself with `watch-reason: <words>` (the `reason` field for ScheduleWakeup, the prompt for CronCreate, the "
           "description for Monitor, a `# watch-reason: <words>` comment or the description for Bash) and retry; the pass is logged.")
RULE = ("A front agent does not watch handed-off work itself: a self-scheduled watch repeats what the board "
        "already delivers, at a time picked for no reason.")
KIND_TEXT = {
    "ScheduleWakeup": "ScheduleWakeup is a wake the front agent schedules for itself.",
    "CronCreate": "CronCreate is a timer the front agent schedules for itself.",
    "Monitor": "Monitor is a watch the front agent arms for itself.",
    "Bash": "This background Bash is a wait/poll ({kind}).",
}


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def texts(tool, inp):
    keys = {"ScheduleWakeup": ("reason", "prompt"), "CronCreate": ("prompt",), "Monitor": ("description", "command"),
            "Bash": ("description", "command")}.get(tool, ())
    return "\n".join(str(inp.get(k) or "") for k in keys)


def poll_words(g, text, depth=0):
    """The poll form without a loop that the command text holds as code, '' when none: `gh … watch` / `gh … --watch`, `watch -…`, and
    a `sleep N` that leads the command and is followed by something. Read through wait-loop-guard's tokenizer, so a word inside a
    quoted argument (a commit message, a --body) is not code; `bash -c '…'` and `eval` are looked into."""
    if depth > 4:
        return ""
    segs, cur = [], []
    for t in g.tokenize(text):
        if t.kind == "op":
            segs.append((cur, t.val))
            cur = []
        elif t.kind == "w":
            cur.append(t)
    segs.append((cur, None))
    for k, (words, op) in enumerate(segs):
        if not words:
            continue
        rest, _ = g.unwrap(words)
        if not rest or rest[0].expands:
            continue
        name = os.path.basename(rest[0].val)
        args = [w.val for w in rest[1:] if not w.quoted]
        if name == "gh" and ("--watch" in args or (len(args) > 1 and args[1] == "watch")):
            return "gh watch"
        if name == "watch" and args and args[0].startswith("-"):
            return "watch"
        if name == "sleep" and k == 0 and op in ("&&", ";", "||") and any(x[0] for x in segs[1:]):
            return "sleep, then poll"
        if name in g.SHELLS:
            script = g.Analyzer.shell_script(rest[1:])
            if script is not None and poll_words(g, script, depth + 1):
                return poll_words(g, script, depth + 1)
        elif name == "eval":
            r = poll_words(g, " ".join(w.val for w in rest[1:]), depth + 1)
            if r:
                return r
    return ""


def poll_kind(cmd):
    """What kind of wait/poll a background Bash command is, '' when it is none."""
    try:
        g = load("wait_loop_guard_for_front_watch", "wait-loop-guard.py")
        r = poll_words(g, cmd)
        if r:
            return r
        if re.search(r"\b(?:while|until|for|select)\b", cmd) and "sleep" in cmd and g.Analyzer(cmd, any_loop=True).run():
            return "loop with a sleep"
    except Exception:
        return ""
    return ""


def log(rec):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "firings.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def deny(reason):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                        "permissionDecisionReason": reason}}, ensure_ascii=False))
    return 0


def who_is(ev):
    """(name, sala id, session id, role) of the session the call is from; role is '' when unknown."""
    bs = load("build_stops_for_front_watch", "build-stops.py")
    sid = str(ev.get("session_id") or "").strip() or os.path.basename(str(ev.get("transcript_path") or "")).replace(".jsonl", "")
    row = bs.ident.resolve(sid) if bs.ident is not None and not os.environ.get("BUILD_STOPS_NAME") else None
    sala_id = str(row.get("id") or "").lower() if row else ""
    name = (str(row.get("name") or "") if row else "") or bs.session_name(sid)
    e = bs.role_entry(name, sala_id)
    return name, sala_id, sid, str((e or {}).get("role") or "")


def front_name(ev):
    """(name, sala id, session id) when the call is from a front agent's pane, else None. Role unknown means not front."""
    name, sala_id, sid, role = who_is(ev)
    return (name, sala_id, sid) if role == "front" else None


# ---------------------------------------------------------------------------------------------------------------------------------
# RULE 2, the GH API floor: every agent, no exception. Kept in this file: one hook, one matcher, one log, one tokenizer.
# ---------------------------------------------------------------------------------------------------------------------------------
FLOOR_MIN = 60                      # seconds between polls of GitHub's check runs, at the fastest
FLOOR_TOOLS = {"Bash": "command", "Monitor": "command"}
VALUE_FLAGS = {"-R", "--repo", "-i", "--interval", "-b", "--branch", "-w", "--workflow", "-e", "--event", "-s", "--status", "-L",
               "--limit", "-q", "--jq", "--json", "-t", "--template", "-u", "--user", "-c", "--commit", "-j", "--job", "-H",
               "--header", "-X", "--method", "-f", "--field", "-F", "--raw-field", "--input", "--hostname", "--cache", "-p"}
LEAD_WORDS = {"while", "until", "if", "elif", "then", "else", "do", "!", "{", "(", "time", "coproc", "function"}
API_CHECKS = re.compile(r"actions/runs|check-runs|check-suites")
READ_LOOP = re.compile(r"^\s*(?:while|until)\s+!?\s*(?:IFS=\S*\s+)?(?:read|mapfile|readarray)\b")   # a loop on finite input
FLOOR_RULE = ("GitHub's API budget is shared: a check watch polls the API on every tick, and "
              "`gh run watch` at its 3 s default costs ~2,400 API calls an hour.")


def floor_instead():
    hint = _config.get_str("ci_runs_hint").strip()
    lead = ("Do instead: " + hint + " Fallback: ") if hint else "Do instead: "
    return (lead + "one `timeout 1800 gh run watch <run-id> --interval 60 --exit-status` in the background "
            "(run_in_background true), and take its completion notice. One `gh run view` "
            "outside a loop is fine. No `watch-reason:` lifts this floor.")


FLOOR_TAG = "(front-watch-gate.py GH API floor)"


def positionals(args):
    """The words of a gh argument list that are neither flags nor flag values (group, verb, ids…), as plain strings."""
    out, skip = [], False
    for a in args:
        v = a.val
        if skip:
            skip = False
        elif v.startswith("-") and len(v) > 1:
            skip = v in VALUE_FLAGS
        else:
            out.append(v)
    return out


def interval_of(args):
    """The last `--interval N` / `--interval=N` / `-i N` / `-iN` / `-i=N` of a gh argument list: None when absent, the int, or -1
    when it is there but not a plain number (a variable, a typo: gh decides, the floor cannot)."""
    found, k = None, 0
    while k < len(args):
        v = args[k].val
        if v in ("--interval", "-i"):
            if k + 1 < len(args):
                nxt = args[k + 1]
                found = int(nxt.val) if nxt.val.isdigit() and not nxt.expands else -1
                k += 1
            else:
                found = -1
        elif v.startswith("--interval=") or re.match(r"-i=?\d", v):
            tail = v.split("=", 1)[1] if v.startswith("--interval=") else v[2:].lstrip("=")
            found = int(tail) if tail.isdigit() and not args[k].expands else -1
        k += 1
    return found


def gh_calls(g, text, depth=0, via_watch=False):
    """Every `gh …` the text holds as code: a list of (argument tokens after gh, found under `watch`). Read through wait-loop-guard's
    tokenizer, so gh named inside a quoted argument (a commit message, a --body) is not a call; `bash -c`, `eval`, `$(…)`/backticks
    and `watch …` are looked into."""
    out = []
    if depth > 5:
        return out
    segs, cur = [], []
    for t in g.tokenize(text):
        if t.kind == "op":
            segs.append(cur)
            cur = []
        elif t.kind == "w":
            cur.append(t)
            for sub in t.subs:
                out += gh_calls(g, sub, depth + 1, via_watch)
    segs.append(cur)
    for words in segs:
        while words and not words[0].quoted and words[0].val in LEAD_WORDS:
            words = words[1:]
        rest, _ = g.unwrap(words) if words else ([], False)
        if not rest or rest[0].expands:
            continue
        name, args = os.path.basename(rest[0].val), rest[1:]
        if name == "gh":
            out.append((args, via_watch))
        elif name == "watch":
            for j, a in enumerate(args):
                if os.path.basename(a.val) == "gh" and not a.quoted:
                    out.append((args[j + 1:], True))
                    break
                if " " in a.val:
                    out += gh_calls(g, a.val, depth + 1, True)
        elif name in g.SHELLS:
            script = g.Analyzer.shell_script(args)
            if script is not None:
                out += gh_calls(g, script, depth + 1, via_watch)
        elif name == "eval":
            out += gh_calls(g, " ".join(a.val for a in args), depth + 1, via_watch)
    return out


def gh_watch_fault(args):
    """A text naming the watch that polls faster than the floor, '' when this gh call is none or is slow enough."""
    pos, iv = positionals(args), interval_of(args)
    if pos[:2] == ["run", "watch"]:
        if iv is None:
            return "`gh run watch` with no --interval polls every 3 s"
        if 0 <= iv < FLOOR_MIN:
            return f"`gh run watch --interval {iv}` polls every {iv} s"
    elif pos[:2] == ["pr", "checks"] and any(a.val in ("--watch", "--watch=true", "--watch=1") for a in args):
        if iv is None:
            return "`gh pr checks --watch` with no --interval polls every 10 s"
        if 0 <= iv < FLOOR_MIN:
            return f"`gh pr checks --watch --interval {iv}` polls every {iv} s"
    return ""


def gh_is_check_read(args):
    """True for a one-shot read of GitHub's check runs: gh run view/list, gh pr checks (no --watch), gh workflow view,
    gh pr view --json …statusCheckRollup…, gh api …/actions/runs…|…/check-runs…|…/check-suites…"""
    pos = positionals(args)
    if pos[:2] in (["run", "view"], ["run", "list"], ["pr", "checks"], ["workflow", "view"]):
        return True
    if pos[:2] == ["pr", "view"]:
        for k, a in enumerate(args):
            lst = a.val[7:] if a.val.startswith("--json=") else (args[k + 1].val if a.val == "--json" and k + 1 < len(args) else "")
            if "statusCheckRollup" in lst.split(","):
                return True
    return pos[:1] == ["api"] and any(API_CHECKS.search(a.val) for a in args)


def floor_fault(cmd):
    """(kind, detail) when the command breaks the GH API floor, else None. kind: gh-watch-interval | gh-poll-loop."""
    g = load("wait_loop_guard_for_front_watch", "wait-loop-guard.py")

    class LoopReads(g.Analyzer):
        """wait-loop-guard's Analyzer with one finding for each while/until/for loop that has a sleep and a check-run read in its text."""

        def close_loop(self, text, frames, end, bounded):
            for k in range(len(frames) - 1, -1, -1):
                f = frames[k]
                if f.kind in ("while", "until", "for"):
                    del frames[k:]
                    cond = text[f.start:f.do_start] if f.do_start else ""
                    # a while/until is a poll with or without a sleep; a for needs the sleep (without one it is a fan-out);
                    # a while on finite input (`while read …`) with no sleep is a fan-out too
                    if f.sleep or (f.kind != "for" and not READ_LOOP.search(cond)):
                        body = text[f.start:end]
                        if any(gh_is_check_read(a) for a, _ in gh_calls(g, body)):
                            self.add("gh-poll-loop", re.sub(r"\s+", " ", body)[:100])
                    return

    calls = gh_calls(g, cmd)
    for args, _ in calls:
        fault = gh_watch_fault(args)
        if fault:
            return "gh-watch-interval", fault
    for args, via_watch in calls:
        if via_watch and gh_is_check_read(args):
            return "gh-poll-loop", "A check-run read under `watch`"
    if re.search(r"\b(?:while|until|for|select)\b", cmd) and any(gh_is_check_read(a) for a, _ in calls):
        found = LoopReads(cmd, any_loop=True).run()
        if found:
            return "gh-poll-loop", "A polling loop around a check-run read (" + found[0][1] + ")"
    return None


def gh_api_floor(ev, tool, inp):
    """The GH API floor on a Bash or Monitor call: prints the deny and returns its exit code, or None to carry on. Fails open."""
    try:
        key = FLOOR_TOOLS.get(tool)
        cmd = str(inp.get(key) or "") if key else ""
        if not cmd or not re.search(r"\bgh\b", cmd):        # the fast path: no gh in the text, no floor
            return None
        fault = floor_fault(cmd)
        if fault is None:
            return None
        kind, detail = fault
        name = sala_id = ""
        sid = str(ev.get("session_id") or "")
        try:
            name, sala_id, sid, _role = who_is(ev)
        except Exception:
            pass
        log({"stop": "deny", "rule": "gh-api-floor", "session": name, "sala_id": sala_id, "sid": sid, "tool": tool, "kind": kind,
             "agent_id": str(ev.get("agent_id") or ""), "text": cmd[:200]})
        if kind == "gh-watch-interval":
            return deny(f"{detail}; the floor is one poll a minute (--interval 60 or more). {FLOOR_RULE} {floor_instead()} {FLOOR_TAG}")
        return deny(f"{detail}: a check-run poll in a loop is refused at any interval. {FLOOR_RULE} {floor_instead()} {FLOOR_TAG}")
    except Exception:
        return None


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(ev, dict):
        return 0
    tool = str(ev.get("tool_name") or "")
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    floor = gh_api_floor(ev, tool, inp)                     # rule 2, every agent (a subagent too), before rule 1
    if floor is not None:
        return floor
    if ev.get("agent_id"):                                  # rule 1 never touches a subagent
        return 0
    if tool == "Bash":
        if inp.get("run_in_background") is not True:        # the fast path: a foreground Bash is no watch
            return 0
        kind = poll_kind(str(inp.get("command") or ""))
        if not kind:
            return 0
    elif tool in WATCH_TOOLS:
        kind = tool
    else:
        return 0
    bs = load("build_stops_for_front_watch", "build-stops.py")
    if not os.path.isfile(bs.ROLES):
        _config.note("front-watch-gate", "no stackshot roles file; the front agents' watch rule is off (the GitHub API floor still applies)")
        return 0
    who = front_name(ev)
    if who is None:
        return 0
    name, sala_id, sid = who
    text = texts(tool, inp)
    base = {"session": name, "sala_id": sala_id, "sid": sid, "tool": tool, "kind": kind,
            "text": (str(inp.get("command") or inp.get("prompt") or inp.get("reason") or inp.get("description") or ""))[:200]}
    m = REASON.search(text)
    if m:
        log({"stop": "allowed", **base, "reason": m.group(1).strip()[:200]})
        return 0
    log({"stop": "deny", **base})
    return deny(f"{name} is a front agent. {KIND_TEXT.get(tool, '').format(kind=kind)} {RULE} {INSTEAD} {TAG}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
