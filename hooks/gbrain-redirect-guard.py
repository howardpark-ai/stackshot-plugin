#!/usr/bin/env python3
"""gbrain-redirect-guard.py - PreToolUse hook: a gbrain write aimed at a page that is only a pointer is refused, with the fix naming
where to write instead. Three checks, one lookup.

1. Redirect stub. A renamed page leaves a stub behind: frontmatter `type: redirect`, `redirect_to: <slug>` (stackshot's
   rename_page.py writes it). gbrain itself accepts any write to a stub, so a session that writes to the old slug lands its edit on
   a page nobody reads. Refused, naming the page it points to.
2. Moved page. A page moved to another brain leaves a pointer page on this one, and gbrain accepts writes to it too, which land
   where the readers of the other brain never see them. The source of truth is the pointer's own banner, a line of its body that
   starts `> **` and says where the record is (setting gbrain_moved_banner). A write to such a page is refused; the text quotes
   the banner, names the page the banner links to (a markdown link to `.../blob/<branch>/<slug>.md`, else the same slug) and
   says to repeat the same call on the other brain. Nothing about the other brain is listed in this file: which pages are moved is
   what their banners say.
3. New page of the other brain. A page that does not exist yet here, written with put_page or put_pages (the tools that can create
   one), whose slug matches a pattern of the setting gbrain_newpage_patterns, or whose literal content has the frontmatter line
   `org: <value>` of the setting gbrain_newpage_org, is a page of the other brain: it is created there. Refused, with the sentence
   of the setting gbrain_newpage_hint saying how. Off until one of the two settings is set. Slugs of the setting gbrain_moved_keep
   pass, and so does a page that exists already (a write to it is the owner's call, as before).

The way through for the second and third check (a move in progress, or a write that belongs on this brain, such as the owner's own
take on a moved page) is a narrow, expiring permit, never a flag in the call: a file `<id>.json` holding `{"slugs": [...], "expires":
<epoch seconds>}` in `$XDG_STATE_HOME/gbrain-move-permits` (default `~/.local/state/gbrain-move-permits`), whose slugs each cover
the page and its sub-pages. The folder is written by the owner's own tool (setting gbrain_move_permit_cli names its command, which
the refusal then shows); this hook only reads it. A redirect stub is refused always, a permit or not.

Which calls: the gbrain tools that write to a named page, made on the direct connector or inside an MCP portal Code Mode script
(`codemode.gbrain_<tool>({...})`). stackshot's connectors.py (STACKSHOT_DIR) resolves both routes to `gbrain.<tool>`; a direct tool
name of a gbrain connector the catalog does not list (`mcp__...GBrain...__put_page`) is caught by its name, unless a pattern of the
setting gbrain_direct_exclude matches it. The target slugs: `slug` (put_page, edit_page, append_page, add_timeline_entry, add_tag,
remove_tag, put_raw_data, takes_*), `from` (add_link, remove_link: the edge renders on that page), `pages[].slug` (put_pages). A
slug the portal script computes is not looked up. Reads (get_page, search, ...) are never touched. Not covered: capture (it names no
page), delete_page, restore_page, revert_version.

Lookup: one read-only query on the local gbrain Postgres database (`psql -d gbrain`, ~50 ms; PSQL_BIN and GBRAIN_DB override), else
the gbrain CLI (`gbrain call get_page`, ~0.7 s a slug; GBRAIN_BIN overrides). The whole lookup is capped at ~2 s. Fail open: no
psql and no gbrain CLI, no stackshot checkout, a lookup error, a timeout: the call goes on. A page counts as new only when the
lookup says it is not there, never when it could not look.

Only the portal entries listed in the setting gbrain_entries (default `gbrain`) are covered, since the lookup reads this box's own
gbrain database: a second brain served by another host needs its own lookup first, and stays out.

Settings (plugin.json key or STACKSHOT_PLUGIN_<NAME>; see the README):
  gbrain_entries            portal entry ids of the gbrain whose database is on this box (default ["gbrain"])
  gbrain_moved_banner       regex of the banner line of a moved page, see BANNER below (default: any "> **...record for this page is in ...")
  gbrain_moved_keep         regexes (case-insensitive) of slugs that are never refused as moved or as a new page of the other brain (default none)
  gbrain_moved_write_hint   a sentence appended to the moved-page refusal, saying how to write on the other brain; {tool}, {slug}
                            and {target} are replaced (default none)
  gbrain_move_permit_cli    the command that grants a permit, shown in the refusal; {slug} is replaced (default none: no hint)
  gbrain_newpage_patterns   regexes (case-insensitive, searched in the slug) of the pages that belong on the other brain, such as
                            "^companies/acme(/|$)" (default none: the third check is off)
  gbrain_newpage_org        the value of a frontmatter line `org: <value>` that marks a people / projects / companies page as one of
                            the other brain (default none)
  gbrain_newpage_hint       a sentence appended to the new-page refusal, saying how to write on the other brain; {tool} and {slug}
                            are replaced (default none)
  gbrain_direct_exclude     regexes (case-insensitive) of direct tool names this hook never looks at, such as the connector of
                            another brain (default none)

Slugs are lower-cased before anything is looked up or compared (gbrain's own normalisation), so a mixed-case slug of a moved page
is refused and one of a live page passes. A regex of a setting is never run on a slug longer than MAX_SLUG characters, and the
whole hook runs under a timer of BUDGET + 1 seconds that ends it with exit 0 (fail open), so a pathological pattern cannot hold a
tool call.

A refusal is a JSON permissionDecision "deny" on stdout with exit 0. Any error: silent exit 0.
"""
import glob, json, os, re, shutil, signal, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402

