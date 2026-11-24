#!/usr/bin/env python3
"""no-ask-watch.py - Stop hook: records a front agent's turn that nobody asked for and that ended with nothing for the owner.

Each such turn is a leak: something reached a front agent's model that did not need to (a peer note, a task notification, a router
notice), and it answered it with a sign-off for the owner ("Nothing here needs you"). The hook posts one record per catch to
stackshot, which counts them and lets whoever fixes the source trace the turn.

A turn is recorded when ALL of these hold; any other case, any failure and any doubt passes (silent exit 0, no output, never blocks):
  1. the pane is a front agent on the terminal surface: its role in stackshot's roles file (build-stops.py's `role_entry`, the lookup
     front-watch-gate.py uses too) is `front`, with a `surface` that is absent or `sala`, and the file is under an hour old. Not a name
     suffix. A hook input with `agent_id` (a subagent) or `stop_hook_active` passes;
  2. the final reply's first 400 characters hold one of the phrases (case-insensitive) and the reply holds no question mark;
  3. the turn had no human entry. A turn is the transcript after the last `system/turn_duration`. Every user entry and every
     `queued_command` attachment carries `origin.kind`: `human` is the owner (also a message queued mid-turn); `peer`,
     `task-notification`, `scheduled-trigger`, `projects-relay`, `slack-ping`, `bridge` are triggers; `plugin` and compaction
     continuations (`isCompactSummary`, the "This session is being continued" text) are ignored; no kind, or a kind not listed,
     counts as the owner. At least one trigger is needed. A transcript read from its tail whose turn start is not in the window
     passes (the human start may lie before it).

The phrases are the plugin setting `no_ask_phrases` (a list, or a comma-separated string in the environment; default: "Nothing here
needs you", "Nothing new for you", "There's nothing you need to do", "Nothing needs you"). A phrase that holds a comma goes in as a
JSON list.

Report: `python3 <stackshot>/.../client.py post catch -` (the `-` makes the client read the JSON body on stdin) with this body, where the client is found through
_config.stackshot_module_path("client") (the stackshot checkout, STACKSHOT_DIR):
    {"hook": "no-ask-watch", "id": sha1("t|pane|sender|kind"), "t": <UTC, as in the log>, "pane", "kind", "sender", "outcome":
     "sent" | "folded", "first": <the first 200 characters of the sign-off, long tokens masked>}
Every catch is posted, a repeat within the hour (outcome `folded`) included: the server dedupes on `id`. With no stackshot checkout
the hook says so on stderr (one line per catch, as each firing is its own process) and keeps the local log only; `--replay` posts the log later.
The hook queues the body in pending.jsonl and starts a detached `--flush` of its own, which posts it and removes it on success; a
failed post stays in pending.jsonl (back-off 5 minutes) and the next firing of any front pane retries it. Nothing is dropped.

`--replay [file]` re-posts the `sent` and `folded` lines of a log file (default: the firings.jsonl below). The lines carry no sign-off
text, so `first` is empty. The server dedupes on `id`, so a replay is idempotent. A line within 5 seconds of the last posted line of
the same pane, sender and kind is skipped as a duplicate firing. Prints one summary line; exit 1 when a post failed.

Dedupe state: key = pane + sender (a task notification's sender is "task-notification"). A repeat within an hour is logged `folded`,
not `sent`. State: ${XDG_STATE_HOME:-~/.local/state}/no-ask-watch/state.json, written atomically (temp file in the same folder,
os.replace) under flock. Keys older than a day are pruned.

Log: one JSON line a firing in firings.jsonl beside it: t (UTC), pane, kind, sender, outcome: sent | folded | send-failed |
pass-human | pass-no-trigger | pass-question | pass-error. Only a reply that carries a no-ask phrase is logged.

Stdlib only. Alarm at 8 s (the hook's own limit is 10 s). Env (tests and the probe only): BUILD_STOPS_NAME, BUILD_STOPS_ROLES
(build-stops.py's), XDG_STATE_HOME, NO_ASK_WATCH_SYNC=1 (run the flush in the hook's own process instead of detached).
"""
import calendar
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _config  # noqa: E402

HOOK = "no-ask-watch"
HOME = os.path.expanduser("~")
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local/state"), HOOK)
LOG_FILE = os.path.join(STATE, "firings.jsonl")

