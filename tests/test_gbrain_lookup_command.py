"""The redirect guard hook, the setting gbrain_lookup_command: a brain whose database is not on this box is looked up by a command of the
owner's own. Set, the hook runs exactly that command and neither the local psql nor the gbrain CLI is ever consulted; any failure of
the command (missing, nonzero, too slow, no end line) tells the hook nothing and the call goes on; a command that outlives its time
is killed with everything it started. Unset, the lookup is the old one.

Run:  uv run --no-project python -m unittest tests.test_gbrain_lookup_command -v   (from the repository root; or discover -s tests)

The command is a stand-in that answers from the same fixture map as the other guard tests (test_gbrain_redirect_guard.py): it reads the
JSON on stdin, applies the banner pattern to each page body as the database would, prints the rows and then the end line it finds in the
script. The psql and gbrain stand-ins of the environment are replaced by ones that only leave a marker file, so a test can say they were
never run. Each test makes its own locked temp folder (tests/_support.py) and removes it when it ends. The one test that starts long
sleepers ends them by pid, and only a process whose command line holds its own temp folder; none is ever killed by name.
"""
import importlib.util
import json
import os
import signal
import sys
import time
import unittest

import _support as S
import test_gbrain_redirect_guard as G
from _support import decision, reason, temp_home

COMMAND_STANDIN = '''#!%(py)s
import json, os, re, sys, time
mp = json.load(open(%(map)r))
raw = sys.stdin.read()
open(%(stdin)r, "w").write(raw)
if mp.get("_cmd_sleep"):
    open(%(pids)r, "a").write(str(os.getpid()) + "\\n")
    if os.fork() == 0:                                  # a child of the command, in its group, that holds nothing open
        open(%(pids)r, "a").write(str(os.getpid()) + "\\n")
        time.sleep(60)
        sys.exit(0)
    time.sleep(60)
if mp.get("_cmd_fail"):
    sys.exit(mp["_cmd_fail"])
req = json.loads(raw)
rows = json.loads(req["variables"]["rows"])
rx = re.compile(req["variables"]["banner"])
for w in rows:
    p = mp.get(w["slug"])
    if not isinstance(p, dict):
        continue
    m = rx.search(p.get("body", ""))
    print("\\t".join([w["slug"], p.get("type", "note"), p.get("redirect_to", ""), m.group(1) if m else ""]))
end = re.search(r"select '(lookup-complete-[0-9a-f]+)';", req["sql"])
mode = mp.get("_cmd_end", "ok")
if mode == "ok":
    print(end.group(1))
elif mode == "canned":                                  # the end line of some other run
    print("lookup-complete-" + "0" * 32)
elif mode == "garbage":
    print("psql: error: connection refused")
elif mode == "text-after":
    print(end.group(1))
    print("a line after the end line")
elif mode == "short-row":
    print("projects/alpha\\tredirect")
    print(end.group(1))
# mode "none": nothing at all, exit 0
'''

MARKER_STANDIN = '''#!%(py)s
import sys
open(%(marker)r, "a").write(sys.argv[0] + "\\n")
sys.exit(1)
'''

NEW = G.NEW
NEW_SETTINGS = G.NEW_SETTINGS