BUDGET = float(os.environ.get("GBRAIN_REDIRECT_BUDGET") or 2.0)        # seconds for the whole lookup
WRITE_TOOLS = ("put_page", "put_pages", "edit_page", "append_page", "add_timeline_entry", "add_tag", "remove_tag", "add_link",
               "remove_link", "put_raw_data", "takes_add", "takes_update", "takes_supersede", "takes_resolve")
MAX_SLUG = 300                                                          # a longer slug is never matched against a setting's regex
NEW_PAGE_TOOLS = ("put_page", "put_pages")                              # the tools that can create a page
ORG_ROOTS = ("people", "projects", "companies")                         # the page kinds whose frontmatter `org:` is read
DIRECT = re.compile(r"(?i)^mcp__.*gbrain.*__(%s)$" % "|".join(WRITE_TOOLS))   # a gbrain connector the catalog lacks

# The banner of a pointer page: a line of its body that starts "> **" and says the record is elsewhere. The setting gbrain_moved_banner
# replaces this pattern (the line without anchors; the hook adds the start-of-line anchor and group 1 is the whole line). Postgres
# (ARE) and Python's re read the same pattern, so keep it to the syntax both share.
DEFAULT_BANNER = r"> \*\*The (?:\w+ )?record for this page is in [^\n]*"

REFUSAL = ("{slug} is a redirect to {target}: write to {target} instead. (A stub left by a rename: frontmatter type: redirect, "
           "redirect_to: {target}. Refused by gbrain-redirect-guard.py; read {target} with get_page and make the change there.)")
MOVED = ("{slug} has moved to another brain: this page is only its pointer (its banner says: \"{banner}\"), and an entry written "
         "here never reaches the readers of the brain it moved to. Write it there instead: the same {tool} call with the same "
         "arguments, on page {target}.{hint} Reads of {slug} stay open.{permit} (Refused by gbrain-redirect-guard.py.)")
NEWPAGE = ("{slug} is a new page of the other brain ({why}): pages of that kind are created there, not on this brain. Write it "
           "there instead: the same {tool} call with the same slug and content.{hint}{permit} (Refused by gbrain-redirect-guard.py.)")
NEWPAGE_PERMIT = (" If this page belongs on this brain (the owner's own take, a private line), grant a narrow expiring permit for the "
                  "slug and repeat the call: {cli}.")
PERMIT = (" If this write belongs on this brain (the owner's own take, a private line, a session record) or you are the session "
          "moving the page, grant a narrow expiring permit for the slug and repeat the call: {cli}.")


def banner_regexes():
    """(Python regex for the banner with group 1 the whole line, the pattern the database gets). The setting
    gbrain_moved_banner, when it is a valid regex, else DEFAULT_BANNER."""
    for pat in (_config.get_str("gbrain_moved_banner"), DEFAULT_BANNER):
        if not pat:
            continue
        full = r"(?:^|\n)(" + pat + ")"
        try:
            return re.compile(full), full
        except re.error:
            _config.note("gbrain-redirect-guard", "the setting gbrain_moved_banner is not a valid regular expression; using the default")
    full = r"(?:^|\n)(" + DEFAULT_BANNER + ")"
    return re.compile(full), full


