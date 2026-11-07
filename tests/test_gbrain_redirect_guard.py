"""gbrain-redirect-guard.py: a write to a redirect stub, and a write to a page that has moved to another brain (its pointer page
carries a banner), are refused with the page to write to named; so is the creation of a new page that belongs on the other brain
(settings gbrain_newpage_*). Everything that differs per owner is a setting: the banner pattern, the command that grants a permit,
the hint for writing on the other brain, the slugs that stay, the new-page patterns, the tool names the hook leaves alone.

Run:  uv run --no-project python -m unittest tests.test_gbrain_redirect_guard -v   (from the repository root; or discover -s tests)

The hook runs as Claude Code runs it (hook JSON on stdin) under a temp HOME. The lookups never reach a real brain: PSQL_BIN and
GBRAIN_BIN point at stand-ins that answer from a fixture map of pages (slug -> type, redirect_to, body; a slug not in the map does not
exist: the stand-in psql prints one row per page that exists, so a slug without a row is new), and it applies the banner pattern
the hook hands it to the page body, as the database does. Permits are
files under the temp XDG_STATE_HOME. Each test makes its own locked temp folder (tests/_support.py) and removes it when it ends; no
test kills a process, starts tmux or touches the real home directory."""
import json
import os
import sys
import time
import unittest

import _support as S
from _support import decision, env_for, reason, run_hook, temp_home

PSQL_STANDIN = '''#!%(py)s
import json, re, sys
mp = json.load(open(%(map)r))
if mp.get("_psql_fail"):
    sys.exit(2)
args = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in sys.argv if a.startswith(("rows=", "banner="))}
sys.stdin.read()
rx = re.compile(args["banner"])
for w in json.loads(args["rows"]):
    p = mp.get(w["slug"])
    if not isinstance(p, dict):
        continue
    m = rx.search(p.get("body", ""))
    print("\\t".join([w["slug"], p.get("type", "note"), p.get("redirect_to", ""), m.group(1) if m else ""]))
'''

GBRAIN_STANDIN = '''#!%(py)s
import json, sys, time
mp = json.load(open(%(map)r))
if mp.get("_sleep"):
    time.sleep(mp["_sleep"])
if mp.get("_cli_fail"):
    print("boom", file=sys.stderr)
    sys.exit(1)
if mp.get("_cli_msg"):
    print(json.dumps({"error": mp["_cli_msg"], "code": "db_error"}))
    sys.exit(mp.get("_cli_rc", 0))
q = json.loads(sys.argv[3])
p = mp.get(q["slug"])
if not isinstance(p, dict):
    print(json.dumps({"error": "page_not_found", "code": "page_not_found"}))
    sys.exit(1)
fm = "---\\ntype: %%s\\n%%s---\\n\\n" %% (p.get("type", "note"), ("redirect_to: %%s\\n" %% p["redirect_to"]) if p.get("redirect_to") else "")
print(json.dumps({"slug": q["slug"], "type": p.get("type", "note"), "content": fm + p.get("body", "")}, indent=2))
'''

# A stand-in for stackshot's connectors.py: a portal script's `codemode.<entry>_<tool>({k: "v"})` calls, entries gbrain and other.
CONNECTORS_STUB = '''
import collections, re
Res = collections.namedtuple("Res", "route calls")
Call = collections.namedtuple("Call", "server tool args clean")

def resolve(tool, inp, mcp_server=None, catalog=None):
    if not tool.endswith("portal_codemode_execute"):
        return Res(None, [])
    calls = []
    for m in re.finditer(r"codemode\\.(gbrain|other)_(\\w+)\\(\\{([^}]*)\\}\\)", str(inp.get("code") or "")):
        args = dict(re.findall(r"(\\w+)\\s*:\\s*\\"([^\\"]*)\\"", m.group(3)))
        calls.append(Call(m.group(1), m.group(2), args, True))
    return Res("portal", calls)

def matches(calls, patterns):
    return [c for c in calls if c.server + "." + c.tool in patterns]
'''

STUB, TARGET = "notes/old-name", "notes/new-name"
MOVED, MOVED_SUB, OTHER_MOVED = "projects/alpha", "projects/alpha/contract", "projects/beta"
LIVE = "projects/gamma"
BANNER = ("> **The record for this page is in acme-brain** (moved last week): `projects/alpha` "
          "([GitHub](https://github.com/acme/acme-brain/blob/main/projects/alpha.md)), reached through the other connector")
BANNER_COMPANY = BANNER.replace("The record", "The company record").replace("projects/alpha", "projects/beta")
BANNER_LOCAL = "> **Cette page est dans acme-brain** (deplacee): `projects/delta`"           # a wording the default pattern does not read
GB = "mcp__acme_GBrain__"
PX = "mcp__acme_Portal__portal_codemode_execute"