class CmdEnv(G.Env):
    """The guard tests' world with the lookup command set and the psql and CLI stand-ins reduced to markers."""

    def __init__(self, home, pages=None, command="list", **extra):
        super().__init__(home, pages, **extra)
        self.stdin = os.path.join(home, "cmd-stdin.json")
        self.pids = os.path.join(home, "cmd-pids.txt")
        self.marker = os.path.join(home, "local-lookup-ran.txt")
        self.cmd = os.path.join(home, "lookup-cmd")
        with open(self.cmd, "w", encoding="utf-8") as f:
            f.write(COMMAND_STANDIN % {"py": sys.executable, "map": self.map, "stdin": self.stdin, "pids": self.pids})
        os.chmod(self.cmd, 0o755)
        for name in ("psql", "gbrain"):
            path = os.path.join(home, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(MARKER_STANDIN % {"py": sys.executable, "marker": self.marker})
        if command == "list":
            self.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"] = json.dumps([self.cmd])
        elif command is not None:
            self.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"] = command

    def local_lookup_ran(self):
        return os.path.exists(self.marker)

    def with_pages(self, **special):
        self.set_pages(dict(self.default_pages(), **special))


class CommandLookup(unittest.TestCase):
    def verdict(self, e, tool, inp, **kw):
        out = e.run(G.GB + tool, inp, **kw)
        S.assert_no_owner(self, out + e.last_err, "the hook output")
        return decision(out), reason(out)

    def test_the_command_is_run_and_the_local_lookups_never_are(self):
        with temp_home() as home:
            e = CmdEnv(home)
            self.assertEqual(self.verdict(e, "add_tag", {"slug": G.STUB, "tag": "x"})[0], "deny")
            self.assertEqual(self.verdict(e, "add_tag", {"slug": G.LIVE, "tag": "x"})[0], "allow")
            self.assertTrue(os.path.exists(e.stdin))
            self.assertFalse(e.local_lookup_ran(), "the local psql or the gbrain CLI was run")

    def test_what_the_command_gets_on_stdin(self):
        with temp_home() as home:
            e = CmdEnv(home)
            e.run(G.GB + "add_tag", {"slug": "Projects/Alpha", "tag": "x", "source_id": "src1"})
            req = json.loads(S.read(e.stdin))
            self.assertEqual(sorted(req), ["sql", "variables"])
            self.assertEqual(sorted(req["variables"]), ["banner", "rows"])
            self.assertEqual(json.loads(req["variables"]["rows"]), [{"slug": "projects/alpha", "src": "src1"}])
            self.assertIn("begin transaction read only;", req["sql"])
            self.assertIn(":'rows'", req["sql"])
            self.assertIn(":'banner'", req["sql"])
            self.assertRegex(req["sql"], r"\nselect 'lookup-complete-[0-9a-f]{32}';\n$")
            self.assertTrue(req["variables"]["banner"].startswith("(?:^|\\n)("))

    def test_the_end_line_is_new_on_every_run(self):
        with temp_home() as home:
            e = CmdEnv(home)
            e.run(G.GB + "add_tag", {"slug": G.LIVE, "tag": "x"})
            first = json.loads(S.read(e.stdin))["sql"]
            e.run(G.GB + "add_tag", {"slug": G.LIVE, "tag": "x"})
            self.assertNotEqual(first, json.loads(S.read(e.stdin))["sql"])

    def test_a_redirect_stub_is_refused_naming_its_target(self):
        with temp_home() as home:
            e = CmdEnv(home)
            d, why = self.verdict(e, "add_timeline_entry", {"slug": G.STUB, "date": "2030-01-15", "summary": "x"})
            self.assertEqual(d, "deny")
            self.assertTrue(why.startswith(f"{G.STUB} is a redirect to {G.TARGET}: write to {G.TARGET} instead."))
            self.assertFalse(e.local_lookup_ran())

    def test_a_moved_page_is_refused_and_a_permit_lets_it_through(self):
        with temp_home() as home:
            e = CmdEnv(home)
            d, why = self.verdict(e, "append_page", {"slug": G.MOVED, "text": "x"})
            self.assertEqual(d, "deny")
            self.assertIn(f"{G.MOVED} has moved to another brain", why)
            e.permit([G.MOVED])
            self.assertEqual(self.verdict(e, "append_page", {"slug": G.MOVED, "text": "x"})[0], "allow")
            self.assertFalse(e.local_lookup_ran())

    def test_a_new_page_of_the_other_brain_is_refused_an_existing_one_is_not(self):
        with temp_home() as home:
            e = CmdEnv(home)
            e.setting(**NEW_SETTINGS)
            d, why = self.verdict(e, "put_page", {"slug": NEW, "content": "x"})
            self.assertEqual(d, "deny")
            self.assertIn(f"{NEW} is a new page of the other brain", why)
            e.with_pages(**{NEW: G.page()})
            self.assertEqual(self.verdict(e, "put_page", {"slug": NEW, "content": "x"})[0], "allow")
            self.assertEqual(self.verdict(e, "put_page", {"slug": "projects/ordinary-new", "content": "x"})[0], "allow")
            self.assertFalse(e.local_lookup_ran())

    def test_every_failure_of_the_command_lets_the_call_go_on(self):
        """The dangerous one is the new page: a command that told nothing must never read as 'the page does not exist', nor must a
        stub or a moved page be refused on what was not seen."""
        with temp_home() as home:
            e = CmdEnv(home)
            e.setting(**NEW_SETTINGS)
            bad = [("exits nonzero", {"_cmd_fail": 3}), ("exits 255, as ssh does", {"_cmd_fail": 255}),
                   ("exits 0 with no output", {"_cmd_end": "none"}), ("no end line, rows only", {"_cmd_end": "garbage"}),
                   ("the end line of another run", {"_cmd_end": "canned"}), ("a line after the end line", {"_cmd_end": "text-after"}),
                   ("a row of two columns", {"_cmd_end": "short-row"})]
            for what, extra in bad:
                e.with_pages(**extra)
                for tool, inp in (("put_page", {"slug": NEW, "content": "x"}), ("add_tag", {"slug": G.STUB, "tag": "x"}),
                                  ("add_tag", {"slug": G.MOVED, "tag": "x"})):
                    self.assertEqual(self.verdict(e, tool, inp)[0], "allow", f"{what}: {tool} {inp['slug']}")
            self.assertFalse(e.local_lookup_ran())

    def test_a_missing_or_unusable_command_fails_open_and_reads_no_local_database(self):
        with temp_home() as home:
            e = CmdEnv(home, command=None)
            e.setting(**NEW_SETTINGS)
            for command in (json.dumps([os.path.join(home, "no-such-command")]), json.dumps([os.path.join(home, "pages.json")]),
                            json.dumps([""]), json.dumps([1, 2]), "'unterminated", "[1]"):
                e.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"] = command
                for tool, inp in (("put_page", {"slug": NEW, "content": "x"}), ("add_tag", {"slug": G.STUB, "tag": "x"})):
                    self.assertEqual(self.verdict(e, tool, inp)[0], "allow", f"{command}: {inp['slug']}")
                self.assertFalse(e.local_lookup_ran(), command)

    def test_a_command_that_is_too_slow_is_ended_and_the_call_goes_on(self):
        with temp_home() as home:
            e = CmdEnv(home)
            e.with_pages(_cmd_sleep=True)
            env = dict(e.env, GBRAIN_REDIRECT_BUDGET="1")
            t = time.time()
            try:
                self.assertEqual(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"}, env=env), "")
                took = time.time() - t
                self.assertLess(took, 4)
                pids = self.pids_of(e)
                self.assertEqual(len(pids), 2, "the stand-in and its child both started")
                time.sleep(0.3)
                self.assertEqual([p for p in pids if self.alive(p)], [], "the command or its child outlived the hook")
            finally:
                self.end_leftovers(e, home)

    def test_the_hooks_own_timer_ends_the_command_and_its_child_too(self):
        """The timer (BUDGET + 1 s) raises SystemExit wherever the hook is: here inside the wait for the command."""
        with temp_home() as home:
            e = CmdEnv(home)
            e.with_pages(_cmd_sleep=True)
            spec = importlib.util.spec_from_file_location("guard_under_test", os.path.join(S.HOOKS, "gbrain-redirect-guard.py"))
            mod = importlib.util.module_from_spec(spec)
            old_home, old_env = os.environ.get("HOME"), dict(os.environ)
            os.environ.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")})
            sys.path.insert(0, S.HOOKS)
            try:
                spec.loader.exec_module(mod)
                handler = signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
                signal.setitimer(signal.ITIMER_REAL, 0.6)
                try:
                    with self.assertRaises(SystemExit):
                        mod.lookup_command([("projects/x", None, "add_tag", None)], time.time() + 30, "b", [e.cmd])
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, handler)
                pids = self.pids_of(e)
                self.assertEqual(len(pids), 2)
                time.sleep(0.3)
                self.assertEqual([p for p in pids if self.alive(p)], [], "the command or its child outlived the hook")
            finally:
                sys.path.remove(S.HOOKS)
                os.environ.clear()
                os.environ.update(old_env)
                self.end_leftovers(e, home)

    def test_the_setting_forms_and_the_home_in_the_first_word(self):
        with temp_home() as home:
            e = CmdEnv(home, command=None)
            tilde = "~/lookup-cmd"
            for how, value in (("a plain string", tilde), ("a plain string with an argument", tilde + " --live"),
                               ("a JSON list", json.dumps([tilde]))):
                e.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"] = value
                self.assertEqual(self.verdict(e, "add_tag", {"slug": G.STUB, "tag": "x"})[0], "deny", how)
            del e.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"]
            e.setting(gbrain_lookup_command=[tilde])                                          # the file key
            self.assertEqual(self.verdict(e, "add_tag", {"slug": G.STUB, "tag": "x"})[0], "deny", "plugin.json")
            self.assertFalse(e.local_lookup_ran())

    def test_works_through_the_portal(self):
        with temp_home() as home:
            e = CmdEnv(home)
            out = e.run(G.PX, G.code(f'gbrain_add_tag({{slug: "{G.MOVED}", tag: "x"}})'), mcp_server={"name": "acme portal"})
            self.assertEqual(decision(out), "deny")
            self.assertFalse(e.local_lookup_ran())

    # -- helpers
    @staticmethod
    def pids_of(e):
        try:
            return [int(x) for x in S.read(e.pids).split()]
        except (OSError, ValueError):
            return []

    @staticmethod
    def alive(pid):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"               # a zombie is gone as far as the hook is concerned
        except OSError:
            return False

    def end_leftovers(self, e, home):
        """End a sleeper of this test by pid, never by name, and only while its command line holds this test's own folder."""
        for pid in self.pids_of(e):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    mine = home.encode() in f.read()
                if mine:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class Unset(unittest.TestCase):
    def test_unset_the_local_lookup_is_the_one_used_and_the_command_is_not_involved(self):
        with temp_home() as home:
            e = G.Env(home)                                                                      # the usual stand-ins, no setting
            out = e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"})
            self.assertEqual(decision(out), "deny")
            e.setting(gbrain_lookup_command=[])                                                  # empty means unset
            self.assertEqual(decision(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"})), "deny")
            e.setting(gbrain_lookup_command="")
            self.assertEqual(decision(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"})), "deny")

    def test_the_local_lookup_script_has_no_end_line(self):
        spec = importlib.util.spec_from_file_location("guard_unset", os.path.join(S.HOOKS, "gbrain-redirect-guard.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, S.HOOKS)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(S.HOOKS)
        self.assertNotIn("lookup-complete", mod.lookup_sql())
        self.assertTrue(mod.lookup_sql("lookup-complete-ab").endswith("commit;\nselect 'lookup-complete-ab';\n"))


if __name__ == "__main__":
    unittest.main()