def regexes(setting):
    """The compiled, case-insensitive regexes of a list setting; an invalid one is skipped with one stderr line."""
    out = []
    for pat in _config.get_list(setting):
        try:
            out.append(re.compile(pat, re.I))
        except re.error:
            _config.note("gbrain-redirect-guard", f"a pattern of the setting {setting} is not a valid regular expression; skipped")
    return out


def patterns():
    return tuple(f"{e}.{t}" for e in _config.get_list("gbrain_entries", ["gbrain"]) for t in WRITE_TOOLS)


def slugs_of(tool, args):
    """[(slug, content or None)] the write names, as literal strings; a computed slug (DYNAMIC) is skipped. The content is the page
    text a put_page / put_pages carries, when it is a literal."""
    if not isinstance(args, dict):
        return []
    out = []
    if tool == "put_pages":
        pages = args.get("pages")
        for p in pages if isinstance(pages, list) else []:
            if isinstance(p, dict) and isinstance(p.get("slug"), str):
                out.append((p["slug"], p.get("content") if isinstance(p.get("content"), str) else None))
    elif tool in ("add_link", "remove_link"):
        if isinstance(args.get("from"), str):
            out.append((args["from"], None))
    elif isinstance(args.get("slug"), str):
        out.append((args["slug"], args.get("content") if tool == "put_page" and isinstance(args.get("content"), str) else None))
    seen, res = set(), []
    for s, c in ((x.strip().lower(), c) for x, c in out):
        if s and s not in seen:
            seen.add(s)
            res.append((s, c))
    return res


def targets(tool_name, inp, mcp_server):
    """[(slug, source_id or None, tool, content or None)] of every gbrain page write in the call."""
    res, calls = None, []
    conn = _config.import_stackshot("connectors", "gbrain-redirect-guard")
    if conn is not None:
        try:
            res = conn.resolve(tool_name, inp, mcp_server)
            calls = conn.matches(res.calls, patterns()) if res is not None else []
        except Exception:
            calls = []
    out = []
    for c in calls:
        src = c.args.get("source_id") if isinstance(c.args, dict) and isinstance(c.args.get("source_id"), str) else None
        out += [(s, src, c.tool, body) for s, body in slugs_of(c.tool, c.args)]
    if not calls and (res is None or res.route is None):          # no catalog, or a gbrain connector it does not list
        m = DIRECT.match(tool_name)
        if m and isinstance(inp, dict) and not any(rx.search(tool_name) for rx in regexes("gbrain_direct_exclude")):
            src = inp.get("source_id") if isinstance(inp.get("source_id"), str) else None
            tool = m.group(1).lower()
            out += [(s, src, tool, body) for s, body in slugs_of(tool, inp)]
    return out


NOT_THERE = {"exists": False, "type": "", "redirect_to": "", "banner": ""}


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")}


def merge(old, new):
    """Two rows of one slug (one per source): what is true on either counts."""
    if old is None:
        return new
    return {"exists": True, "type": old["type"] if old["type"] == "redirect" else new["type"],
            "redirect_to": old["redirect_to"] or new["redirect_to"], "banner": old["banner"] or new["banner"]}


def lookup_psql(pairs, deadline, banner):
    """{slug: {exists, type, redirect_to, banner}} of the pages among `pairs`, by one read-only query, a slug it did not find with
    exists False; None when psql is absent or the query fails."""
    psql = os.environ.get("PSQL_BIN") or shutil.which("psql")
    if not psql or not os.path.isfile(psql) or not os.access(psql, os.X_OK):
        return None
    rows = json.dumps([{"slug": s, "src": src} for s, src, *_ in pairs])
    sql = ("\\set ON_ERROR_STOP on\n"
           "begin transaction read only;\n"
           "select p.slug, p.type, coalesce(p.frontmatter->>'redirect_to',''), "
           "replace(replace(coalesce(substring(p.compiled_truth from :'banner'),''), E'\\t', ' '), E'\\r', ' ') from pages p "
           "join jsonb_to_recordset(:'rows'::jsonb) as w(slug text, src text) "
           "on p.slug = w.slug and (w.src is null or p.source_id = w.src) "
           "where p.deleted_at is null;\n"
           "commit;\n")
    try:
        r = subprocess.run([psql, "-X", "-q", "-A", "-t", "-F", "\t", "-d", os.environ.get("GBRAIN_DB") or "gbrain",
                            "-v", "rows=" + rows, "-v", "banner=" + banner], input=sql, capture_output=True, text=True,
                           env=clean_env(), cwd=os.path.expanduser("~"), timeout=max(0.1, deadline - time.time()))
    except Exception:
        return None
    if r.returncode != 0:
        _config.note("gbrain-redirect-guard", f"psql exited {r.returncode}, so the gbrain CLI is tried instead; if the setting "
                     "gbrain_moved_banner is a pattern Postgres rejects, fix it (Python and Postgres read the same pattern)")
        return None
    found = {s: dict(NOT_THERE) for s, *_ in pairs}
    seen = {}
    for line in r.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        seen[cols[0]] = merge(seen.get(cols[0]), {"exists": True, "type": cols[1].strip(), "redirect_to": cols[2].strip(),
                                                  "banner": cols[3].strip() if len(cols) > 3 else ""})
    found.update(seen)
    return found