def page(banner=None, **kw):
    d = {"type": "note", "body": "# T\n\n" + (banner + "\n\n" if banner else "") + "body\n"}
    d.update(kw)
    return d


class Env:
    """One test's world: a temp home with the two stand-ins, a pages map and a stub stackshot checkout."""

    def __init__(self, home, pages=None, **extra):
        self.home = home
        self.map = os.path.join(home, "pages.json")
        self.set_pages(pages if pages is not None else self.default_pages())
        for name, body in (("psql", PSQL_STANDIN), ("gbrain", GBRAIN_STANDIN)):
            path = os.path.join(home, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body % {"py": sys.executable, "map": self.map})
            os.chmod(path, 0o755)
        self.stackshot = os.path.join(home, "stackshot")
        os.makedirs(self.stackshot)
        with open(os.path.join(self.stackshot, "connectors.py"), "w", encoding="utf-8") as f:
            f.write(CONNECTORS_STUB)
        self.env = env_for(home, PSQL_BIN=os.path.join(home, "psql"), GBRAIN_BIN=os.path.join(home, "gbrain"),
                           STACKSHOT_DIR=self.stackshot, **extra)

    @staticmethod
    def default_pages():
        return {STUB: {"type": "redirect", "redirect_to": TARGET, "body": "Moved.\n"},
                MOVED: page(BANNER), MOVED_SUB: page(BANNER), OTHER_MOVED: page(BANNER_COMPANY),
                LIVE: page(), "sessions/s1": page(BANNER)}

    def set_pages(self, d):
        with open(self.map, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def setting(self, **kw):
        """Write the plugin.json of this home."""
        d = os.path.join(self.home, ".config", "stackshot")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "plugin.json"), "w", encoding="utf-8") as f:
            json.dump(kw, f)

    def permit(self, slugs, expires=None, name="p1"):
        d = os.path.join(self.home, ".local", "state", "gbrain-move-permits")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
            json.dump({"id": name, "slugs": slugs, "expires": time.time() + 3600 if expires is None else expires}, f)

    def run(self, tool, inp, env=None, mcp_server=None):
        ev = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp}
        if mcp_server:
            ev["mcp_server"] = mcp_server
        rc, out, err = run_hook("gbrain-redirect-guard.py", ev, env or self.env)
        assert rc == 0, err
        self.last_err = err
        return out


def code(*calls):
    return {"code": "async () => {\n" + "\n".join(f"  await codemode.{c};" for c in calls) + "\n  return 1;\n}"}


class Guard(unittest.TestCase):
    def verdict(self, e, tool, inp, **kw):
        out = e.run(tool, inp, **kw)
        S.assert_no_owner(self, out + e.last_err, "the hook output")
        return decision(out), reason(out)

    def entry(self, slug, **kw):
        return {"slug": slug, "date": "2030-01-15", "summary": "x", **kw}


class RedirectStub(Guard):
    def test_write_to_a_stub_is_refused_on_every_write_tool(self):
        with temp_home() as home:
            e = Env(home)
            for tool, inp in [("add_timeline_entry", self.entry(STUB)), ("put_page", {"slug": STUB, "content": "x"}),
                              ("edit_page", {"slug": STUB, "expected_revision": "r", "edits": []}),
                              ("append_page", {"slug": STUB, "text": "x"}), ("add_tag", {"slug": STUB, "tag": "x"}),
                              ("put_raw_data", {"slug": STUB, "source": "s", "data": {}}),
                              ("add_link", {"from": STUB, "to": LIVE}),
                              ("put_pages", {"pages": [{"slug": LIVE, "content": "x"}, {"slug": STUB, "content": "y"}]})]:
                d, why = self.verdict(e, GB + tool, inp)
                self.assertEqual(d, "deny", tool)
                self.assertTrue(why.startswith(f"{STUB} is a redirect to {TARGET}: write to {TARGET} instead."), tool)

    def test_a_permit_never_covers_a_stub(self):
        with temp_home() as home:
            e = Env(home)
            e.permit([STUB])
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": STUB, "tag": "x"})[0], "deny")

    def test_cli_fallback_when_psql_is_absent_or_fails(self):
        with temp_home() as home:
            e = Env(home)
            env = dict(e.env, PSQL_BIN="/nonexistent/psql")
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": STUB, "tag": "x"}, env=env)[0], "deny")
            e.set_pages(dict(e.default_pages(), _psql_fail=True))
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": STUB, "tag": "x"})[0], "deny")


