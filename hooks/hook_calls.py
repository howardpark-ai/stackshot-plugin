"""hook_calls.py - which MCP server and tool a hook's tool call reaches, by the direct connector or inside a portal Code Mode script.

A small shared helper for the hooks that note connector calls (post-tool-note.py and the like): every rule about connector calls
must hold for the same call made through an MCP portal (`portal_codemode_execute`, `codemode.<entry>_<tool>(...)` in its JavaScript).

    calls_of(tool_name, tool_input, mcp_server=None)  ->  [(kind, server, tool)]

  kind    "workspace" | "slack" | "github" | "gbrain" | "stackshot" | "notion" | "other"
  server  the catalog id when stackshot's connectors.py resolved it (gbrain, github ...), else the best name available
          (the entry id split off a codemode name, or the `mcp__<server>__` part of a direct tool name)
  tool    the upstream tool name (send_gmail_message, create_pull_request ...)

stackshot's `connectors.py` and its catalog (STACKSHOT_DIR) are used when the box has them; without them the names come from the
tool name (direct) or from a regex over the script (portal), split at the entry ids of the setting connector_entries (default
stackshot, gbrain, github, notion, slack; a portal's own entry ids go there). The setting workspace_ids lists the entry ids that
are Google Workspace connectors (default none; a catalog entry whose name says "workspace" counts without it).
Nothing here decides anything: callers note, they never block on this. Any error gives an empty list. Stdlib only.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402

EXECUTE = re.compile(r"portal_codemode_execute$")
# only used when the catalog does not name the entry (a box without connectors.py, or an entry the portal sync has not added yet)
DEFAULT_ENTRIES = ("stackshot", "gbrain", "github", "notion", "slack")


def fallback_entries():
    return tuple(_config.get_list("connector_entries", DEFAULT_ENTRIES))


def workspace_ids():
    return set(_config.get_list("workspace_ids"))


CALL = re.compile(r"(?<![\w$.])codemode\s*\.\s*([A-Za-z_$][\w$]*)\s*\(")


def _catalog(conn):
    try:
        return conn.load_catalog() or {}
    except Exception:
        return {}


def kind_of(server, catalog=None):
    s = str(server or "").lower()
    entry = ((catalog or {}).get("servers") or {}).get(server) or {}
    text = " ".join([s, str(entry.get("name") or "").lower(), str(entry.get("connector") or "").lower()])
    if s in workspace_ids() or "workspace" in text:
        return "workspace"
    for k in ("slack", "github", "gbrain", "stackshot", "notion"):
        if k in text:
            return k
    return "other"


def split_entry(ident, entries):
    """(entry, tool) of a portal name `<entry>_<tool>`: the longest known entry id that prefixes it; else (None, ident)."""
    best = None
    for e in entries:
        if ident.startswith(e + "_") and (best is None or len(e) > len(best)):
            best = e
    return (best, ident[len(best) + 1:]) if best else (None, ident)


def calls_of(tool_name, tool_input=None, mcp_server=None):
    try:
        return _calls_of(str(tool_name or ""), tool_input if isinstance(tool_input, dict) else {}, mcp_server)
    except Exception:
        return []


def _calls_of(tool, inp, mcp_server):
    if not tool.startswith("mcp__"):
        return []
    conn, cat, res = _config.import_stackshot("connectors", "hook_calls"), {}, None
    if conn is not None:
        try:
            cat = _catalog(conn)
            res = conn.resolve(tool, inp, mcp_server)
        except Exception:
            res = None
    entries = sorted(set(fallback_entries()) | set((cat.get("servers") or {})), key=len, reverse=True)
    out = []
    if res is not None and res.route:
        for c in res.calls:
            server, name = c.server, c.tool
            if server is None:
                server, name = split_entry(name, entries)
            elif server == getattr(conn, "PORTAL", "portal"):
                continue
            out.append((kind_of(server, cat), server or "?", name))
        return out
    if EXECUTE.search(tool):
        for m in CALL.finditer(str(inp.get("code") or "")):
            server, name = split_entry(m.group(1), entries)
            out.append((kind_of(server, cat), server or "?", name))
        return out
    parts = tool.split("__")
    if len(parts) < 3:
        return []
    server, name = parts[1], "__".join(parts[2:])
    return [(kind_of(server, cat), server, name)]