def lookup_cli(pairs, deadline, banner_re):
    """{slug: {exists, type, redirect_to, banner}} through `gbrain call get_page`; a slug it cannot read in time is left out
    (unknown, not new)."""
    g = os.environ.get("GBRAIN_BIN") or os.path.expanduser("~/.bun/bin/gbrain")
    if not os.path.isfile(g) or not os.access(g, os.X_OK):
        return {}
    found = {}
    for slug, src, *_ in pairs:
        left = deadline - time.time()
        if left <= 0.05:
            break
        q = {"slug": slug, "include_content": True, "content_only": True}
        if src:
            q["source_id"] = src
        try:
            r = subprocess.run([g, "call", "get_page", json.dumps(q)], capture_output=True, text=True, env=clean_env(),
                               cwd=os.path.expanduser("~"), stdin=subprocess.DEVNULL, timeout=left)
        except Exception:
            continue
        raw = r.stdout
        if r.returncode != 0:
            if "page_not_found" in raw + r.stderr:
                found[slug] = dict(NOT_THERE)
            continue
        i = raw.find("{")
        try:
            d, _ = json.JSONDecoder().raw_decode(raw, i) if i >= 0 else (None, 0)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("error"):
            if isinstance(d, dict) and d.get("code") == "page_not_found":
                found[slug] = dict(NOT_THERE)
            continue
        content = str(d.get("content") or "")
        fm = re.match(r"---\n(.*?)\n---", content, re.S)
        fm = fm.group(1) if fm else ""
        typ = d.get("type") or (re.search(r"(?m)^type:\s*['\"]?([\w-]+)", fm) or [None, None])[1] or ""
        m = re.search(r"(?m)^redirect_to:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", fm)
        b = banner_re.search(content)
        found[slug] = {"exists": True, "type": typ, "redirect_to": m.group(1).strip() if (m and typ == "redirect") else "",
                       "banner": b.group(1).strip() if b else ""}
    return found


def lookup(pairs, banner_re, banner):
    deadline = time.time() + BUDGET
    found = lookup_psql(pairs, deadline, banner)
    if found is None:
        found = lookup_cli(pairs, deadline, banner_re)
    return found


# ---------------------------------------------------------------- the way through a moved page: expiring permits

def permit_dir():
    base = os.environ.get("XDG_STATE_HOME", "")
    if not os.path.isabs(base):                                  # a relative XDG_STATE_HOME is ignored (the XDG specification)
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "gbrain-move-permits")


