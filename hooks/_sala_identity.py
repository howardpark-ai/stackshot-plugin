"""_sala_identity.py - which sala session is this hook call from, shared by build-stops.py and front-watch-gate.py. sala is optional:
without it there is no registry and every answer is "cannot tell".

A session's name changes (`sala rename`); its sala id never does. sala publishes the registry of ids on every box as
<fleet>/<letter>/session-registry.json (schema 1: rows keyed by sala id with name, aliases, state, transcripts[]; see salamander's
docs/session-registry.md and stackshot's sessions_registry.py, which this is the small hook-side cut of). This module reads that
file directly (no subprocess, no network: a hook runs on every tool call) and answers two questions:

  resolve(session_id)   the live registry row of the session that made this hook call, or None = "cannot tell".
  current_name(sala_id) the name that sala id goes by now ("" when the registry does not know it).

The rule with sala: a missing, unreadable, ambiguous or non-matching registry means "cannot tell", never a guess. Every caller then
keeps its name-based logic. Nothing here raises; every failure is None / "" / {}.

How a call is resolved, in this order:
  1. the hook input's session_id (the Claude transcript UUID) is listed in exactly one live row's transcripts[]: that row. Several
     live rows list it (a fork or an import): ambiguous, unless $SALA_SESSION_ID is one of them.
  2. no row lists it yet (the conversation is newer than the last heartbeat): $SALA_SESSION_ID, the id sala gives the pane at spawn,
     when it names a live row. Not used in a headless `claude -p` (CLAUDE_CODE_ENTRYPOINT=sdk-cli), which inherits the pane's
     environment and is not the session.
  3. otherwise None.

Env: SALA_HEARTBEAT_STATE_DIR, the store the registry sits in (sala's own variable; default ~/.local/state/salamander/fleet)."""
import glob
import json
import os

STORE_ENV = "SALA_HEARTBEAT_STATE_DIR"
DEFAULT_STORE = ".local/state/salamander/fleet"


def _store():
    return os.environ.get(STORE_ENV) or os.path.join(os.path.expanduser("~"), DEFAULT_STORE)


def registry_rows():
    """{sala id: row} from every <store>/<letter>/session-registry.json that reads and is schema 1; {} when there is none."""
    rows = {}
    for f in sorted(glob.glob(os.path.join(_store(), "*", "session-registry.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            if not isinstance(d, dict) or d.get("schema") != 1 or not isinstance(d.get("sessions"), dict):
                continue
            for k, r in d["sessions"].items():
                if not isinstance(r, dict):
                    continue
                i = str(r.get("id") or k).strip().lower()
                if i and i not in rows:
                    rows[i] = r
        except Exception:
            continue
    return rows


def _live(r):
    return r.get("state") == "live"


def _listed(r, sid):
    return any(isinstance(t, dict) and str(t.get("id") or "").lower() == sid for t in (r.get("transcripts") or []))


def resolve(session_id, rows=None):
    """The live registry row of the session behind `session_id`, or None when that cannot be told (see the module docstring)."""
    try:
        rows = registry_rows() if rows is None else rows
        sid = str(session_id or "").strip().lower()
        env_id = "" if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-cli" else os.environ.get("SALA_SESSION_ID", "").strip().lower()
        if not rows:
            return None
        hits = [i for i, r in rows.items() if sid and _live(r) and _listed(r, sid)]
        if len(hits) == 1:
            return rows[hits[0]]
        if len(hits) > 1:
            return rows[env_id] if env_id in hits else None
        r = rows.get(env_id) if env_id else None
        return r if r is not None and _live(r) else None
    except Exception:
        return None


def current_name(sala_id, rows=None):
    """The name the live session `sala_id` goes by now; '' when the registry does not know it (or it is retired)."""
    try:
        r = (registry_rows() if rows is None else rows).get(str(sala_id or "").strip().lower())
        return str(r.get("name") or "") if r is not None and _live(r) else ""
    except Exception:
        return ""
