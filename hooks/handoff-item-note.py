#!/usr/bin/env python3
"""handoff-item-note.py - PreToolUse hook: a NOTE when a front agent hands its project's builder a message that names no item.

Never a block, never an ask: it prints hookSpecificOutput.additionalContext (no permissionDecision, so the call goes on as it would
have) and exits 0. The note is one line:

    Hand-off with no item: file the item first and hand it over by id, with what done means.

It fires when ALL of these hold, and is silent in every other case (and on any doubt):

  1. The call is a message to a session: the native SendMessage (`to`, `message`), or stackshot's message tool (`to`, `text`, `sender`,
     `fyi`) by either route: the direct connector tool, or a portal Code Mode script (`portal_codemode_execute`) with
     `codemode.<entry>_message({...})` inside. hook_calls.py reads which route and which tool, as in the other hooks. A portal call
     is read only when `to` and `text` are plain string literals in its object (a variable or a shorthand cannot be read: silent).
  2. `fyi` is not true (a message that asks nothing is not a hand-off).
  3. The sender is a front agent: the session behind the hook call, by the same lookup as build-stops.py (sala's session registry,
     then the fleet store, then Claude's own session files; the role from stackshot's roles.json). A background agent, an unknown
     session or a missing roles file: silent. The `sender` argument of the message is never trusted.
  4. The recipient is a background agent of the SAME project, not retired, found in roles.json by name (or by sala id), and it is
     the project's builder when that can be told: the `build_stops` setting names a project's `builder` (and optionally
     `builder_id`), and the recipient must be that one. When the project has no builder in that setting (or is not in it) the
     builder cannot be told apart, and then ANY background agent of the project counts. (stackshot's roles.json carries a role per
     session, not its mandate, so the builder is known only from the setting.) A project address (`projects/<slug>`) or a front
     agent is not a recipient here.
  5. The message names no item: none of the patterns of the setting `item_ref_patterns` matches the text (for a portal call, the
     call's whole argument text).

Setting `item_ref_patterns` (env STACKSHOT_PLUGIN_ITEM_REF_PATTERNS; a JSON list or a comma-separated list of regular expressions;
case-insensitive): how an item reference is written on this board. Default: a word, an optional dash-joined name, `#` and digits
(`<board>#<n>`), and an `items/<n>` path of a board link. An invalid expression is skipped.

One note per tool call, however many messages a portal script holds. Without stackshot's roles file (or a session it can name)
there is no front agent to tell, so the hook is silent and reads nothing else (it writes no line of its own on stderr: it runs on
every message).
Any error or unreadable input: silent exit 0. Stdlib only, no model call.
"""
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _config  # noqa: E402

TAG = "(handoff-item-note.py)"
NOTE = "Hand-off with no item: file the item first and hand it over by id, with what done means. " + TAG
DEFAULT_ITEM_REFS = (r"\b[a-z][\w-]*#\d+", r"/items/[\w-]*\d")