DEFAULT_PHRASES = ("Nothing here needs you", "Nothing new for you", "There's nothing you need to do", "Nothing needs you")
NOASK_WINDOW = 400                       # the phrase is looked for in the first 400 characters, case-insensitive
TRIGGER_KINDS = ("peer", "task-notification", "scheduled-trigger", "projects-relay", "slack-ping", "bridge")
DEDUPE_S = 3600.0
PRUNE_S = 86400.0
ROLES_MAX_AGE_S = 3600.0
RETRY_S = 300.0
PENDING_CAP = 100
TAIL_BYTES = 4 * 1024 * 1024
FIRST_CHARS = 200
ALARM_S = 8
FLUSH_ALARM_S = 900
REPLAY_ALARM_S = 3600
POST_TIMEOUT_S = 150
REPLAY_POST_TIMEOUT_S = 30
REPLAY_GIVE_UP = 3                       # consecutive failed posts after which a replay stops
REPLAY_DUP_S = 5
TOKEN = re.compile(r"[A-Za-z0-9_\-]{32,}")
COMPACT_TEXT = ("This session is being continued", "<tools-used-before-summary>", "<artifact-content-authored-by-others/>")
T_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def phrases():
    """The no-ask phrases: the setting `no_ask_phrases`, else the defaults; empty entries dropped (an empty phrase would match every reply)."""
    return [p for p in _config.get_list("no_ask_phrases", DEFAULT_PHRASES) if p.strip()]


