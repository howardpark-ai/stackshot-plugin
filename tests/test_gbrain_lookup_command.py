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

The stand-in also records its argv and its working folder, writes noise to stderr, prints more than the hook will read, and exits
nonzero after a full answer, each on the request of a key of the pages map, so a test can say how the hook ran it and what it did
with what it printed.
"""
import contextlib
import importlib.util
import io
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
json.dump(sys.argv[1:], open(%(argv)r, "w"))
open(%(cwd)r, "w").write(os.getcwd())
if mp.get("_cmd_stderr"):                               # noise on stderr, more than a pipe holds: it must go nowhere
    sys.stderr.write("warning: noise before the answer\\n" * 8000)
    sys.stderr.flush()


def pad(nbytes):                                        # rows of pages nobody asked for, valid as rows
    done, i = 0, 0
    while done < nbytes:
        row = "pad/" + str(i) + "\\tnote\\t\\t\\n"
        sys.stdout.write(row)
        done += len(row)
        i += 1
    sys.stdout.flush()


if mp.get("_cmd_spew"):                                 # prints without end and without reading stdin; a closed pipe does not end it
    open(%(pids)r, "a").write(str(os.getpid()) + "\\n")
    try:
        while True:
            pad(65536)
    except BrokenPipeError:
        time.sleep(60)
if mp.get("_cmd_pad_first"):                            # prints before it reads stdin: a hook that only writes first would hang
    pad(mp["_cmd_pad_first"])
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
mode = mp.get("_cmd_end", "ok")
for w in rows:
    p = mp.get(w["slug"])
    if not isinstance(p, dict):
        continue
    m = rx.search(p.get("body", ""))
    if mode == "short-rows":                            # the page is there, its row has two columns
        print(w["slug"] + "\\tnote")
    else:
        print("\\t".join([w["slug"], p.get("type", "note"), p.get("redirect_to", ""), m.group(1) if m else ""]))
end = re.search(r"select '(lookup-complete-[0-9a-f]+)';", req["sql"])
if mode in ("ok", "short-rows"):
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
if mp.get("_cmd_exit_after"):                           # a full, valid answer, then a failure status
    sys.stdout.flush()
    sys.exit(mp["_cmd_exit_after"])
'''

MARKER_STANDIN = '''#!%(py)s
import sys
open(%(marker)r, "a").write(sys.argv[0] + "\\n")
sys.exit(1)
'''

NEW = G.NEW
NEW_SETTINGS = G.NEW_SETTINGS
CAP = 1 << 20                                                                        # the most stdout the hook reads from the command


@contextlib.contextmanager
def guard_module(home):
    """The hook's module, imported under `home` (HOME and XDG_CONFIG_HOME), for a test that calls lookup_command itself."""
    spec = importlib.util.spec_from_file_location("guard_under_test", os.path.join(S.HOOKS, "gbrain-redirect-guard.py"))
    mod = importlib.util.module_from_spec(spec)
    saved = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME")}
    os.environ.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")})
    sys.path.insert(0, S.HOOKS)
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.path.remove(S.HOOKS)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def alarm(seconds):
    """Fail the test (TimeoutError) when the block runs longer: a hook that hangs must not hang the suite."""
    def fire(*_):
        raise TimeoutError("the hook did not return in time")
    old = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