def permitted(slug, now=None):
    """True when a live permit file names `slug` or one of its parents. A file that cannot be read or has another shape is ignored."""
    now = time.time() if now is None else now
    for f in sorted(glob.glob(os.path.join(permit_dir(), "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            if not (isinstance(d, dict) and isinstance(d.get("slugs"), list) and isinstance(d.get("expires"), (int, float))
                    and d["expires"] > now):
                continue
            for s in d["slugs"]:
                s = s.strip("/").lower() if isinstance(s, str) else ""
                if s and (slug == s or slug.startswith(s + "/")):
                    return True
        except Exception:
            continue
    return False


def kept(slug, keep):
    return len(slug) <= MAX_SLUG and any(rx.search(slug) for rx in keep)


def moved_target(banner, slug):
    """The page on the other brain, read from the banner's repository link (`.../blob/<branch>/<slug>.md`); the same slug when
    the banner links none."""
    m = re.search(r"\]\(https?://[^)\s]*?/blob/[^/\s)]+/([^)\s]+?)\.md", banner or "")
    return m.group(1) if m else slug


def banner_lead(banner):
    """The bold lead of the banner (what it says), else its first 160 characters."""
    m = re.match(r">\s*\*\*(.+?)\*\*", banner or "")
    text = (m.group(1) if m else (banner or "")).strip().replace('"', "'")
    return (text[:160] + "...") if len(text) > 160 else text


def newpage_why(slug, content, patterns, org):
    """Why a NEW page at this slug belongs on the other brain ('' when it does not): its slug matches a pattern of the setting
    gbrain_newpage_patterns, or a people / projects / companies page carries the frontmatter line `org: <gbrain_newpage_org>`."""
    for rx in patterns if len(slug) <= MAX_SLUG else ():
        if rx.search(slug):
            return "its slug matches the setting gbrain_newpage_patterns"
    segs = slug.split("/")
    if org and len(segs) > 1 and segs[0] in ORG_ROOTS and isinstance(content, str):
        fm = re.match(r"\s*---\r?\n(.*?)\r?\n---", content, re.S)
        if fm and re.search(r"(?mi)^org:\s*['\"]?" + re.escape(org) + r"['\"]?\s*(#.*)?$", fm.group(1)):
            return "its frontmatter says org: " + org
    return ""


def deny(reason):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                        "permissionDecisionReason": reason}}))   # ASCII-escaped: a lone surrogate in a slug cannot lose the deny
    return 0


def refusals(pairs, info):
    """One refusal line per page write that breaks a rule; the lookup decides, a slug it knows nothing of passes."""
    lines, done = [], set()
    keep = regexes("gbrain_moved_keep")
    cli = _config.get_str("gbrain_move_permit_cli").strip()
    hint = _config.get_str("gbrain_moved_write_hint").strip()
    new_patterns = regexes("gbrain_newpage_patterns")
    new_org = _config.get_str("gbrain_newpage_org").strip()
    new_hint = _config.get_str("gbrain_newpage_hint").strip()
    for slug, _src, tool, content in pairs:
        i = info.get(slug)
        if i is None or (slug, tool) in done:
            continue
        done.add((slug, tool))
        if i["type"] == "redirect":
            lines.append(REFUSAL.format(slug=slug, target=i["redirect_to"] or "the page named in its redirect_to"))
        elif kept(slug, keep):
            continue
        elif i["banner"]:
            if not permitted(slug):
                target = moved_target(i["banner"], slug)
                lines.append(MOVED.format(
                    slug=slug, tool=tool, target=target, banner=banner_lead(i["banner"]),
                    hint=(" " + hint.replace("{tool}", tool).replace("{slug}", slug).replace("{target}", target)) if hint else "",
                    permit=PERMIT.format(cli=cli.replace("{slug}", slug)) if cli else ""))
        elif not i["exists"] and tool in NEW_PAGE_TOOLS:
            why = newpage_why(slug, content, new_patterns, new_org)
            if why and not permitted(slug):
                lines.append(NEWPAGE.format(
                    slug=slug, tool=tool, why=why,
                    hint=(" " + new_hint.replace("{tool}", tool).replace("{slug}", slug)) if new_hint else "",
                    permit=NEWPAGE_PERMIT.format(cli=cli.replace("{slug}", slug)) if cli else ""))
    return lines


def _out_of_time(*_):
    sys.exit(0)                                       # fail open: the call goes on


def main():
    timer = hasattr(signal, "setitimer")
    if timer:
        signal.signal(signal.SIGALRM, _out_of_time)
        signal.setitimer(signal.ITIMER_REAL, BUDGET + 1)
    try:
        return run()
    finally:
        if timer:
            signal.setitimer(signal.ITIMER_REAL, 0)


def run():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(ev, dict):
        return 0
    tool = str(ev.get("tool_name") or "")
    inp = ev.get("tool_input") or {}
    if not tool.startswith("mcp__") or not isinstance(inp, dict):
        return 0
    code = str(inp.get("code") or "")
    if not ("gbrain" in tool.lower() or "codemode" in code):
        return 0                                      # the fast path: no gbrain tool, no portal script
    pairs = targets(tool, inp, ev.get("mcp_server"))
    if not pairs:
        return 0
    banner_re, banner = banner_regexes()
    info = lookup(pairs, banner_re, banner)
    lines = refusals(pairs, info)
    if not lines:
        return 0
    return deny(lines[0] if len(lines) == 1 else "\n".join(lines))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