class MovedPage(Guard):
    def test_every_write_tool_is_refused_naming_the_banner_and_the_target(self):
        with temp_home() as home:
            e = Env(home)
            for tool, inp in [("add_timeline_entry", self.entry(MOVED)), ("append_page", {"slug": MOVED, "text": "x"}),
                              ("put_page", {"slug": MOVED, "content": "x"}),
                              ("edit_page", {"slug": MOVED, "expected_revision": "r", "edits": []}),
                              ("add_tag", {"slug": MOVED, "tag": "x"}), ("add_link", {"from": MOVED, "to": LIVE}),
                              ("put_pages", {"pages": [{"slug": LIVE, "content": "x"}, {"slug": MOVED_SUB, "content": "y"}]})]:
                d, why = self.verdict(e, GB + tool, inp)
                self.assertEqual(d, "deny", tool)
                self.assertIn("has moved to another brain", why)
                self.assertIn("The record for this page is in acme-brain", why)       # the banner's bold lead, quoted
                self.assertIn(f"the same {tool} call", why)
                self.assertIn("on page projects/alpha.", why)                          # from the banner's repository link
            # the company wording is read by the default pattern too, and the target is the one its link names
            d, why = self.verdict(e, GB + "append_page", {"slug": OTHER_MOVED, "text": "x"})
            self.assertEqual(d, "deny")
            self.assertIn("The company record for this page is in acme-brain", why)

    def test_a_banner_without_a_link_names_the_same_slug(self):
        with temp_home() as home:
            e = Env(home, {MOVED: page("> **The record for this page is in acme-brain** (no link)")})
            self.assertIn(f"on page {MOVED}.", self.verdict(e, GB + "add_tag", {"slug": MOVED, "tag": "x"})[1])

    def test_no_permit_command_set_means_no_hint_and_no_hint_text(self):
        with temp_home() as home:
            e = Env(home)
            why = self.verdict(e, GB + "add_tag", {"slug": MOVED, "tag": "x"})[1]
            self.assertNotIn("permit", why)
            self.assertNotIn("~", why)
            self.assertIn("Reads of " + MOVED + " stay open.", why)

    def test_the_permit_command_and_the_write_hint_are_the_settings(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(gbrain_move_permit_cli="my-permit grant --slugs {slug} --ttl 2h",
                      gbrain_moved_write_hint="Use `other.{tool}({...})` for {target} (from {slug}).")
            why = self.verdict(e, GB + "append_page", {"slug": MOVED, "text": "x"})[1]
            self.assertIn("grant a narrow expiring permit for the slug and repeat the call: my-permit grant --slugs "
                          f"{MOVED} --ttl 2h.", why)
            self.assertIn("on page projects/alpha. Use `other.append_page({...})` for projects/alpha (from " + MOVED + ").", why)
            # through the environment, as the README says
            env = dict(e.env, STACKSHOT_PLUGIN_GBRAIN_MOVE_PERMIT_CLI="env-cli {slug}")
            self.assertIn("repeat the call: env-cli " + MOVED + ".", self.verdict(e, GB + "append_page", {"slug": MOVED, "text": "x"}, env=env)[1])

    def test_reads_links_to_a_moved_page_and_live_pages_pass(self):
        with temp_home() as home:
            e = Env(home)
            for tool in ("get_page", "search", "get_timeline"):
                self.assertEqual(self.verdict(e, GB + tool, {"slug": MOVED, "query": "x"})[0], "allow", tool)
            self.assertEqual(self.verdict(e, GB + "add_link", {"from": LIVE, "to": MOVED})[0], "allow")   # the edge renders on `from`
            self.assertEqual(self.verdict(e, GB + "add_timeline_entry", self.entry(LIVE))[0], "allow")
            self.assertEqual(self.verdict(e, GB + "add_timeline_entry", self.entry("missing/page"))[0], "allow")
            self.assertEqual(self.verdict(e, "mcp__other__send_message", {"slug": MOVED})[0], "allow")

    def test_cli_fallback_refuses_a_moved_page(self):
        with temp_home() as home:
            e = Env(home)
            env = dict(e.env, PSQL_BIN="/nonexistent/psql")
            self.assertIn("has moved to another brain", self.verdict(e, GB + "add_tag", {"slug": MOVED, "tag": "x"}, env=env)[1])
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": LIVE, "tag": "x"}, env=env)[0], "allow")
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": "missing/page", "tag": "x"}, env=env)[0], "allow")

    def test_fail_open_when_the_lookup_cannot_tell(self):
        with temp_home() as home:
            e = Env(home)
            nothing = dict(e.env, PSQL_BIN="/nonexistent/psql", GBRAIN_BIN="/nonexistent/gbrain")
            self.assertEqual(e.run(GB + "add_tag", {"slug": MOVED, "tag": "x"}, env=nothing), "")
            e.set_pages(dict(e.default_pages(), _psql_fail=True, _cli_fail=True))
            self.assertEqual(e.run(GB + "add_tag", {"slug": MOVED, "tag": "x"}), "")
            self.assertEqual(e.run(GB + "add_tag", {"slug": STUB, "tag": "x"}), "")
            # a stalled CLI is cut off by the budget
            e.set_pages(dict(e.default_pages(), _psql_fail=True, _sleep=3))
            env = dict(e.env, GBRAIN_REDIRECT_BUDGET="0.5")
            t = time.time()
            self.assertEqual(e.run(GB + "add_tag", {"slug": MOVED, "tag": "x"}, env=env), "")
            self.assertLess(time.time() - t, 2.5)

    def test_garbage_in_is_a_silent_allow(self):
        with temp_home() as home:
            e = Env(home)
            for payload in ("not json", "[]", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, {"tool_name": "mcp__x__y", "tool_input": []}):
                rc, out, _ = run_hook("gbrain-redirect-guard.py", payload, e.env)
                self.assertEqual((rc, out), (0, ""))


class Permits(Guard):
    def test_a_live_permit_covers_the_page_and_its_sub_pages_only(self):
        with temp_home() as home:
            e = Env(home)
            w = lambda slug: self.verdict(e, GB + "add_timeline_entry", self.entry(slug))[0]  # noqa: E731
            self.assertEqual((w(MOVED), w(MOVED_SUB), w(OTHER_MOVED)), ("deny", "deny", "deny"))
            e.permit([MOVED])
            self.assertEqual((w(MOVED), w(MOVED_SUB)), ("allow", "allow"))
            self.assertEqual(w(OTHER_MOVED), "deny")                                   # another moved page is not covered
            self.assertEqual(w("projects/alphabet"), "allow")                          # a name prefix is not a sub-page (and is live)
            e.set_pages(dict(e.default_pages(), **{"projects/alphabet": page(BANNER)}))
            self.assertEqual(w("projects/alphabet"), "deny")

    def test_an_expired_or_garbled_permit_does_not_count(self):
        with temp_home() as home:
            e = Env(home)
            w = lambda: self.verdict(e, GB + "add_timeline_entry", self.entry(MOVED))[0]  # noqa: E731
            e.permit([MOVED], expires=time.time() - 5, name="old")
            d = os.path.join(home, ".local", "state", "gbrain-move-permits")
            with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            with open(os.path.join(d, "odd.json"), "w", encoding="utf-8") as f:
                json.dump({"slugs": "x", "expires": "never"}, f)
            self.assertEqual(w(), "deny")
            e.permit([MOVED], name="fresh")
            self.assertEqual(w(), "allow")

    def test_the_permit_folder_follows_xdg_state_home(self):
        with temp_home() as home:
            e = Env(home)
            e.permit([MOVED])
            moved_state = os.path.join(home, "elsewhere")
            os.makedirs(moved_state)
            env = dict(e.env, XDG_STATE_HOME=moved_state)
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": MOVED, "tag": "x"}, env=env)[0], "deny")   # not in that folder
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": MOVED, "tag": "x"})[0], "allow")


class Settings(Guard):
    def test_the_banner_pattern_is_a_setting(self):
        with temp_home() as home:
            e = Env(home, {"projects/delta": page(BANNER_LOCAL), MOVED: page(BANNER)})
            w = lambda slug: self.verdict(e, GB + "add_tag", {"slug": slug, "tag": "x"})[0]  # noqa: E731
            self.assertEqual((w("projects/delta"), w(MOVED)), ("allow", "deny"))      # the default does not read the other wording
            e.setting(gbrain_moved_banner=r"> \*\*(?:Cette page est dans|The record for this page is in) [^\n]*")
            self.assertEqual((w("projects/delta"), w(MOVED)), ("deny", "deny"))
            why = self.verdict(e, GB + "add_tag", {"slug": "projects/delta", "tag": "x"})[1]
            self.assertIn("Cette page est dans acme-brain", why)
            # the same through the CLI fallback, which reads the pattern in Python
            env = dict(e.env, PSQL_BIN="/nonexistent/psql")
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": "projects/delta", "tag": "x"}, env=env)[0], "deny")

    def test_an_invalid_banner_pattern_falls_back_to_the_default_with_one_note(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(gbrain_moved_banner="(unclosed")
            out = e.run(GB + "add_tag", {"slug": MOVED, "tag": "x"})
            self.assertEqual(decision(out), "deny")
            self.assertEqual(len([ln for ln in e.last_err.splitlines() if "gbrain_moved_banner" in ln]), 1)

    def test_pages_that_stay_are_a_setting(self):
        with temp_home() as home:
            e = Env(home)
            w = lambda slug: self.verdict(e, GB + "add_timeline_entry", self.entry(slug))[0]  # noqa: E731
            self.assertEqual(w("sessions/s1"), "deny")                            # nothing stays by default
            e.setting(gbrain_moved_keep=["^sessions/", "BETA$"])
            self.assertEqual((w("sessions/s1"), w(OTHER_MOVED), w(MOVED)), ("allow", "allow", "deny"))
            e.set_pages(dict(e.default_pages(), **{"sessions/stub": {"type": "redirect", "redirect_to": "sessions/s1"}}))
            self.assertEqual(w("sessions/stub"), "deny")                          # a stub is refused whatever stays

    def test_direct_tool_names_can_be_excluded(self):
        with temp_home() as home:
            e = Env(home)
            other = "mcp__acme_OtherBrain_gbrain__"
            self.assertEqual(self.verdict(e, other + "add_tag", {"slug": MOVED, "tag": "x"})[0], "deny")   # by name, as the catalog lacks it
            e.setting(gbrain_direct_exclude=["otherbrain"])
            self.assertEqual(self.verdict(e, other + "add_tag", {"slug": MOVED, "tag": "x"})[0], "allow")
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": MOVED, "tag": "x"})[0], "deny")      # the others still count

    def test_an_invalid_exclude_pattern_is_skipped(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(gbrain_direct_exclude=["(bad"], gbrain_moved_keep=["(bad"])
            out = e.run(GB + "add_tag", {"slug": MOVED, "tag": "x"})
            self.assertEqual(decision(out), "deny")
            self.assertIn("gbrain_direct_exclude", e.last_err)


NEW = "companies/acme/deal"                       # a made-up slug of "the other brain", never a real one
NEW_SETTINGS = dict(gbrain_newpage_patterns=["^companies/acme(/|$)", "^projects/acme-"], gbrain_newpage_org="acme")
ORG_PAGE = "---\ntype: person\norg: acme\n---\n\n# Pat\n"


class NewPage(Guard):
    def put(self, e, slug, content="x", tool="put_page", **kw):
        if tool == "put_pages":
            return self.verdict(e, GB + tool, {"pages": [{"slug": LIVE, "content": "y"}, {"slug": slug, "content": content}]}, **kw)
        return self.verdict(e, GB + tool, {"slug": slug, "content": content}, **kw)

    def test_a_new_page_of_the_other_brain_is_refused_by_slug_and_by_org(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(**NEW_SETTINGS, gbrain_newpage_hint="Use `other.{tool}({...})` for {slug}.")
            for slug in (NEW, "companies/acme", "projects/acme-site"):
                d, why = self.put(e, slug)
                self.assertEqual(d, "deny", slug)
                self.assertIn(f"{slug} is a new page of the other brain (its slug matches the setting gbrain_newpage_patterns)", why)
                self.assertIn("the same put_page call with the same slug and content. Use `other.put_page({...})` for " + slug + ".", why)
                self.assertNotIn("permit", why)                                       # no permit command is set
            d, why = self.put(e, "people/pat", ORG_PAGE)
            self.assertEqual(d, "deny")
            self.assertIn("(its frontmatter says org: acme)", why)
            for content in ("---\norg: 'acme'  # note\n---\nx", "---\r\norg: acme\r\n---\r\nx"):
                self.assertEqual(self.put(e, "projects/pat", content)[0], "deny")
            # the frontmatter is read on people / projects / companies pages only, and only the value of the setting counts
            self.assertEqual(self.put(e, "notes/pat", ORG_PAGE)[0], "allow")
            self.assertEqual(self.put(e, "people/pat", ORG_PAGE.replace("acme", "other-org"))[0], "allow")
            self.assertEqual(self.put(e, "people/pat", "org: acme\n")[0], "allow")             # no frontmatter block
            self.assertEqual(self.put(e, "people/pat", "# Pat\n\n---\norg: acme\n---\n")[0], "allow")
            # only the tools that can create a page are looked at, and a content that is not a literal is not read
            self.assertEqual(self.verdict(e, GB + "add_timeline_entry", self.entry(NEW))[0], "allow")
            self.assertEqual(self.verdict(e, GB + "append_page", {"slug": NEW, "text": "x"})[0], "allow")
            self.assertEqual(self.verdict(e, GB + "put_page", {"slug": "people/pat"})[0], "allow")

    def test_the_org_setting_works_alone_and_so_do_the_patterns(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(gbrain_newpage_org="acme")
            self.assertEqual((self.put(e, NEW)[0], self.put(e, "people/pat", ORG_PAGE)[0]), ("allow", "deny"))
            e.setting(gbrain_newpage_patterns="^companies/acme")                     # a plain string works too
            self.assertEqual((self.put(e, NEW)[0], self.put(e, "people/pat", ORG_PAGE)[0]), ("deny", "allow"))

    def test_an_existing_page_passes(self):
        with temp_home() as home:
            e = Env(home, dict(Env.default_pages(), **{NEW: page(), "people/pat": page()}))
            e.setting(**NEW_SETTINGS)
            self.assertEqual(self.put(e, NEW)[0], "allow")
            self.assertEqual(self.put(e, "people/pat", ORG_PAGE)[0], "allow")
            self.assertEqual(self.put(e, NEW, tool="put_pages")[0], "allow")
            self.assertEqual(self.put(e, "companies/acme/other")[0], "deny")                      # a sibling that is new still is not
            # the CLI fallback tells the same
            env = dict(e.env, PSQL_BIN="/nonexistent/psql")
            self.assertEqual(self.put(e, NEW, env=env)[0], "allow")
            self.assertEqual(self.put(e, "companies/acme/other", env=env)[0], "deny")

    def test_a_page_that_stays_passes(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(**NEW_SETTINGS, gbrain_moved_keep=["^sessions/", "-journal$"])
            for slug in ("sessions/acme-1", "projects/acme-journal", "projects/acme-notes-journal"):
                self.assertEqual(self.put(e, slug)[0], "allow", slug)
            self.assertEqual(self.put(e, "projects/acme-notes")[0], "deny")

    def test_a_live_permit_lets_a_new_page_through(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(**NEW_SETTINGS, gbrain_move_permit_cli="my-permit grant --slugs {slug}")
            d, why = self.put(e, NEW)
            self.assertEqual(d, "deny")
            self.assertIn("grant a narrow expiring permit for the slug and repeat the call: my-permit grant --slugs " + NEW + ".", why)
            e.permit(["companies/acme"], expires=time.time() - 5, name="old")
            self.assertEqual(self.put(e, NEW)[0], "deny")                                          # expired
            e.permit(["companies/acme"])
            self.assertEqual((self.put(e, NEW)[0], self.put(e, "companies/acme")[0]), ("allow", "allow"))
            self.assertEqual(self.put(e, "projects/acme-site")[0], "deny")                         # another page is not covered

    def test_off_by_default(self):
        with temp_home() as home:
            e = Env(home)
            for slug, content in ((NEW, "x"), ("projects/acme-site", "x"), ("people/pat", ORG_PAGE)):
                self.assertEqual(self.put(e, slug, content)[0], "allow", slug)
                self.assertEqual(self.put(e, slug, content, tool="put_pages")[0], "allow", slug)
            e.setting(gbrain_newpage_patterns=[], gbrain_newpage_org="", gbrain_newpage_hint="unused")
            self.assertEqual(self.put(e, NEW)[0], "allow")

    def test_put_pages_checks_every_page_of_the_batch(self):
        with temp_home() as home:
            e = Env(home, dict(Env.default_pages(), **{"projects/acme-old": page()}))
            e.setting(**NEW_SETTINGS)
            d, why = self.verdict(e, GB + "put_pages", {"pages": [
                {"slug": LIVE, "content": "a"}, {"slug": "projects/acme-old", "content": "b"},
                {"slug": NEW, "content": "c"}, {"slug": "people/pat", "content": ORG_PAGE}, {"slug": "notes/new", "content": "d"}]})
            self.assertEqual(d, "deny")
            self.assertEqual(len(why.splitlines()), 2)                                            # the new two, not the old or the others
            self.assertIn(f"{NEW} is a new page of the other brain", why)
            self.assertIn("people/pat is a new page of the other brain", why)
            self.assertNotIn("projects/acme-old is", why)
            ok, _ = self.verdict(e, GB + "put_pages", {"pages": [{"slug": "projects/acme-old", "content": "b"},
                                                                  {"slug": "notes/new", "content": "d"}]})
            self.assertEqual(ok, "allow")

    def test_a_stub_and_a_moved_page_keep_their_own_refusals(self):
        with temp_home() as home:
            e = Env(home, dict(Env.default_pages(), **{"companies/acme": page(BANNER)}))
            e.setting(**NEW_SETTINGS)
            self.assertIn("has moved to another brain", self.put(e, "companies/acme")[1])
            self.assertIn("is a redirect to", self.put(e, STUB)[1])

    def test_fail_open_when_the_lookup_cannot_tell(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(**NEW_SETTINGS)
            nothing = dict(e.env, PSQL_BIN="/nonexistent/psql", GBRAIN_BIN="/nonexistent/gbrain")
            self.assertEqual(e.run(GB + "put_page", {"slug": NEW, "content": "x"}, env=nothing), "")
            e.set_pages(dict(e.default_pages(), _psql_fail=True, _cli_fail=True))                 # both fail: unknown is not new
            self.assertEqual(e.run(GB + "put_page", {"slug": NEW, "content": "x"}), "")
            e.set_pages(dict(e.default_pages(), _psql_fail=True, _sleep=3))                       # a stalled CLI is cut off
            env = dict(e.env, GBRAIN_REDIRECT_BUDGET="0.5")
            self.assertEqual(e.run(GB + "put_page", {"slug": NEW, "content": "x"}, env=env), "")
            # an invalid pattern is skipped with a note, the valid ones still work
            e.set_pages(e.default_pages())
            e.setting(gbrain_newpage_patterns=["(bad", "^companies/acme"])
            self.assertEqual(decision(e.run(GB + "put_page", {"slug": NEW, "content": "x"})), "deny")
            self.assertIn("gbrain_newpage_patterns", e.last_err)

    def test_portal_calls_are_checked(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(**NEW_SETTINGS)
            d, why = self.verdict(e, PX, code(f'gbrain_put_page({{slug: "{NEW}", content: "x"}})'))
            self.assertEqual(d, "deny")
            self.assertIn("the same put_page call", why)
            self.assertEqual(self.verdict(e, PX, code(f'other_put_page({{slug: "{NEW}", content: "x"}})'))[0], "allow")
            self.assertEqual(self.verdict(e, PX, code('gbrain_put_page({slug: s, content: "x"})'))[0], "allow")   # a computed slug


class Hardening(Guard):
    def test_slugs_are_lower_cased_for_the_lookup_the_permits_and_the_keep_list(self):
        with temp_home() as home:
            e = Env(home)
            for tool, inp in (("add_tag", {"slug": "Projects/ALPHA", "tag": "x"}), ("append_page", {"slug": " Projects/Alpha ", "text": "x"}),
                              ("put_pages", {"pages": [{"slug": "PROJECTS/Alpha/Contract", "content": "y"}]})):
                d, why = self.verdict(e, GB + tool, inp)
                self.assertEqual(d, "deny", tool)
                self.assertIn("projects/alpha", why)                                   # the lower-cased slug is the one named
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": "Projects/Gamma", "tag": "x"})[0], "allow")   # a live page
            # the CLI fallback and the keep list see the same slug, and so does a permit written with capitals or slashes
            env = dict(e.env, PSQL_BIN="/nonexistent/psql")
            self.assertEqual(self.verdict(e, GB + "append_page", {"slug": "Projects/Alpha", "text": "x"}, env=env)[0], "deny")
            e.permit(["Projects/Alpha/"])
            self.assertEqual(self.verdict(e, GB + "append_page", {"slug": "Projects/Alpha", "text": "x"})[0], "allow")
            self.assertEqual(self.verdict(e, GB + "append_page", {"slug": "projects/alpha/contract", "text": "x"})[0], "allow")
            e.setting(gbrain_moved_keep=["^projects/beta$"])
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": "Projects/BETA", "tag": "x"})[0], "allow")

    def test_a_pathological_pattern_cannot_hold_the_call(self):
        with temp_home() as home:
            slug = "projects/" + "a" * 41 + "!"
            e = Env(home, {slug: page(BANNER)})
            e.setting(gbrain_moved_keep=["(a+)+$"])
            env = dict(e.env, GBRAIN_REDIRECT_BUDGET="1")
            t = time.time()
            self.assertEqual(e.run(GB + "add_tag", {"slug": slug, "tag": "x"}, env=env), "")      # out of time: the call goes on
            self.assertLess(time.time() - t, 6)

    def test_a_very_long_slug_is_never_matched_against_a_setting(self):
        with temp_home() as home:
            slug = "projects/" + "a" * 400
            e = Env(home, {slug: page(BANNER)})
            e.setting(gbrain_moved_keep=[".*"], gbrain_newpage_patterns=[".*"])
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": slug, "tag": "x"})[0], "deny")   # the keep pattern was not run
            e.set_pages({})
            e.setting(gbrain_newpage_patterns=[".*"])
            self.assertEqual(self.verdict(e, GB + "put_page", {"slug": slug, "content": "x"})[0], "allow")   # nor the new-page one
            self.assertEqual(self.verdict(e, GB + "put_page", {"slug": "projects/short", "content": "x"})[0], "deny")   # a short one is

    def test_a_relative_xdg_state_home_is_ignored(self):
        with temp_home() as home:
            e = Env(home)
            e.permit([MOVED])                                                                    # in ~/.local/state
            env = dict(e.env, XDG_STATE_HOME="elsewhere")                                        # the hook runs in the home folder
            os.makedirs(os.path.join(home, "elsewhere", "gbrain-move-permits"))
            self.assertEqual(self.verdict(e, GB + "add_tag", {"slug": MOVED, "tag": "x"}, env=env)[0], "allow")

    def test_only_page_not_found_means_the_page_is_absent(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(**NEW_SETTINGS)
            put = lambda: e.run(GB + "put_page", {"slug": NEW, "content": "x"})  # noqa: E731
            e.set_pages(dict(e.default_pages(), _psql_fail=True))
            self.assertEqual(decision(put()), "deny")                                            # the CLI says page_not_found: new
            for rc in (0, 1):
                e.set_pages(dict(e.default_pages(), _psql_fail=True, _cli_msg="database not found", _cli_rc=rc))
                self.assertEqual(put(), "", rc)                                                  # another "not found": unknown, not new

    def test_a_failed_psql_leaves_one_note(self):
        with temp_home() as home:
            e = Env(home)
            e.set_pages(dict(e.default_pages(), _psql_fail=True))
            self.assertEqual(decision(e.run(GB + "add_tag", {"slug": STUB, "tag": "x"})), "deny")     # the CLI answers
            self.assertEqual(len([ln for ln in e.last_err.splitlines() if "psql exited 2" in ln]), 1)
            self.assertIn("gbrain_moved_banner", e.last_err)

    def test_a_lone_surrogate_in_a_slug_still_gets_the_deny(self):
        with temp_home() as home:
            slug = "projects/alpha\ud800"
            e = Env(home, {slug: page(BANNER)})
            env = dict(e.env, PSQL_BIN="/nonexistent/psql")
            ev = json.dumps({"tool_name": GB + "add_tag", "tool_input": {"slug": slug, "tag": "x"}})      # ASCII: "\\ud800"
            rc, out, err = run_hook("gbrain-redirect-guard.py", ev, env)
            self.assertEqual(rc, 0, err)
            self.assertEqual(decision(out), "deny")


class Portal(Guard):
    def test_codemode_writes_are_checked_on_the_listed_entries_only(self):
        with temp_home() as home:
            e = Env(home)
            ms = {"name": "acme portal"}
            scr = lambda *c: code(*c)  # noqa: E731
            d, why = self.verdict(e, PX, scr(f'gbrain_get_page({{slug: "{MOVED}"}})',
                                             f'gbrain_add_timeline_entry({{slug: "{MOVED}", date: "2030-01-15", summary: "x"}})'),
                                  mcp_server=ms)
            self.assertEqual(d, "deny")
            self.assertIn(f"{MOVED} has moved to another brain", why)
            self.assertIn("the same add_timeline_entry call", why)
            self.assertEqual(self.verdict(e, PX, scr(f'gbrain_get_page({{slug: "{MOVED}"}})'), mcp_server=ms)[0], "allow")
            self.assertEqual(self.verdict(e, PX, scr(f'gbrain_add_tag({{slug: "{STUB}", tag: "x"}})'), mcp_server=ms)[0], "deny")
            # another portal entry is another brain: never looked up here
            self.assertEqual(self.verdict(e, PX, scr(f'other_add_tag({{slug: "{MOVED}", tag: "x"}})'), mcp_server=ms)[0], "allow")
            # a computed slug is not looked up
            self.assertEqual(self.verdict(e, PX, scr('gbrain_add_tag({slug: s, tag: "x"})'), mcp_server=ms)[0], "allow")

    def test_gbrain_entries_is_a_setting(self):
        with temp_home() as home:
            e = Env(home)
            e.setting(gbrain_entries=["other"])
            self.assertEqual(self.verdict(e, PX, code(f'other_add_tag({{slug: "{MOVED}", tag: "x"}})'))[0], "deny")
            self.assertEqual(self.verdict(e, PX, code(f'gbrain_add_tag({{slug: "{MOVED}", tag: "x"}})'))[0], "allow")

    def test_a_permit_works_through_the_portal(self):
        with temp_home() as home:
            e = Env(home)
            c = code(f'gbrain_add_tag({{slug: "{MOVED}", tag: "x"}})')
            self.assertEqual(self.verdict(e, PX, c)[0], "deny")
            e.permit([MOVED])
            self.assertEqual(self.verdict(e, PX, c)[0], "allow")


if __name__ == "__main__":
    unittest.main()