class CmdEnv(G.Env):
    """The guard tests' world with the lookup command set and the psql and CLI stand-ins reduced to markers."""

    def __init__(self, home, pages=None, command="list", cmd_folder="", **extra):
        super().__init__(home, pages, **extra)
        self.stdin = os.path.join(home, "cmd-stdin.json")
        self.pids = os.path.join(home, "cmd-pids.txt")
        self.argv = os.path.join(home, "cmd-argv.json")
        self.cwd = os.path.join(home, "cmd-cwd.txt")
        self.marker = os.path.join(home, "local-lookup-ran.txt")
        self.cmd = os.path.join(home, cmd_folder, "lookup-cmd")                      # a folder name may hold spaces and shell characters
        os.makedirs(os.path.dirname(self.cmd), exist_ok=True)
        with open(self.cmd, "w", encoding="utf-8") as f:
            f.write(COMMAND_STANDIN % {"py": sys.executable, "map": self.map, "stdin": self.stdin, "pids": self.pids,
                                       "argv": self.argv, "cwd": self.cwd})
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

    def run_in(self, cwd, tool, inp, env=None):
        """The hook run with `cwd` as its working folder (Claude Code runs a hook in the project folder)."""
        ev = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp, "cwd": cwd}
        rc, out, err = S.run_hook("gbrain-redirect-guard.py", ev, env or self.env, cwd=cwd)
        assert rc == 0, err
        self.last_err = err
        return out

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
                self.assertLess(took, 1.7, "the command's own deadline (the budget) ended it, not the hook's timer at budget + 1 s")
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

    def test_the_command_is_run_without_a_shell(self):
        """A folder and arguments full of spaces and shell characters reach the command as they are: nothing is split, expanded or run."""
        with temp_home() as home:
            odd = "odd dir; touch HITA; $(touch HITB) `touch HITC` 'q' & \"d\" > HITD"
            e = CmdEnv(home, command=None, cmd_folder=odd)
            args = ["--opt=a b", "x;touch HITE", "$(touch HITF)", "it's", "*", "~/not-a-home-path"]
            e.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"] = json.dumps([e.cmd] + args)
            self.assertEqual(self.verdict(e, "mcp__acme_GBrain__add_tag", {"slug": G.STUB, "tag": "x"})[0], "deny")
            self.assertEqual(json.loads(S.read(e.argv)), args)
            self.assertEqual([n for n in os.listdir(home) if n.startswith("HIT")], [], "a shell ran part of the command text")
            self.assertFalse(e.local_lookup_ran())

    def test_the_command_runs_in_the_home_folder_not_the_project_folder(self):
        with temp_home() as home:
            e = CmdEnv(home)
            project = os.path.join(home, "project")
            os.makedirs(project)
            self.assertEqual(decision(e.run_in(project, G.GB + "add_tag", {"slug": G.STUB, "tag": "x"})), "deny")
            self.assertEqual(os.path.realpath(S.read(e.cwd)), os.path.realpath(home))

    def test_the_setting_is_read_from_the_user_config_never_from_the_project_folder(self):
        """A project folder that holds a plugin.json (in any of the usual places) does not set the command: the default lookup runs."""
        with temp_home() as home:
            e = CmdEnv(home, command=None)
            project = os.path.join(home, "project")
            for rel in ("plugin.json", os.path.join(".claude", "plugin.json"), os.path.join("stackshot", "plugin.json"),
                        os.path.join(".config", "stackshot", "plugin.json"), os.path.join(".claude", "stackshot", "plugin.json")):
                os.makedirs(os.path.dirname(os.path.join(project, rel)), exist_ok=True)
                with open(os.path.join(project, rel), "w", encoding="utf-8") as f:
                    json.dump({"gbrain_lookup_command": [e.cmd]}, f)
            self.assertEqual(decision(e.run_in(project, G.GB + "add_tag", {"slug": G.STUB, "tag": "x"})), "allow")
            self.assertFalse(os.path.exists(e.stdin), "the command of a project folder was run")
            self.assertTrue(e.local_lookup_ran(), "the default lookup was not the one used")

    def test_a_nonzero_exit_after_a_full_valid_answer_is_a_failure(self):
        with temp_home() as home:
            e = CmdEnv(home)
            self.assertEqual(self.verdict(e, "add_tag", {"slug": G.STUB, "tag": "x"})[0], "deny", "the answer alone is valid")
            for status in (1, 255):
                e.with_pages(_cmd_exit_after=status)
                for slug in (G.STUB, G.MOVED):
                    self.assertEqual(self.verdict(e, "add_tag", {"slug": slug, "tag": "x"})[0], "allow", f"exit {status}: {slug}")

    def test_the_commands_stderr_is_discarded_and_never_part_of_the_answer(self):
        with temp_home() as home:
            e = CmdEnv(home)
            e.with_pages(_cmd_stderr=True)                                       # 200 KB of noise: more than a pipe holds
            self.assertEqual(self.verdict(e, "add_tag", {"slug": G.STUB, "tag": "x"})[0], "deny")
            self.assertEqual(self.verdict(e, "add_tag", {"slug": G.LIVE, "tag": "x"})[0], "allow")
            self.assertEqual(e.last_err, "", "the command's stderr reached the hook's own")

    def test_stdout_is_read_up_to_one_megabyte_and_no_further(self):
        with temp_home() as home:
            e = CmdEnv(home)
            try:
                e.with_pages(_cmd_pad_first=CAP - 150_000)                       # a long answer under the cap is an answer
                self.assertEqual(self.verdict(e, "add_tag", {"slug": G.STUB, "tag": "x"})[0], "deny")
                self.assertEqual(e.last_err, "")
                e.with_pages(_cmd_pad_first=CAP + 250_000)                       # more than the cap is a failure, however valid it reads
                self.assertEqual(self.verdict(e, "add_tag", {"slug": G.STUB, "tag": "x"})[0], "allow")
                self.assertIn(f"printed more than {CAP} bytes", e.last_err)
                e.with_pages(_cmd_spew=True)                                     # one that never stops: ended at the cap, not at the deadline
                env = dict(e.env, GBRAIN_REDIRECT_BUDGET="5")
                t = time.time()
                self.assertEqual(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"}, env=env), "")
                self.assertLess(time.time() - t, 3.5)
                self.assertIn(f"printed more than {CAP} bytes", e.last_err)
                pids = self.pids_of(e)
                self.assertEqual(len(pids), 1)
                time.sleep(0.3)
                self.assertEqual([p for p in pids if self.alive(p)], [], "the command outlived the hook")
            finally:
                self.end_leftovers(e, home)

    def test_a_request_bigger_than_a_pipe_and_a_reply_started_early_do_not_hang_the_hook(self):
        """The command prints 200 KB before it reads a byte of its 280 KB request: a hook that writes first and reads later hangs."""
        with temp_home() as home:
            e = CmdEnv(home)
            e.with_pages(_cmd_pad_first=200_000)
            pairs = [("projects/slug-%05d" % i, None, "add_tag", None) for i in range(6000)]
            with guard_module(home) as mod, alarm(8):
                t = time.time()
                found = mod.lookup_command(pairs, time.time() + 6, "b", [e.cmd])
                took = time.time() - t
            self.assertLess(took, 4)
            self.assertEqual(len(json.loads(json.loads(S.read(e.stdin))["variables"]["rows"])), 6000, "the whole request arrived")
            self.assertFalse(found["projects/slug-00000"]["exists"])
            self.assertEqual(len([x for x in found if x.startswith("projects/slug-")]), 6000)

    def test_the_command_has_its_own_deadline_before_the_hooks_timer(self):
        """lookup_command returns at the deadline it is given, with nothing to end it from outside (the hook's timer is not set here)."""
        with temp_home() as home:
            e = CmdEnv(home)
            e.with_pages(_cmd_sleep=True)
            try:
                with guard_module(home) as mod, alarm(4), contextlib.redirect_stderr(io.StringIO()):
                    t = time.time()
                    found = mod.lookup_command([("projects/x", None, "add_tag", None)], time.time() + 1.0, "b", [e.cmd])
                    took = time.time() - t
                self.assertEqual(found, {})
                self.assertLess(took, 1.7)
                pids = self.pids_of(e)
                time.sleep(0.3)
                self.assertEqual([p for p in pids if self.alive(p)], [], "the command or its child outlived the hook")
            finally:
                self.end_leftovers(e, home)

    def test_one_short_row_makes_the_whole_answer_unusable_it_is_not_skipped(self):
        """A skipped row would leave the page it names as not-there, and the new-page check would refuse a page that exists."""
        with temp_home() as home:
            e = CmdEnv(home)
            e.setting(**NEW_SETTINGS)
            put = {"slug": NEW, "content": "x"}
            e.with_pages(**{NEW: G.page(), "_cmd_end": "short-rows"})            # the page exists; its row has two columns
            self.assertEqual(self.verdict(e, "put_page", put)[0], "allow", "a page that exists was refused on a short row")
            self.assertIn("fewer than three columns", e.last_err)
            e.with_pages(**{NEW: G.page()})                                      # the same page, a full row
            self.assertEqual(self.verdict(e, "put_page", put)[0], "allow")
            e.with_pages()                                                       # a page that is not there: the check is armed
            self.assertEqual(self.verdict(e, "put_page", put)[0], "deny")

    def test_each_failure_leaves_one_short_note_that_names_the_reason_only(self):
        with temp_home() as home:
            e = CmdEnv(home)
            budget = dict(e.env, GBRAIN_REDIRECT_BUDGET="0.5")
            cases = [("nonzero exit", {"_cmd_fail": 3}, "exited with status 3", None),
                     ("noise and a nonzero exit", {"_cmd_fail": 2, "_cmd_stderr": True}, "exited with status 2", None),
                     ("a full answer, then a nonzero exit", {"_cmd_exit_after": 7}, "exited with status 7", None),
                     ("no output", {"_cmd_end": "none"}, "printed no end line", None),
                     ("rows without the end line", {"_cmd_end": "garbage"}, "printed no end line", None),
                     ("the end line of another run", {"_cmd_end": "canned"}, "printed no end line", None),
                     ("a line after the end line", {"_cmd_end": "text-after"}, "printed no end line", None),
                     ("a short row", {"_cmd_end": "short-row"}, "fewer than three columns", None),
                     ("too much output", {"_cmd_pad_first": CAP + 250_000}, f"printed more than {CAP} bytes", None),
                     ("too slow", {"_cmd_sleep": True}, "ran out of time", budget)]
            try:
                for what, extra, phrase, env in cases:
                    e.with_pages(**extra)
                    self.assertEqual(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"}, env=env), "", what)
                    self.assert_one_note(e, phrase, what)
                e.with_pages()
                for what, command in (("a command that is not there", os.path.join(home, "no-such-command")),
                                      ("a file that is not executable", e.map)):
                    e.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"] = json.dumps([command])
                    self.assertEqual(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"}), "", what)
                    self.assert_one_note(e, "was not found, is not executable or could not be started", what)
            finally:
                self.end_leftovers(e, home)

    def test_a_setting_that_is_not_a_command_is_noted_and_reads_no_local_database(self):
        with temp_home() as home:
            e = CmdEnv(home, command=None)
            for value in (0, {}, True, [1, 2], ["a", 1], [""], ["  ", "x"]):               # the file's values
                e.setting(gbrain_lookup_command=value)
                self.assertEqual(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"}), "", repr(value))
                self.assert_one_note(e, "the setting gbrain_lookup_command is not a command", repr(value))
                self.assertFalse(e.local_lookup_ran(), repr(value))
            os.remove(os.path.join(home, ".config", "stackshot", "plugin.json"))
            for text in ("'unterminated", '""', "[1]", "{}"):                              # the environment's values
                e.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"] = text
                self.assertEqual(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"}), "", text)
                self.assert_one_note(e, "the setting gbrain_lookup_command is not a command", text)
                self.assertFalse(e.local_lookup_ran(), text)

    def assert_one_note(self, e, phrase, what):
        lines = e.last_err.splitlines()
        self.assertEqual(len(lines), 1, f"{what}: {lines!r}")
        self.assertRegex(lines[0], r"^stackshot-plugin: [a-z-]+: the (lookup command|setting gbrain_lookup_command) ", what)
        self.assertIn(phrase, lines[0], what)
        for secret in (G.STUB, "notes/", "pad/", "noise", "lookup-complete", "rows", "banner"):
            self.assertNotIn(secret, lines[0], f"{what}: the note carries {secret!r}")

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

    def test_false_null_and_blank_mean_unset_and_the_local_lookup_is_used(self):
        with temp_home() as home:
            e = G.Env(home)
            for value in (False, None, [], "", "   ", "\t\n"):                                  # the file's values
                e.setting(gbrain_lookup_command=value)
                self.assertEqual(decision(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"})), "deny", repr(value))
                self.assertEqual(e.last_err, "", repr(value))
            os.remove(os.path.join(home, ".config", "stackshot", "plugin.json"))
            for text in ("false", "null", "[]"):                                                 # the environment's values
                e.env["STACKSHOT_PLUGIN_GBRAIN_LOOKUP_COMMAND"] = text
                self.assertEqual(decision(e.run(G.GB + "add_tag", {"slug": G.STUB, "tag": "x"})), "deny", text)
                self.assertEqual(e.last_err, "", text)

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