# --- who ---------------------------------------------------------------------------------------------------------------
def pane_is_front(ev):
    """(pane name, True) when the session is a front agent in a roles file under an hour old, else (name or '', False). Same lookup as front-watch-gate."""
    bs = load("build_stops_for_no_ask_watch", "build-stops.py")
    sid = str(ev.get("session_id") or "").strip() or os.path.basename(str(ev.get("transcript_path") or "")).replace(".jsonl", "")
    row = bs.ident.resolve(sid) if bs.ident is not None and not os.environ.get("BUILD_STOPS_NAME") else None
    sala_id = str(row.get("id") or "").lower() if row else ""
    name = (str(row.get("name") or "") if row else "") or bs.session_name(sid)
    if not name:
        return "", False
    e = bs.role_entry(name, sala_id)
    if str((e or {}).get("role") or "") != "front":
        return name, False
    if str(e.get("surface") or "sala") != "sala":       # a front agent on another surface (a chat app) answers there, not in a terminal pane; absent = terminal
        return name, False
    try:
        gen = json.load(open(bs.ROLES, encoding="utf-8")).get("generated")
        t = datetime.strptime(str(gen)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return name, False
    return name, abs(time.time() - t) <= ROLES_MAX_AGE_S


# --- the transcript ----------------------------------------------------------------------------------------------------
def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def read_turn(path):
    """(entries, last_text): the current turn's user entries and queued_command attachments as (kind, text, origin) with kind 'human' | a trigger
    kind | 'ignore', and the last assistant text of the turn. None when the turn's start cannot be seen."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
        raw = f.read()
    lines = raw.decode("utf-8", "replace").splitlines()
    truncated = size > TAIL_BYTES
    if truncated:
        lines = lines[1:]
    cur, prev, last_cur, last_prev = [], [], "", ""
    boundary = not truncated          # a turn start is seen when the file is whole or a turn_duration came before the entries
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        ty = e.get("type")
        if ty == "system" and e.get("subtype") == "turn_duration":
            prev, last_prev = cur, last_cur
            cur, last_cur = [], ""
            boundary = True
        elif ty == "user":
            c = (e.get("message") or {}).get("content")
            if isinstance(c, list) and c and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                continue
            cur.append(classify(e.get("origin"), text_of(c), e))
        elif ty == "attachment":
            a = e.get("attachment") or {}
            if a.get("type") == "queued_command":
                cur.append(classify(a.get("origin") or e.get("origin"), str(a.get("prompt") or ""), a))
        elif ty == "assistant":
            t = text_of((e.get("message") or {}).get("content"))
            if t.strip():
                last_cur = t
    if not cur:                       # the turn's own turn_duration is already written: the turn is the one before it
        cur, last_cur = prev, last_prev
    if not boundary:
        return None
    return cur, last_cur


def classify(origin, text, rec):
    kind = (origin or {}).get("kind") if isinstance(origin, dict) else None
    if kind == "plugin":
        return ("ignore", text, origin)
    if not kind and (rec.get("isCompactSummary") or text.lstrip().startswith(COMPACT_TEXT)):
        return ("ignore", text, origin)
    if kind in TRIGGER_KINDS:
        return (kind, text, origin)
    return ("human", text, origin)          # human, no kind, an unknown kind: the owner's (doubt passes)


def sender_of(kind, text, origin):
    name = (origin or {}).get("name") if isinstance(origin, dict) else None
    m = None if name else (re.search(r'from-name="([^"]*)"', text) or re.search(r"^from:\s*(.+)$", text, re.M))
    s = str(name or (m.group(1) if m else "") or "")
    s = "".join(c for c in s if c.isprintable() or c.isspace())   # drops ESC, zero-width and bidi marks, any other control: the server refuses a sender that is not printable
    s = " ".join(s.split())
    if not s or s.lower() == "unknown":
        s = kind
    return s[:80]


def first_of(sign_off):
    """The first 200 characters of the sign-off, with long tokens (a key, a hash) masked."""
    return TOKEN.sub("<token>", sign_off)[:FIRST_CHARS]


def catch_id(t, pane, sender, kind):
    """The record's identity: the same catch gets the same id whether the hook posts it or a replay does, so the server dedupes on it."""
    return hashlib.sha1(f"{t}|{pane}|{sender}|{kind}".encode("utf-8")).hexdigest()


def body_of(t, pane, kind, sender, outcome, first):
    return {"hook": HOOK, "id": catch_id(t, pane, sender, kind), "t": t, "pane": pane, "kind": kind, "sender": sender,
            "outcome": outcome, "first": first}


# --- state -------------------------------------------------------------------------------------------------------------
def ensure_state():
    os.makedirs(STATE, exist_ok=True)


class Lock:
    """flock on a file beside the state; blocking unless `wait` is False (then __enter__ returns False when held)."""
    def __init__(self, name, wait=True):
        self.path, self.wait, self.fd = os.path.join(STATE, name), wait, None

    def __enter__(self):
        ensure_state()
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False
        return True

    def __exit__(self, *a):
        if self.fd is not None:
            os.close(self.fd)          # closing releases the flock


def load_state():
    try:
        d = json.load(open(os.path.join(STATE, "state.json"), encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("keys"), dict):
            return d
    except Exception:
        pass
    return {"keys": {}}


def atomic_write(name, text):
    ensure_state()
    final = os.path.join(STATE, name)
    tmp = f"{final}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(st):
    atomic_write("state.json", json.dumps(st, ensure_ascii=False))


def read_pending():
    out = []
    try:
        for ln in open(os.path.join(STATE, "pending.jsonl"), encoding="utf-8"):
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def write_pending(rows):
    atomic_write("pending.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[-PENDING_CAP:]))


def now_t():
    return time.strftime(T_FORMAT, time.gmtime())


def log(pane, kind, sender, outcome, t=None):
    """Append one firing to the log; returns its `t`."""
    t = t or now_t()
    try:
        ensure_state()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": t, "pane": pane, "kind": kind, "sender": sender, "outcome": outcome}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return t


# --- report ------------------------------------------------------------------------------------------------------------
def client_path():
    """stackshot's client.py in the checkout, or '' when there is none."""
    return _config.stackshot_module_path("client")


def post(client, body, timeout):
    """One `client.py post catch -` (the `-` makes the client read the body from stdin); True when it exited 0."""
    try:
        p = subprocess.run([sys.executable, client, "post", "catch", "-"], input=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, timeout=timeout)
        return p.returncode == 0
    except Exception:
        return False


def flush():
    """Post what pending.jsonl holds and is due; a posted row is removed, a failed one stays with its back-off. One flusher at a time."""
    client = client_path()
    if not client:
        return
    with Lock("flush.lock", wait=False) as got:
        if not got:
            return
        with Lock("state.lock"):
            rows = read_pending()
        now = time.time()
        done = set()
        failed = {}
        for r in rows:
            body = r.get("body")
            if not isinstance(body, dict) or float(r.get("next_at") or 0) > now:
                continue
            if post(client, body, POST_TIMEOUT_S):
                done.add(r.get("id"))
            else:
                failed[r.get("id")] = now + RETRY_S
                log(body.get("pane", ""), body.get("kind", ""), body.get("sender", ""), "send-failed")
        with Lock("state.lock"):
            keep = []
            for r in read_pending():          # re-read: a firing may have queued more meanwhile
                if not isinstance(r.get("body"), dict):
                    continue                  # a row of the earlier message-based hook: its firing is in the log, `--replay` posts it
                if r.get("id") in done:
                    continue
                if r.get("id") in failed:
                    r["next_at"] = failed[r["id"]]
                keep.append(r)
            write_pending(keep)


def start_flush():
    if os.environ.get("NO_ASK_WATCH_SYNC") == "1":
        flush()
        return
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--flush"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def replay(path):
    """Re-post the `sent` and `folded` lines of a log file. Returns the exit code."""
    client = client_path()
    if not client:
        sys.stderr.write(f"{HOOK}: no stackshot checkout (set STACKSHOT_DIR): nothing to post to\n")
        return 1
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError as e:
        sys.stderr.write(f"{HOOK}: cannot read {path}: {e.strerror}\n")
        return 1
    last_posted = {}                       # (pane, sender, kind) -> epoch of the last line taken
    taken = posted = dup = failed = streak = 0
    for ln in lines:
        try:
            d = json.loads(ln)
            t, pane, kind, sender, outcome = (d[k] for k in ("t", "pane", "kind", "sender", "outcome"))
            ts = calendar.timegm(time.strptime(t, T_FORMAT))
        except (ValueError, KeyError, TypeError):
            continue
        if outcome not in ("sent", "folded") or not all(isinstance(x, str) for x in (pane, kind, sender)):
            continue
        key = (pane, sender, kind)
        if key in last_posted and abs(ts - last_posted[key]) <= REPLAY_DUP_S:
            dup += 1
            continue
        last_posted[key] = ts
        taken += 1
        if post(client, body_of(t, pane, kind, sender, outcome, ""), REPLAY_POST_TIMEOUT_S):
            posted += 1
            streak = 0
        else:
            failed += 1
            streak += 1
            if streak >= REPLAY_GIVE_UP:
                break
    print(f"{HOOK}: replay of {path}: {posted} posted, {dup} skipped as duplicates, {failed} failed"
          + (" (stopped after repeated failures)" if streak >= REPLAY_GIVE_UP else ""))
    return 1 if failed else 0


# --- the hook ----------------------------------------------------------------------------------------------------------
def noask(text):
    head = text[:NOASK_WINDOW].replace("’", "'").lower()
    return any(p.replace("’", "'").lower() in head for p in phrases())


def run(ev):
    if ev.get("stop_hook_active") or ev.get("agent_id"):
        return
    pane, front = pane_is_front(ev)
    if not front:
        return
    with Lock("state.lock"):
        due = any(float(r.get("next_at") or 0) <= time.time() for r in read_pending())
    if due:
        start_flush()                      # a failed post is retried at the next firing of any front pane
    last = ev.get("last_assistant_message")
    last = last if isinstance(last, str) and last.strip() else ""
    if last and not noask(last):
        return                             # the reply holds no phrase: the transcript is not read
    path = str(ev.get("transcript_path") or "")
    turn = None
    try:
        turn = read_turn(path)
    except OSError:
        turn = None
    if not last and turn:
        last = turn[1]
    if not last or not noask(last):
        return
    if "?" in last or "？" in last:
        log(pane, "", "", "pass-question")
        return
    if turn is None:
        return
    entries = turn[0]
    if any(k == "human" for k, _t, _o in entries):
        log(pane, "", "", "pass-human")
        return
    trig = next(((k, t, o) for k, t, o in entries if k != "ignore"), None)
    if trig is None:
        log(pane, "", "", "pass-no-trigger")
        return
    kind, text, origin = trig
    sender = sender_of(kind, text, origin)
    client = client_path()
    if not client:
        _config.note(HOOK, "no stackshot checkout (set STACKSHOT_DIR): the catch is kept in the local log only; `no-ask-watch.py --replay` posts it later")
    now = time.time()
    key = f"{pane}|{sender}"
    with Lock("state.lock"):
        st = load_state()
        keys = st["keys"]
        for k in [k for k, v in keys.items() if not isinstance(v, dict) or now - float(v.get("last_sent") or 0) > PRUNE_S]:
            del keys[k]
        cur = keys.get(key)
        if cur and now - float(cur.get("last_sent") or 0) < DEDUPE_S:
            cur["folded"] = int(cur.get("folded") or 0) + 1
            outcome = "folded"
        else:
            keys[key] = {"last_sent": now, "folded": 0}
            outcome = "sent"
        t = now_t()
        body = body_of(t, pane, kind, sender, outcome, first_of(last))
        if client:
            rows = read_pending()
            if not any(r.get("id") == body["id"] for r in rows):
                rows.append({"id": body["id"], "next_at": 0, "body": body})
                write_pending(rows)       # queued before the state says sent: a crash between the two loses nothing
        save_state(st)
        log(pane, kind, sender, outcome, t)
    if client:
        start_flush()


def main():
    signal.signal(signal.SIGALRM, lambda *a: os._exit(0))
    args = sys.argv[1:]
    if "--replay" in args:
        i = args.index("--replay")
        path = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else LOG_FILE
        signal.alarm(REPLAY_ALARM_S)
        try:
            return replay(path)
        except BaseException:
            return 1
    flushing = "--flush" in args
    signal.alarm(FLUSH_ALARM_S if flushing else ALARM_S)       # the detached flush waits for the client (up to 150 s a post)
    try:
        if flushing:
            flush()
            return 0
        ev = json.loads(sys.stdin.read() or "{}")
        if isinstance(ev, dict):
            run(ev)
    except BaseException:
        pass                               # fails open: never blocks, never prints
    return 0


if __name__ == "__main__":
    sys.exit(main())