STR = r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`)"""
KEY_TO = re.compile(r"""\bto\s*:\s*""" + STR)
KEY_TEXT = re.compile(r"""\btext\s*:\s*""" + STR)
KEY_FYI = re.compile(r"""\bfyi\s*:\s*true\b""")


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def item_ref_regexes():
    raw = _config.get_list("item_ref_patterns", DEFAULT_ITEM_REFS)
    out = []
    for p in raw:
        try:
            out.append(re.compile(p, re.I))
        except re.error:
            continue
    return out or [re.compile(p, re.I) for p in DEFAULT_ITEM_REFS]


def strings(o, skip=(), depth=0):
    if depth > 8:
        return
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            if k not in skip:
                yield from strings(v, skip, depth + 1)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v, skip, depth + 1)


def literal(m):
    return next((g for g in m.groups() if g is not None), "")


def messages(ev):
    """[(to, text_to_scan)] of the messages this tool call sends that this hook can read; fyi messages and unreadable ones left out."""
    tool = str(ev.get("tool_name") or "")
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    if tool == "SendMessage":
        to = str(inp.get("to") or inp.get("recipient") or "")
        return [(to, "\n".join(strings(inp, skip=("to", "recipient"))))] if to else []
    if not tool.startswith("mcp__"):
        return []
    import hook_calls
    calls = [c for c in hook_calls.calls_of(tool, inp, ev.get("mcp_server")) if c[0] == "stackshot" and c[2] == "message"]
    if not calls:
        return []
    if "code" not in inp:                                   # the direct connector tool
        if inp.get("fyi") is True:
            return []
        to = str(inp.get("to") or "")
        return [(to, "\n".join(strings(inp, skip=("to", "sender"))))] if to else []
    servers = {c[1] for c in calls}
    code = str(inp.get("code") or "")
    found = list(hook_calls.CALL.finditer(code))
    out = []
    for i, m in enumerate(found):
        ident = m.group(1)
        if not ident.endswith("_message") or ident[:-len("_message")] not in servers:
            continue
        seg = code[m.end():found[i + 1].start() if i + 1 < len(found) else len(code)]
        to_m, text_m = KEY_TO.search(seg), KEY_TEXT.search(seg)
        if not to_m or not text_m or KEY_FYI.search(seg):
            continue                                        # not plain literals, or an fyi: cannot tell, so silent
        out.append((literal(to_m), seg))
    return out


def who_is(bs, ev):
    """(name, sala id, role entry) of the session the call is from."""
    sid = str(ev.get("session_id") or "").strip() or os.path.basename(str(ev.get("transcript_path") or "")).replace(".jsonl", "")
    row = bs.ident.resolve(sid) if bs.ident is not None and not os.environ.get("BUILD_STOPS_NAME") else None
    sala_id = str(row.get("id") or "").lower() if row else ""
    name = (str(row.get("name") or "") if row else "") or bs.session_name(sid)
    return name, sala_id, bs.role_entry(name, sala_id)


def find_recipient(bs, to):
    """(name, sala id, roles entry) of the session `to` names (a name, or a sala id the roles file maps); None when it cannot be told."""
    try:
        d = json.load(open(bs.ROLES, encoding="utf-8"))
    except Exception:
        return None
    sessions = d.get("sessions") if isinstance(d.get("sessions"), dict) else {}
    ids = d.get("ids") if isinstance(d.get("ids"), dict) else {}
    to = to.strip().lstrip("@")
    if isinstance(sessions.get(to), dict):
        return to, str(ids.get(to) or "").lower(), sessions[to]
    names = [n for n, i in ids.items() if str(i).lower() == to.lower() and isinstance(sessions.get(n), dict) and not sessions[n].get("retired")]
    entries = {(sessions[n].get("role"), sessions[n].get("project")) for n in names}
    if names and len(entries) == 1:
        return names[0], to.lower(), sessions[names[0]]
    return None


def same_project(bs, a, b):
    a, b = str(a or "").strip().strip("/"), str(b or "").strip().strip("/")
    if not a or not b:
        return False
    if a == b:
        return True
    pa = bs.project_id(a)
    return bool(pa) and pa == bs.project_id(b)


def is_builder(bs, project, name, sala_id):
    """True / False when the project's builder is named in the `build_stops` setting, None when it cannot be told."""
    pid = bs.project_id(project)
    p = bs.PROJECTS.get(pid) if pid else None
    if not p or not (p["builder"] or p["builder_id"]):
        return None
    if p["builder_id"] and sala_id and p["builder_id"].lower() == sala_id:
        return True
    return name in {n for n in (p["builder"], bs.builder_name(pid)) if n}


def may_send(ev):
    """The fast path: False for a call that cannot be a message (no lookup is made for it)."""
    tool = str(ev.get("tool_name") or "")
    if tool == "SendMessage":
        return True
    if not tool.startswith("mcp__"):
        return False
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    return tool.endswith("message") or ("portal_codemode_execute" in tool and "message" in str(inp.get("code") or ""))


def decide(ev):
    """The note's text when the call is a front agent's hand-off to its builder with no item named, else ''."""
    if not may_send(ev):
        return ""
    bs = load("build_stops_for_handoff", "build-stops.py")
    if not os.path.isfile(bs.ROLES):
        return ""
    name, sala_id, me = who_is(bs, ev)
    if not isinstance(me, dict) or me.get("role") != "front":
        return ""
    msgs = messages(ev)
    refs = item_ref_regexes()
    for to, text in msgs:
        rcpt = find_recipient(bs, to)
        if rcpt is None:
            continue
        r_name, r_id, r = rcpt
        if r.get("role") != "back" or r.get("retired") or not same_project(bs, me.get("project"), r.get("project")):
            continue
        if is_builder(bs, me.get("project"), r_name, r_id) is False:
            continue
        if any(rx.search(text) for rx in refs):
            continue
        return NOTE
    return ""


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(ev, dict):
        return 0
    note = decide(ev)
    if note:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
