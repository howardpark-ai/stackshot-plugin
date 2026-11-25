"""post-tool-note: after a session creates a pull request or an issue, the hook anchors it through stackshot's machine client and says
so in one line; whatever goes wrong, it falls back to the older note with the reason after it.

The hook runs as the CLI runs it (hook JSON on stdin). The session's name (BUILD_STOPS_NAME), the roles file, the state folder
(XDG_STATE_HOME) and a stand-in stackshot checkout whose fleet/client.py answers `get open-records` from a file and records every call
(arguments and stdin) are made under a temp home, locked while the test runs and removed after, pass or fail. The only processes started
are the hook and the stand-in client; nothing is killed, nothing touches the network, tmux or the real home directory. A real call to a
server is never made: the client is always the stand-in."""
import fcntl
import hashlib
import importlib.util
import itertools
import json
import multiprocessing
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _support as S
from _support import context, env_for, temp_home

SCRIPT = os.path.join(S.HOOKS, "post-tool-note.py")
ME = "widgets-builder"
PROJECT = "projects/example"
PR = "https://github.com/acme/widgets/pull/7"
ISSUE = "https://github.com/acme/widgets/issues/12"
NOTE_TAG = "(post-tool-note.py)"
OLDER = "Created {url}. Anchor it now: stackshot anchors(project=..., add=[{url}]) " + NOTE_TAG + "."
NOT_FROM_CREATE = "the hook could not tell from the call's shape or output that the link came from the create call itself"
OTHER_PR = "https://github.com/acme/widgets/pull/99"
EVIL = "https://github.com/evil-org/evil-repo/pull/99"        # a link the create call never printed

# The stand-in for stackshot's client.py: control files sit beside $PTN_OUT.
FAKE_CLIENT = '''import json, os, sys, time
out = os.environ["PTN_OUT"]
args = sys.argv[1:]
with open(out + ".calls", "a") as f:
    f.write(json.dumps(args) + "\\n")
def ctl(name):
    p = out + "." + name
    return open(p).read() if os.path.exists(p) else None
if args[:2] == ["get", "open-records"]:
    if ctl("sleep.get"):
        with open(out + ".pid", "w") as f:
            f.write(str(os.getpid()))
        if ctl("grandchild"):                          # a process the client started: it shares the client's process group
            import subprocess
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", out + ".grandchild"])
            with open(out + ".gpid", "w") as f:
                f.write(str(g.pid))
        time.sleep(float(ctl("sleep.get")))
    if ctl("get.rc") not in (None, "0"):
        print(ctl("get.out") or "[401] no token on this machine")
        sys.exit(int(ctl("get.rc")))
    print(ctl("get.out") if ctl("get.out") is not None else '{"session": "x", "total": 0, "truncated": false, "unreadable": [], "items": []}')
    sys.exit(0)
if args[:3] == ["post", "anchor-add", "-"]:
    body = sys.stdin.read()
    with open(out + ".posted", "a") as f:
        f.write(body + "\\n=====\\n")
    if ctl("sleep.post"):
        time.sleep(float(ctl("sleep.post")))
    if ctl("refuse.items") and '"items"' in body:      # the server refuses a call that names an item
        sys.stderr.write(ctl("refuse.items"))
        sys.exit(1)
    if ctl("post.rc") not in (None, "0"):
        sys.stderr.write(ctl("post.err") or "stackshot: anchors not added [400] Refused: nope")
        sys.exit(int(ctl("post.rc")))
    print("anchors of the project")
    sys.exit(0)
sys.exit(2)
'''


def record(item_id, shown, mover, next_text, **meta):
    """One row of `get open-records`, as the route documents it: {id, shown, record: {subject, status, metadata}, anchors}."""
    return {"id": item_id, "shown": shown, "record": {"subject": "an item", "status": "open",
                                                       "metadata": {"mover": mover, "next": next_text, "project": PROJECT, **meta}}, "anchors": []}


def answer(*items, **extra):
    return json.dumps({"session": ME, "total": len(items), "truncated": False, "unreadable": [], "items": list(items), **extra})


def gh_event(url, command="gh pr create --title t --body b", session="sess-1", trailing=""):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": {"stdout": url + trailing + "\n"},
            "session_id": session, "hook_event_name": "PostToolUse"}


class Base(unittest.TestCase):
    def setUp(self):
        cm = temp_home()
        self.home = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.roles = os.path.join(self.home, "roles.json")
        self.write(self.roles, json.dumps({"sessions": {ME: {"role": "back", "project": PROJECT}}, "ids": {}}))
        self.out = os.path.join(self.home, "client")
        self.checkout = self.make_checkout()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def make_checkout(self, with_client=True):
        d = os.path.join(self.home, "stackshot" if with_client else "stackshot-without-client")
        os.makedirs(os.path.join(d, "stackshot", "fleet"))
        for init in (os.path.join(d, "stackshot", "__init__.py"), os.path.join(d, "stackshot", "fleet", "__init__.py")):
            self.write(init, "")
        if with_client:
            self.write(os.path.join(d, "stackshot", "fleet", "client.py"), FAKE_CLIENT)
        return d

    def ctl(self, name, text):
        self.write(f"{self.out}.{name}", text)

    def env(self, name=ME, **extra):
        env = env_for(self.home, BUILD_STOPS_ROLES=self.roles, BUILD_STOPS_PROJECTS=os.path.join(self.home, "no-projects.json"),
                      SALA_HEARTBEAT_STATE_DIR=os.path.join(self.home, "no-registry-store"), STACKSHOT_DIR=self.checkout,
                      PTN_OUT=self.out, POST_TOOL_NOTE_CALL_TIMEOUT_S="1.5")
        if name is not None:
            env["BUILD_STOPS_NAME"] = name
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def run_hook(self, ev, **env_extra):
        r = subprocess.run([sys.executable, SCRIPT], input=json.dumps(ev).encode(), capture_output=True, env=self.env(**env_extra),
                           timeout=30, cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        out = r.stdout.decode()
        S.assert_no_owner(self, out + r.stderr.decode(), "the hook output")
        return context(out)

    def calls(self):
        p = self.out + ".calls"
        return [json.loads(x) for x in S.read(p).splitlines()] if os.path.exists(p) else []

    def posted(self):
        p = self.out + ".posted"
        return [json.loads(x) for x in S.read(p).split("=====") if x.strip()] if os.path.exists(p) else []


class Anchoring(Base):
    def test_one_matching_item_is_picked_for_a_pull_request(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: finish the build\nthe-owner: look at it")))
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, f"Anchored {PR} to item-7 (your only open item as mover, with one open step of yours); nothing more is needed. {NOTE_TAG}")
        self.assertEqual(self.calls(), [["get", "open-records", ME], ["post", "anchor-add", "-"]])
        self.assertEqual(self.posted(), [{"project": PROJECT, "add": [PR], "sender": ME, "items": ["a7"]}])

    def test_an_issue_is_anchored_the_same_way(self):
        self.ctl("get.out", answer(record("a9", "item-9", ME, f"**{ME} (builder):** fix it")))
        note = self.run_hook(gh_event(ISSUE, command="gh issue create --title t --body b"))
        self.assertIn(f"Anchored {ISSUE} to item-9", note)
        self.assertEqual(self.posted(), [{"project": PROJECT, "add": [ISSUE], "sender": ME, "items": ["a9"]}])

    def test_the_connector_route_is_anchored_too(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"- {ME}: step")))
        ev = {"tool_name": "mcp__github__create_pull_request", "tool_input": {"owner": "acme", "repo": "widgets", "title": "t"},
              "tool_response": {"content": [{"type": "text", "text": "created " + PR}]}, "session_id": "sess-1"}
        self.assertIn(f"Anchored {PR} to item-7", self.run_hook(ev))
        self.assertEqual(self.posted()[0]["add"], [PR])

    def test_items_of_other_movers_are_ignored(self):
        self.ctl("get.out", answer(record("a1", "item-1", "someone-else", "someone-else: a step"),
                                   record("a7", "item-7", ME.upper(), f"{ME}: the step"),
                                   record("a2", "item-2", "the-owner", "the-owner: decide")))
        self.assertIn("to item-7", self.run_hook(gh_event(PR)))
        self.assertEqual(self.posted()[0]["items"], ["a7"])

    def test_zero_items_make_a_project_wide_anchor(self):
        self.ctl("get.out", answer(record("a1", "item-1", "someone-else", "someone-else: a step")))
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (no open item names you as its mover); not tied to a board item. {NOTE_TAG}")
        self.assertEqual(self.posted(), [{"project": PROJECT, "add": [PR], "sender": ME}])

    def test_an_empty_list_is_zero_items_too(self):
        note = self.run_hook(gh_event(PR))
        self.assertIn("project-wide", note)
        self.assertIn("no open item names you", note)
        self.assertNotIn("items", self.posted()[0])

    def test_several_items_make_a_project_wide_anchor(self):
        self.ctl("get.out", answer(record("a1", "item-1", ME, f"{ME}: one"), record("a2", "item-2", ME, f"{ME}: two")))
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (2 open items name you as their mover); not tied to a board item. {NOTE_TAG}")
        self.assertNotIn("nothing more is needed", note)
        self.assertNotIn("items", self.posted()[0])

    def test_one_item_needs_exactly_one_open_step_of_the_sessions_own(self):
        self.ctl("get.out", answer(record("a1", "item-1", ME, "the-owner: decide")))
        note = self.run_hook(gh_event(PR))
        self.assertIn("(your one open item as mover has no open step of yours)", note)
        self.assertNotIn("items", self.posted()[0])
        self.ctl("get.out", answer(record("a1", "item-1", ME, f"{ME}: first\n  - detail\n{ME}: second\nthe-owner: look")))
        note = self.run_hook(gh_event(ISSUE, command="gh issue create"))
        self.assertIn("(your one open item as mover has 2 open steps of yours)", note)
        self.assertNotIn("items", self.posted()[1])

    def test_a_cut_list_is_never_trusted_for_an_item(self):
        for extra in ({"truncated": True}, {"unreadable": ["a3"]}):
            with self.subTest(extra=extra):
                self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step"), **extra))
                note = self.run_hook(gh_event(PR, session=f"s-{sorted(extra)[0]}"))
                self.assertIn("project-wide", note)
                self.assertIn("cut or held unreadable ones", note)
                self.assertNotIn("items", self.posted()[-1])

    def test_a_last_line_that_is_more_than_the_url_is_not_anchored(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step")))
        for trailing in (" (draft) see also https://example.com/x?y=1", "/files"):
            note = self.run_hook(gh_event(PR, trailing=trailing, session="s" + str(len(trailing))))
            self.assertEqual(note, OLDER.format(url=PR) + f" Not anchored automatically: {NOT_FROM_CREATE}.")
        self.assertEqual(self.calls(), [])

    def test_the_same_url_is_anchored_once_per_session(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step")))
        self.assertIn("Anchored", self.run_hook(gh_event(PR)))
        again = self.run_hook(gh_event(PR))
        self.assertEqual(again, f"{PR} was already anchored in this session. {NOTE_TAG}")
        self.assertEqual(len(self.posted()), 1)
        self.assertEqual(len(self.calls()), 2)                    # the repeat made no client call at all
        self.assertIn("Anchored", self.run_hook(gh_event(PR, session="sess-2")))     # another session anchors it itself
        self.assertEqual(len(self.posted()), 2)

    def test_a_failed_attempt_leaves_no_marker(self):
        self.ctl("post.rc", "1")
        self.assertIn("Not anchored automatically", self.run_hook(gh_event(PR)))
        self.ctl("post.rc", "0")
        self.assertIn("Anchored", self.run_hook(gh_event(PR)))


class Falling(Base):
    """Whatever goes wrong: the older note, unchanged, then one short line with the reason; the hook exits 0 and prints nothing else."""

    def fallback(self, ev, why, **env_extra):
        note = self.run_hook(ev, **env_extra)
        self.assertTrue(note.startswith(OLDER.format(url=PR)), note)
        self.assertEqual(note, OLDER.format(url=PR) + f" Not anchored automatically: {why}.")
        return note

    def test_no_roles_entry(self):
        self.write(self.roles, json.dumps({"sessions": {"another-session": {"role": "back", "project": PROJECT}}, "ids": {}}))
        self.fallback(gh_event(PR), "no roles entry for this session")
        self.assertEqual(self.calls(), [])

    def test_no_roles_file(self):
        os.unlink(self.roles)
        self.fallback(gh_event(PR), "no roles entry for this session")
        self.assertEqual(self.calls(), [])

    def test_an_entry_without_a_project(self):
        self.write(self.roles, json.dumps({"sessions": {ME: {"role": "back"}}, "ids": {}}))
        self.fallback(gh_event(PR), "the session's roles entry names no usable project")

    def test_the_session_is_not_known(self):
        self.fallback(gh_event(PR), "the session's name is not known", BUILD_STOPS_NAME="")

    def test_no_client_in_the_checkout(self):
        self.fallback(gh_event(PR), "no stackshot client was found (set STACKSHOT_DIR)", STACKSHOT_DIR=self.make_checkout(with_client=False))

    def test_the_read_is_refused(self):
        self.ctl("get.rc", "1")
        self.ctl("get.out", "[401] no token on this machine")
        self.fallback(gh_event(PR), "the read of your open items failed ([401] no token on this machine)")
        self.assertEqual(self.posted(), [])

    def test_an_answer_that_does_not_parse(self):
        for bad in ("not json at all", "[]", '{"items": "x"}', "null"):
            with self.subTest(bad=bad):
                self.ctl("get.out", bad)
                self.fallback(gh_event(PR), "the answer to the read of your open items did not parse")
        self.assertEqual(self.posted(), [])

    def test_a_400_from_the_anchor_call(self):
        self.ctl("post.rc", "1")
        self.ctl("post.err", "stackshot: anchors not added [400] Refused: no item a7\nmore lines")
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step")))
        self.fallback(gh_event(PR), "the anchor call failed (stackshot: anchors not added [400] Refused: no item a7 more lines)")

    def test_a_403_and_a_503_fall_back_the_same_way(self):
        for code in ("403", "503"):
            with self.subTest(code=code):
                self.ctl("post.rc", "1")
                self.ctl("post.err", f"stackshot: anchors not added [{code}] refused")
                self.fallback(gh_event(PR, session="s" + code), f"the anchor call failed (stackshot: anchors not added [{code}] refused)")

    def test_a_slow_read_times_out(self):
        self.ctl("sleep.get", "20")
        t = time.monotonic()
        self.fallback(gh_event(PR), "the read of your open items timed out", POST_TOOL_NOTE_CALL_TIMEOUT_S="0.7")
        self.assertLess(time.monotonic() - t, 8)
        self.assertEqual(self.posted(), [])

    def test_a_slow_anchor_call_times_out(self):
        self.ctl("sleep.post", "20")
        t = time.monotonic()
        self.fallback(gh_event(PR), "the anchor call timed out", POST_TOOL_NOTE_CALL_TIMEOUT_S="0.7")
        self.assertLess(time.monotonic() - t, 8)

    def test_a_client_that_crashes(self):
        self.write(os.path.join(self.checkout, "stackshot", "fleet", "client.py"), "this is not python (\n")
        note = self.run_hook(gh_event(PR))
        self.assertTrue(note.startswith(OLDER.format(url=PR) + " Not anchored automatically: the read of your open items failed"), note)

    def test_a_repository_or_a_branch_is_not_sent_to_the_route(self):
        repo = "https://github.com/acme/widgets"
        ev = {"tool_name": "Bash", "tool_input": {"command": "gh repo create acme/widgets"}, "tool_response": {"stdout": repo + "\n"}, "session_id": "s"}
        self.assertEqual(self.run_hook(ev), OLDER.format(url=repo))
        self.assertEqual(self.calls(), [])

    def test_no_url_means_no_note(self):
        ev = {"tool_name": "Bash", "tool_input": {"command": "gh pr create --web"}, "tool_response": {"stdout": "Opening github.com in your browser."}, "session_id": "s"}
        self.assertEqual(self.run_hook(ev), "")
        self.assertEqual(self.calls(), [])

    def test_a_dotted_owner_is_not_sent(self):
        url = "https://github.com/../widgets/pull/7"
        self.assertEqual(self.run_hook(gh_event(url)), OLDER.format(url=url))      # the route refuses `..` as an owner: not sent
        self.assertEqual(self.calls(), [])


class Picking(unittest.TestCase):
    """pick_item on its own: the one rule for the item."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("ptn_mod", SCRIPT)
        cls.m = importlib.util.module_from_spec(spec)
        sys.path.insert(0, S.HOOKS)
        spec.loader.exec_module(cls.m)

    def pick(self, *items, **extra):
        return self.m.pick_item(json.loads(answer(*items, **extra)), {ME})

    def test_the_forms_of_a_step_opener(self):
        for text in (f"{ME}: do it", f"**{ME}:** do it", f"- {ME}: do it", f"1. {ME} (builder): do it", f"{ME.upper()}: do it"):
            with self.subTest(text=text):
                item, _why = self.pick(record("a1", "item-1", ME, text))
                self.assertIsNotNone(item, text)

    def test_what_is_not_the_sessions_own_step(self):
        for text in (f"the-owner: ask {ME}: now", f"{ME}-two: do it", "a line naming nobody", f"  {ME}: indented, a sub-line", f"({ME}: a note)"):
            with self.subTest(text=text):
                self.assertIsNone(self.pick(record("a1", "item-1", ME, text))[0], text)

    def test_stored_steps_are_used_when_there_is_no_next(self):
        meta = {"steps": [{"text": f"{ME}: done one", "status": "done"}, {"text": f"{ME}: open one", "status": "open"}]}
        it = {"id": "a1", "shown": "item-1", "record": {"status": "open", "metadata": {"mover": ME, **meta}}, "anchors": []}
        self.assertIsNotNone(self.pick(it)[0])

    def test_the_registry_id_counts_as_the_session(self):
        it = record("a1", "item-1", "abc123", "abc123: step")
        self.assertIsNotNone(self.m.pick_item(json.loads(answer(it)), {ME, "abc123"})[0])

    def test_a_row_that_is_not_a_record_is_skipped(self):
        item, why = self.pick("junk", {"id": "a1"}, record("a2", "item-2", ME, f"{ME}: step"))
        self.assertEqual(item["id"], "a2")

    def test_an_item_without_an_id_is_refused(self):
        bad = record("", "item-1", ME, f"{ME}: step")
        with self.assertRaises(ValueError):
            self.pick(bad)


def load_module(name):
    """A fresh copy of the hook as a module (its state is set by the test)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, S.HOOKS)
    spec.loader.exec_module(mod)
    return mod


def bash_event(command, stdout, stderr="", session="sess-1"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": {"stdout": stdout, "stderr": stderr},
            "session_id": session, "hook_event_name": "PostToolUse"}


class Narrowing(Base):
    """The hook anchors only a link the create call itself printed: `gh pr|issue create` as a real command, the URL the last line of stdout."""

    def untrusted(self, ev):
        note = self.run_hook(ev)
        self.assertTrue(note.endswith(f" Not anchored automatically: {NOT_FROM_CREATE}."), note)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.posted(), [])
        return note

    def test_a_command_that_only_mentions_gh_pr_create_in_quotes(self):
        note = self.untrusted(bash_event('grep -n "gh pr create" docs/notes.md', OTHER_PR + "\n"))
        self.assertTrue(note.startswith(OLDER.format(url=OTHER_PR)), note)         # the older note, as before

    def test_a_command_with_gh_issue_create_in_a_comment(self):
        issue = "https://github.com/acme/widgets/issues/5"
        note = self.untrusted(bash_event("cat README.md  # gh issue create", issue + "\n"))
        self.assertTrue(note.startswith(OLDER.format(url=issue)), note)

    def test_a_create_followed_by_another_command_that_prints_a_url(self):
        self.untrusted(bash_event("gh pr create --fill && gh pr view --json url -q .url", PR + "\n" + OTHER_PR + "\n"))
        self.untrusted(bash_event("gh pr create --fill && gh pr view --json comments", PR + "\n" + '{"comments": []}\n', session="s2"))

    def test_a_url_on_stderr_only(self):
        self.untrusted(bash_event("gh pr create --fill", "created\n", stderr=PR + "\n"))

    def test_a_url_that_is_not_the_last_line(self):
        self.untrusted(bash_event("gh pr create --fill", PR + "\nsome trailing line\n"))

    def test_a_response_without_a_stdout_field(self):
        ev = bash_event("gh pr create --fill", "")
        ev["tool_response"] = PR + "\n"
        self.untrusted(ev)

    def test_a_create_inside_a_heredoc_body_or_a_substitution(self):
        self.untrusted(bash_event("cat <<'EOF'\ngh pr create --fill\nEOF", PR + "\n"))
        self.untrusted(bash_event('echo "$(gh pr create --fill)"', PR + "\n", session="s2"))

    def distrusted(self, commands, stdout=EVIL + "\n"):
        """Each command ends its stdout in a link the create did not print: nothing is anchored, the older note says why."""
        for n, command in enumerate(commands):
            with self.subTest(command=command):
                note = self.untrusted(bash_event(command, stdout, session=f"d{n}"))
                self.assertTrue(note.startswith(OLDER.format(url=EVIL)), note)

    def test_a_conditional_or_makes_the_whole_command_untrusted(self):
        # the create never runs when the first command succeeds, and its link is the one printed first
        self.distrusted([f"echo {EVIL} || gh pr create --fill", "cat pr-url.txt || gh pr create --fill",
                         "[ -f x ] && cat x || gh pr create", "gh pr view --json url -q .url || gh pr create --fill",
                         "gh pr create --fill || true", f"gh pr create --fill || echo {EVIL}", "gh pr create --fill |& cat"])

    def test_a_backgrounded_command_makes_the_whole_command_untrusted(self):
        self.distrusted([f"gh pr create --fill & echo {EVIL}", f"gh pr create --fill &\necho {EVIL}", "echo hi & gh pr create --fill"])

    def test_arithmetic_and_other_unmodeled_shell_syntax_make_the_command_untrusted(self):
        self.distrusted([f"(( 1<<X )); gh pr create --fill\necho {EVIL}", f"echo $(( 1<<X )); gh pr create --fill\necho {EVIL}",
                         f"x=$[ 1<<X ]; gh pr create --fill\necho {EVIL}", "[[ -f x ]] && gh pr create --fill",
                         f"case x in x) gh pr create --fill;; esac\necho {EVIL}", f"{{ gh pr create --fill; echo {EVIL}; }}",
                         f"f() {{ gh pr create --fill; }}; f\necho {EVIL}", f"(gh pr create --fill; echo {EVIL})",
                         "gh pr create --fill <(echo x)", "gh pr create --fill >(cat)", "gh pr create --fill ;; echo x"])

    def test_a_heredoc_opener_that_is_not_well_formed_makes_the_command_untrusted(self):
        self.distrusted(["gh pr create --fill <<$DELIM", "gh pr create --fill << )", "gh pr create --fill <<"])
        self.distrusted(["gh pr create --fill <<EOF\nnever ends", "gh pr create --fill <<EOF\nbody\nEOF\ngh pr create <<EOF2"])

    def test_ansi_c_quoting_makes_the_command_untrusted(self):
        # `$'\''` is one quoted quote to the shell; read plainly it hides the echo inside a quote, in the second form with every quote balanced
        self.distrusted([f"gh pr create --fill --title $'\\'' ; echo {EVIL} ; : $'\\''", f"gh pr create --fill --title $'\\'' ; echo {EVIL} ; : \\'",
                         "gh pr create --fill --title $'a'", 'gh pr create --fill --title $"a"'])

    def test_anything_before_the_create_beyond_plain_commands_makes_the_command_untrusted(self):
        # a command that ends the line, rewires it or is not on the short list of plain ones
        self.distrusted([f"exit 0; gh pr create --fill\necho {EVIL}", "exit 0; gh pr create --fill", "eval 'gh() { :; }'; gh pr create --fill",
                         "source ./x.sh && gh pr create --fill", "if true; then true; fi; gh pr create --fill", "bash -c 'echo hi'; gh pr create --fill",
                         "time gh pr create --fill", "sudo gh pr create --fill", "env X=1 gh pr create --fill", "! true; gh pr create --fill"])

    def test_a_create_that_prints_no_link_or_whose_stdout_goes_elsewhere_is_untrusted(self):
        self.distrusted([f"echo {EVIL}; gh pr create --web", f"echo {EVIL}; gh pr create --fill -w", f"echo {EVIL}; gh pr create --fill --dry-run",
                         f"echo {EVIL}; gh pr create --fill > /dev/null", f"echo {EVIL}; gh pr create --fill >&2", f"echo {EVIL}; gh pr create --fill &> /dev/null",
                         f"echo {EVIL}; gh pr create --fill >> log", f"echo {EVIL}; gh pr create --fill 1> /dev/null", f"echo {EVIL}; gh pr create --help"])

    def test_the_stderr_of_the_create_may_go_anywhere(self):
        for n, command in enumerate(["gh pr create --fill 2>&1", "gh pr create --fill 2>/dev/null", "gh pr create --fill --title 'a -w b' 2>&1"]):
            with self.subTest(command=command):
                self.assertIn(f"Anchored {PR} project-wide", self.run_hook(bash_event(command, PR + "\n", session=f"e{n}")))

    def test_the_forms_of_a_real_create_are_anchored(self):
        body_file = "gh pr create --title t --body-file - <<'EOF'\nthe body, with gh pr view in it\nEOF"
        substitution = "gh pr create --title \"a; b\" --body \"$(cat <<'EOF'\nsummary \"quoted\" and `gh pr view`\n# not a comment\nEOF\n)\""
        forms = ["gh pr create --title t --body b", body_file, substitution, "cd work && gh pr create --fill",
                 "GH_REPO=acme/widgets gh pr create --fill", "gh -R acme/widgets pr create --fill",
                 "echo start\ngh pr create \\\n  --title t \\\n  --body b", "git push -u origin x; gh pr create --fill  # then wait",
                 "gh pr new --fill", "cat x | gh pr create --body-file -"]
        for n, command in enumerate(forms):
            with self.subTest(command=command):
                note = self.run_hook(bash_event(command, f"warning: a line first\n\n{PR}\n\n", stderr="Creating pull request\n", session=f"f{n}"))
                self.assertIn(f"Anchored {PR} project-wide", note)

    def test_an_issue_create_is_anchored_from_its_last_line(self):
        note = self.run_hook(bash_event("gh issue create --title t --body-file /tmp/x", ISSUE + "\n"))
        self.assertIn(f"Anchored {ISSUE} project-wide", note)

    def test_the_connector_result_must_hold_one_distinct_url(self):
        def mcp(text):
            return {"tool_name": "mcp__github__create_pull_request", "tool_input": {"owner": "acme", "repo": "widgets"},
                    "tool_response": {"content": [{"type": "text", "text": text}]}, "session_id": "sess-m"}
        note = self.run_hook(mcp(f"created {PR} (see also {OTHER_PR})"))
        self.assertEqual(note, OLDER.format(url=PR) + f" Not anchored automatically: {NOT_FROM_CREATE}.")
        self.assertEqual(self.calls(), [])
        # the same pull request under several spellings is one URL
        self.assertIn(f"Anchored {PR}", self.run_hook(mcp(f'{{"url": "{PR}", "html_url": "{PR}", "diff_url": "{PR}.diff"}}')))

    # The hook never sees the exit status of what it reads (a failing command fires no PostToolUse hook), so each test below is a command
    # that bash runs differently from a plain reading of its text, and that ends in a link the create did not print.

    def test_a_heredoc_delimiter_word_that_goes_on_makes_the_command_untrusted(self):
        # bash's delimiter is the whole word (`EOF#x`), so the line `EOF` below closes nothing and the create is part of the body
        tails = ["#x", "/x", ":", ",", "$x", '"x"', "@", "+", "=", "%", "\\\\"]
        self.distrusted([f"echo {EVIL} <<EOF{t}\nEOF\ngh pr create --fill" for t in tails])
        self.distrusted([f"printf '%s\\n' {EVIL} <<EOF#x\nEOF\ngh pr create --fill", f"echo {EVIL} <<-EOF#x\n\tEOF\ngh pr create --fill",
                         f"echo {EVIL} <<\\EOF#x\nEOF\ngh pr create --fill", f"echo {EVIL} <<'EOF'x\nEOF\ngh pr create --fill",
                         f"echo {EVIL} <<\"EOF\"x\nEOF\ngh pr create --fill", f"echo {EVIL} <<E\"O\"F\nEOF\ngh pr create --fill",
                         f"echo {EVIL} << EOF#x\nEOF\ngh pr create --fill", f"echo {EVIL} <<\"E\\\\x\"\nE\\\\x\ngh pr create --fill"])

    def test_a_heredoc_delimiter_word_that_ends_at_a_metacharacter_is_modeled(self):
        for n, tail in enumerate([" ", "\n", ";", "&&", "|", " >/dev/null", "<in.txt", " 2>&1"]):
            with self.subTest(tail=tail):
                command = f"cat <<EOF{tail}" + ("" if tail == "\n" else "\n") + "body\nEOF\ngh pr create --fill"
                self.assertIn(f"Anchored {PR} project-wide", self.run_hook(bash_event(command, PR + "\n", session=f"m{n}")))

    def test_a_heredoc_closes_only_on_a_line_that_is_exactly_its_delimiter(self):
        # a line that is a blank away from the delimiter, or has a carriage return after it, does not close a plain `<<` body
        self.distrusted([f"echo {EVIL} <<EOF\nbody\n EOF\ngh pr create --fill", f"echo {EVIL} <<EOF\nbody\nEOF \ngh pr create --fill",
                         f"echo {EVIL} <<EOF\nbody\n\tEOF\ngh pr create --fill", f"echo {EVIL} <<EOF\nbody\nEOF\r\ngh pr create --fill",
                         f"echo {EVIL} <<'EOF'\nbody\n\tEOF\ngh pr create --fill", f"echo {EVIL} <<-EOF\nbody\n EOF\ngh pr create --fill",
                         f"echo {EVIL} <<-EOF\nbody\n\t EOF\ngh pr create --fill", f"echo {EVIL} <<-EOF\nbody\nEOF \ngh pr create --fill"])

    def test_a_heredoc_closes_on_its_delimiter_alone_and_on_tabs_only_for_dash(self):
        forms = ["cat <<EOF\nbody\nEOF\ngh pr create --fill", "cat <<-EOF\n\t\tbody\n\t\tEOF\ngh pr create --fill",
                 "cat <<-'EOF'\nbody\n\tEOF\ngh pr create --fill", "cat <<'a b'\nbody\na b\ngh pr create --fill",
                 "cat <<\"EOF\"\nbody\nEOF\ngh pr create --fill", "cat <<\\EOF\nbody\nEOF\ngh pr create --fill",
                 "cat <<EOF\nEOF\ngh pr create --fill"]
        for n, command in enumerate(forms):
            with self.subTest(command=command):
                self.assertIn(f"Anchored {PR} project-wide", self.run_hook(bash_event(command, PR + "\n", session=f"c{n}")))

    def test_a_body_line_that_only_holds_the_delimiter_does_not_end_the_heredoc(self):
        # never closed: the whole create is body text. A scanner that ended the body on a line that contains, starts or ends with the word
        # would run the create
        self.distrusted([f"echo {EVIL} <<EOF\nsay EOF now\ngh pr create --fill", f"echo {EVIL} <<EOF\nEOF x\ngh pr create --fill",
                         f"echo {EVIL} <<EOF\nx EOF\ngh pr create --fill", f"echo {EVIL} <<EOF\nEOFEOF\ngh pr create --fill",
                         f"echo {EVIL} <<EOF\nEO\ngh pr create --fill"])
        command = "cat <<EOF\nsay EOF now\nEOF x\nx EOF\nEOF\ngh pr create --fill"
        self.assertIn(f"Anchored {PR} project-wide", self.run_hook(bash_event(command, PR + "\n")))

    def test_an_unquoted_heredoc_body_joins_a_line_that_ends_in_a_backslash_to_the_next(self):
        # bash reads `ab\` + `EOF` as the one line `abEOF`: the body goes on, and the create below is only text. Quoting the delimiter turns
        # that joining off
        self.distrusted([f"echo {EVIL} <<EOF\nab\\\nEOF\ngh pr create --fill", f"echo {EVIL} <<-EOF\n\tab\\\n\tEOF\ngh pr create --fill",
                         f"echo {EVIL} <<EOF\nab\\\\\\\nEOF\ngh pr create --fill"])
        for n, command in enumerate(["cat <<'EOF'\nab\\\nEOF\ngh pr create --fill", "cat <<\\EOF\nab\\\nEOF\ngh pr create --fill",
                                     "cat <<EOF\nab\\\\\nEOF\ngh pr create --fill"]):
            with self.subTest(command=command):
                self.assertIn(f"Anchored {PR} project-wide", self.run_hook(bash_event(command, PR + "\n", session=f"q{n}")))

    def test_a_backslash_newline_with_no_blank_before_it_makes_the_command_untrusted(self):
        # bash deletes the pair, so `|` + `|` become `||`, `create` + `#x` one word and `$` + `'` an ANSI-C quote
        self.distrusted([f"echo {EVIL} |\\\n| gh pr create --fill", "cat url.txt |\\\n| gh pr create", f"gh pr create --fill\\\n#x; echo {EVIL}",
                         f"gh pr create --fill --title $\\\n'\\'' ; echo {EVIL} ; : \\'", f"echo {EVIL} &\\\n& gh pr create --fill",
                         f"echo {EVIL} ;\\\n; gh pr create --fill", f"gh pr create --fill\\\n; echo {EVIL}", f"echo {EVIL}; gh pr create --fill --title a\\\nb",
                         f"gh pr create --fill --title \"a\\\nb\"; echo {EVIL}", f"echo {EVIL}; gh pr create --fill \"$\\\n(echo x)\"",
                         f"echo {EVIL}; gh pr create --fill \\ \\\n#x", f"echo {EVIL}; gh pr create --fill a\\ \\\n#x; echo {EVIL}",
                         f"echo {EVIL}; gh pr create \"`\\\n`\""])

    def test_a_backslash_newline_after_a_blank_is_a_continuation(self):
        forms = ["gh pr create \\\n  --title t \\\n  --body b", "echo a \\\n b; gh pr create --fill", "cd . && \\\n  gh pr create --fill",
                 "gh pr create --title \"a \\\n b\" --fill", "gh pr create --fill \\\n  # a comment\n", "git push \\\n  -u origin x\ngh pr create --fill",
                 "gh \\\npr create --fill", "echo a;\n\\\ngh pr create --fill"]
        for n, command in enumerate(forms):
            with self.subTest(command=command):
                self.assertIn(f"Anchored {PR} project-wide", self.run_hook(bash_event(command, PR + "\n", session=f"b{n}")))

    def test_a_hash_that_does_not_start_a_word_is_not_a_comment(self):
        # `a#b` and `a\ #b` are one word each: the `; echo` after them runs and is the last command
        self.distrusted([f"gh pr create --fill a#b; echo {EVIL}", f"gh pr create --fill a\\ #b; echo {EVIL}",
                         f"gh pr create --fill 'a'#b; echo {EVIL}", f"gh pr create --fill $x#b; echo {EVIL}",
                         f"gh pr create --fill a$(true)#b; echo {EVIL}", f"gh pr create --fill \"a\"#b; echo {EVIL}"])
        for n, command in enumerate(["gh pr create --fill --title a#b", "gh pr create --fill # then wait; echo x", "gh pr create --fill\t#c",
                                     "echo a;#c\ngh pr create --fill"]):
            with self.subTest(command=command):
                self.assertIn(f"Anchored {PR} project-wide", self.run_hook(bash_event(command, PR + "\n", session=f"h{n}")))

    def test_a_word_that_ends_in_a_two_is_not_a_stderr_redirection(self):
        # `x2>f` is the word `x2` and a redirection of stdout; only a `2` that is a word of its own names stderr
        self.distrusted([f"echo {EVIL}; gh pr create --fill x2>f", f"echo {EVIL}; gh pr create --fill a2>>f", f"echo {EVIL}; gh pr create --fill 22>f",
                         f"echo {EVIL}; gh pr create --fill $2>f", f"echo {EVIL}; gh pr create --fill '2'>f", f"echo {EVIL}; gh pr create --fill \\2>f",
                         f"echo {EVIL}; gh pr create --fill \"x\"2>f"])

    def test_a_create_inside_quotes_or_a_substitution_of_an_assignment_is_not_the_create(self):
        # `A="x gh pr create"` is one assignment word: no command runs, and the words in the quotes are text
        self.distrusted([f"echo {EVIL}; A=\"x gh pr create --fill\"", f"echo {EVIL}; A='x gh pr create --fill'", f"echo {EVIL}; A=$(x gh pr create --fill)",
                         f"echo {EVIL}; A=`x gh pr create --fill`", f"echo {EVIL}; A=1' \ngh pr create --fill'", f"echo {EVIL}; A=\\ gh pr create --fill",
                         f"echo {EVIL}; B=\"a\" A=\"x gh pr create --fill\""])

    def test_the_commands_that_may_come_before_the_create_are_a_closed_list(self):
        self.distrusted([f"{c} x; gh pr create --fill" for c in ("python3", "sed", "tee", "env", "gh", "sh", "bash", "xargs", "command", "builtin",
                                                                  "source", ".", "read", "wait", "set", "trap", "unset", "alias", "mapfile")])

    def test_a_command_longer_than_the_cap_is_not_modeled(self):
        base = "gh pr create --fill --title "
        for n, (size, trusted) in enumerate(((200_000, True), (200_001, False))):
            with self.subTest(size=size):
                command = base + "a" * (size - len(base))
                note = self.run_hook(bash_event(command, PR + "\n", session=f"l{n}"))
                if trusted:
                    self.assertIn(f"Anchored {PR} project-wide", note)
                else:
                    self.assertEqual(note, OLDER.format(url=PR) + f" Not anchored automatically: {NOT_FROM_CREATE}.")

    def test_substitutions_nested_past_the_recursion_limit_give_the_older_note_with_a_reason(self):
        # the scanner recurses into each `$(`: an error there must not end the hook without a note
        command = "gh pr create --fill --title \"" + "$(echo " * 3000 + ")" * 3000 + "\""
        self.assertTrue(len(command) < 200_000)
        note = self.run_hook(bash_event(command, PR + "\n"))
        self.assertEqual(note, OLDER.format(url=PR) + f" Not anchored automatically: {NOT_FROM_CREATE}.")
        self.assertEqual(self.calls(), [])

    # A quiet flag (`--web`, `-w`, `--dry-run`, `--help`) that the shell only builds at run time: the create prints no link, bash exits 0, and
    # the last line of stdout is the echoed prefix.

    def test_an_expansion_in_a_flag_position_makes_the_command_untrusted(self):
        forms = ["W=--web; echo @U@; gh pr create $W", 'W=--web; echo @U@; gh pr create "$W"', "echo @U@; gh pr create {--web,}",
                 "echo @U@; gh pr create --web$X", "echo @U@; gh pr create $(printf -- --web)", "echo @U@; gh pr create `echo --web`",
                 "echo @U@; gh pr create -$(echo w)", "echo @U@; gh pr create --fill $W", "echo @U@; gh pr create --fill ${W}",
                 "echo @U@; gh pr create --fill --w${X}eb", 'echo @U@; gh pr create --fill "-$X"', "echo @U@; gh pr create --fill *",
                 "echo @U@; gh pr create --fill ?", "echo @U@; gh pr create --fill [w]", "echo @U@; gh pr create -{w,}",
                 "echo @U@; gh pr create --fill --title $W", "echo @U@; gh pr create --fill --title $(echo x)",
                 "echo @U@; gh pr create --fill --title {a,b}", "echo @U@; gh pr create --fill --title *", "echo @U@; gh pr create --fill -d$X",
                 "echo @U@; gh pr create --fill 2>$F", "echo @U@; gh -R $R pr create --fill"]
        for quiet in ("--web", "--dry-run", "--help"):
            forms += [f"W={quiet}; echo @U@; gh pr create $W", f'W={quiet}; echo @U@; gh pr create "$W"', f"echo @U@; gh pr create {quiet}$X",
                      f"echo @U@; gh pr create $(printf -- {quiet})", f"echo @U@; gh pr create `echo {quiet}`"]
        self.distrusted([f.replace("@U@", EVIL) for f in forms])

    def test_a_quiet_flag_in_any_quoting_makes_the_command_untrusted(self):
        self.distrusted([f"echo {EVIL}; gh pr create {q}" for q in ('"--web"', "'--web'", '"--dry-run"', "'--help'", "--we\\b", '--"web"', "--w'e'b",
                                                                   '"-w"', "-\\w", '"--web"=true', "--web=true", "-fw", "-h", '-"h"', "'--we'\"b\"", "--we\\\nb x")])
        self.distrusted([f"echo {EVIL}; gh pr create --fill --title -w", f"echo {EVIL}; gh pr create --fill -R x/y -h"])
        # a redirection between a flag and its value is not the value: `-R` takes `-t` here and `"--web"` is a flag of its own
        self.distrusted([f'echo {EVIL}; gh pr create -R 2>/dev/null -t "--w""eb"', f'echo {EVIL}; gh pr create -R < in.txt -t "--web"',
                         f'echo {EVIL}; gh pr create -R 2>&1 -t "--web"', f'echo {EVIL}; gh pr create -B 2>>err.log -R "--web"'])

    def test_a_short_flag_cluster_that_swallows_its_value_is_untrusted_end_to_end(self):
        # the hook itself, not only the scanner: the last stdout line is a link, yet the create may have been `--web` or `--help`
        self.distrusted([f"echo {EVIL}; gh pr create {w}" for w in ("-wt1", "-wb1", "-wt-x", "-wR1", "-dwt1", "-fwt1", "-ewt1", "-hb1", "-hl1", "-wH.")])
        self.distrusted([f"echo {EVIL}; gh pr create--web --title t", f"echo {EVIL}; gh pr\xa0create --title t", f"echo {EVIL}; gh pr create\r --title t",
                         f"echo {EVIL}; gh pr create --fill >/dev/null"])

    def test_an_expansion_in_the_value_of_a_flag_that_takes_one_is_trusted(self):
        # the value of --title and the like can never become a flag of its own; `$( )` and backticks only inside double quotes
        body = "gh pr create --title \"x $Y\" --body \"$(cat <<'EOF'\nthe body, with --web and $HOME in it\nEOF\n)\""
        forms = [body, 'gh pr create --title "x $Y" --fill', 'gh pr create --title="x $Y" --fill', 'gh pr create -R "$R" --fill', 'gh -R "$R" pr create --fill', 'gh pr create --base "$B" --head "$H" --fill',
                 'gh pr create --label "$L" --assignee "$A" --fill', 'gh pr create --body-file "$F" --fill', "gh pr create --fill",
                 "gh pr create --title 'a $b' --fill", "W=--web gh pr create --fill", 'T="x $Y" gh pr create --fill', 'echo "$X" && gh pr create --fill']
        for n, command in enumerate(forms):
            with self.subTest(command=command):
                self.assertIn(f"Anchored {PR} project-wide", self.run_hook(bash_event(command, PR + "\n", session=f"x{n}")))

    def test_an_unquoted_expansion_in_a_value_is_untrusted(self):
        # word splitting can turn it into a flag
        self.distrusted([f"echo {EVIL}; gh pr create --title $Y --fill", f"echo {EVIL}; gh pr create --fill --body $(cat f)",
                         f"echo {EVIL}; gh pr create --fill -R $R", f"echo {EVIL}; gh pr create --fill --title=$Y", f"echo {EVIL}; gh pr create --fill -t$Y", f'echo {EVIL}; gh pr create -t"$Y" -B main "--web"', f'echo {EVIL}; gh pr create --fill -R"$Y" "--web"',
                         f"echo {EVIL}; gh pr create --fill --body `cat f`", f"echo {EVIL}; gh pr create --fill --title x{{a,b}}"])

    def test_bash_agrees_that_each_of_these_ends_in_the_echoed_link_and_not_the_creates(self):
        # the claim behind the two tests above, run for real with a stand-in `gh` that prints a link unless it is given a quiet flag
        gh = ('gh(){ local q=0 skip=0 a; for a in "$@"; do if ((skip)); then skip=0; continue; fi; case $a in '
              '--title|-t|--body|-b|--body-file|-F|--repo|-R|--base|-B|--head|-H|--label|-l|--assignee|-a) skip=1;; '
              '--title=*|--body=*|--repo=*|-[tbFRBHla]?*) ;; --web|--dry-run|--help|-w|-h) q=1;; esac; done; ((q)) || echo CREATED; }\n')
        for command in ("W=--web; echo U; gh pr create $W", 'W=--web; echo U; gh pr create "$W"', "echo U; gh pr create {--web,}", "echo U; gh pr create --web$X",
                        "echo U; gh pr create $(printf -- --web)", "echo U; gh pr create `echo --web`", "echo U; gh pr create -$(echo w)",
                        "W=--dry-run; echo U; gh pr create $W", "echo U; gh pr create --help$X", 'echo U; gh pr create "--web"'):
            with self.subTest(command=command):
                with tempfile.TemporaryDirectory() as d:
                    r = subprocess.run(["bash", "-c", gh + command], capture_output=True, text=True, cwd=d, timeout=20, env={"PATH": os.environ["PATH"], "HOME": d})
                self.assertEqual((r.returncode, r.stdout.split()[-1]), (0, "U"), (r.stdout, r.stderr))
        for command in ('echo U; gh pr create --title "x $Y"', "echo U; gh pr create --fill"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as d:
                r = subprocess.run(["bash", "-c", gh + command], capture_output=True, text=True, cwd=d, timeout=20, env={"PATH": os.environ["PATH"], "HOME": d})
                self.assertEqual((r.returncode, r.stdout.split()[-1]), (0, "CREATED"), (r.stdout, r.stderr))

    def test_a_nul_byte_makes_the_command_untrusted(self):
        self.distrusted([f"echo {EVIL}; gh pr create --fill\x00", f"echo \x00{EVIL}; gh pr create --fill", f"\x00echo {EVIL}; gh pr create --fill",
                         f"echo {EVIL}; gh pr create --title 'a\x00b' --fill"])

    def test_an_array_assignment_makes_the_command_untrusted(self):
        self.distrusted([f"x=(a); echo {EVIL}; gh pr create --fill", "x=(a); gh pr create --fill", f"echo {EVIL}; x=(a b)\ngh pr create --fill"])

    def test_a_heredoc_that_is_never_closed_makes_the_command_untrusted(self):
        # no newline at all after the opener: the body is empty, and where the command ends is not modeled
        self.distrusted([f"echo {EVIL}; gh pr create --fill <<EOF", f"echo {EVIL}; gh pr create --fill <<'EOF'", f"echo {EVIL}; gh pr create --fill <<-EOF"])


# ---- the bash differential: a command the scanner trusts, run by bash with `gh` a function that only prints its argv -------------------
GH_STUB = 'gh(){ printf "%s\\0" "$@" > "$ARGV_FILE"; }\n'            # never a real gh: it writes its argv, NUL-separated, to a file
# gh's value flags as pflag reads them, for `gh pr create` and `gh issue create` together (the help lists them; written out here on purpose, apart
# from the hook's own table, so that the oracle is not the code under test): a long one takes the next word unless it has `=`, a short one takes
# the rest of its word, or the next word when it ends the word
ARGV_VALUE_LONG = frozenset(("assignee", "attach", "base", "blocked-by", "blocking", "body", "body-file", "head", "label", "milestone", "parent", "project",
                             "recover", "reviewer", "template", "title", "type", "repo"))
ARGV_VALUE_SHORT = frozenset("aBbFHlmprTtR")
TOKENS = [" ", "<", ">", "(", ")", ";", "~", "$", "@", "*", '"', "'", "\\", "`", "-", "w", "e", "b", "case",
          "1", ".", "/", "t", "h", "d", "m", "r"]            # digits and punctuation after a flag letter: gh lets a value flag swallow the rest of its word
DIFF_PREFIX = ["", "cd d && ", "git push && ", "echo U; ", "W=--web; ", "W=-w; HOME=--web; ", "export HOME=-w; ", "HOME=--dry-run; ", "A=1 ", "cd d && git push && ",
               'W="--web x"; X=; echo U; ', "true;\n", "echo a | cat; "]
DIFF_GOOD = ["--fill", "--title t", '--title "x $Y"', "--title 'a b'", "--body b", '--body "$(cat <<\'EOF\'\nthe body --web\nEOF\n)"', "--body-file - <<'EOF'\nbody\nEOF",
             "2>&1", "2>/dev/null", "-R x/y", "-B main", "-H feat", "-l bug", "-a me", "-d", "-t t", '-F "$F"', '--repo="$R"', '--title="a $B"', "--draft",
             "# a comment", "-t1", "-b1", "-dt1", "-fb.x", "-R1", "-l1_2"]
DIFF_BAD = ["--web<f", "-w<f", "--dry-run<<<x", "--help<&-", "--web<<EOF\nx\nEOF", "--title t --web</dev/null", "~", "~/x", '"$@"', '-t "$@" -b "$W"',
            '-l "$@" -a "$W"', "--title \"$(case a in a) echo '\"' ;; esac)\" --web #'", "$W", '"$W"', "{--web,}", "--web$X", "$(printf -- --web)",
            "`echo --web`", "-$(echo w)", '"--web"', "--we\\b", "-w", "--web", "*", "?", "[w]", "$HOME", '"$HOME"', '-t"$W"', "--title $W", "> f", "< f", ">&2",
            "2>$F", "<<EOF\nx\nEOF", "<<'EOF'\nx\nEOF", "--fill -w", "--fill\t--web", "--fill \\\n--web", "-t -w", '-R "$W" -w', "--title=--web", "!", "a}b",
            "a]b", '"a b" \'c\'', "-wt1", "-wb1", "-wt-x", "-wR1", "-dwt1", "-fwt1", "-ewt1", "-hb1", "-hl1", "-wH.", "-w1", "-h.", "-dh/x", "-wt_1"]
DIFF_RAW = ["gh pr create", "--web", "-w", "$W", '"$W"', "<", ">", "~", "$@", "case", "$(", ")", "(", "`", ";", "'", '"', "\\", " ", "\t", "\n", "-t", "-b",
            "x", "--", "-", "-wt1", "-hb1", "-dwt1", "-wH.", "1", ".", "/", "_", "-t1", "-d"]
DIFF_VALUE_FLAGS = ["-m", "-p", "-r", "-T", "-a", "-l", "-R", "-B", "-H", "-F", "-t", "-b", "-dt", "-ft", "-dR", "-dH", "-dm", "-fb", "-ep", "-dr", "-fT",
                    "--milestone", "--project", "--reviewer", "--template", "--recover", "--attach", "--blocked-by", "--blocking", "--parent", "--type",
                    "--title", "--body", "--body-file", "--repo", "--base", "--head", "--label", "--assignee"]
DIFF_QUOTED_QUIET = ['"-w"', "'-w'", '"--web"', "'--help'", '"-h"', '"$W"', "'-t'", "'-b'", "-hlm.w''", "'--dry-run'", '"-dw"', '"$W$X"', "-w", "--web"]
DIFF_POOL = DIFF_GOOD + DIFF_BAD + DIFF_VALUE_FLAGS + DIFF_VALUE_FLAGS + DIFF_QUOTED_QUIET + DIFF_QUOTED_QUIET
_DIFF_MODULE = None                                                   # set before the worker processes are forked


def argv_has_quiet_flag(argv):
    """Whether the words gh would be given hold a flag that stops it printing the link, read the way pflag reads them: a value flag takes the next
    word whatever it looks like, a cluster of short flags is read letter by letter until a value letter takes the rest of the word (or the next
    word), and `--` ends the flags."""
    pending = False
    for x in argv:
        if pending:
            pending = False
        elif x == "--":
            return False
        elif x.startswith("--"):
            name, eq, _rest = x[2:].partition("=")
            if name in ("web", "dry-run", "help"):
                return True
            pending = name in ARGV_VALUE_LONG and not eq
        elif x.startswith("-") and len(x) > 1:
            for k, ch in enumerate(x[1:]):
                if ch in "wh":
                    return True
                if ch in ARGV_VALUE_SHORT:
                    pending = k == len(x) - 2
                    break
    return False


def run_with_stub(cmd, d):
    """The argv bash gives `gh` for `cmd` in the folder `d` (None when the command fails or never runs gh)."""
    af = os.path.join(d, "argv")
    if os.path.exists(af):
        os.unlink(af)
    open(os.path.join(d, "f"), "a").close()                           # the file a `<f` reads
    try:
        r = subprocess.run(["bash", "-c", GH_STUB + cmd], capture_output=True, cwd=d, timeout=10, stdin=subprocess.DEVNULL,
                           env={"PATH": os.environ["PATH"], "HOME": d, "ARGV_FILE": af})
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not os.path.exists(af):
        return None
    with open(af, "rb") as f:
        return f.read().decode("utf-8", "replace").split("\0")[:-1]


def bash_argv(cmd):
    with tempfile.TemporaryDirectory(prefix="ptn-argv-") as d:
        return run_with_stub(cmd, d)


class LockedTemp:
    """A temp folder, locked with flock while it is in use (so systemd-tmpfiles ageing leaves it alone) and removed on exit."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ptn-diff-")
        self.fd = os.open(self.tmp.name, os.O_RDONLY)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self.tmp.name

    def __exit__(self, *exc):
        os.close(self.fd)
        self.tmp.cleanup()


def diff_one(cmd, d, tally, hits):
    if _DIFF_MODULE.trusted_create(cmd) is None:
        return
    tally[0] += 1
    argv = run_with_stub(cmd, d)
    if argv is None:
        return
    tally[1] += 1
    if argv_has_quiet_flag(argv):
        hits.append((cmd, argv))


def diff_random_command(rng):
    kind = rng.random()
    create = "gh " + rng.choice(["pr", "pr", "pr", "issue"]) + " create"
    if kind < 0.4:
        sep = rng.choice([" ", " ", "\t", " \\\n "])
        return rng.choice(DIFF_PREFIX) + create + sep + sep.join(rng.choice(DIFF_POOL) for _ in range(rng.randint(0, 5)))
    if kind < 0.55:
        return rng.choice(DIFF_PREFIX) + create + " " + "".join(rng.choice(DIFF_RAW) for _ in range(rng.randint(1, 6)))
    if kind < 0.8:                                # a value flag, then the word that a real gh gives it, then more flags
        words = [rng.choice(DIFF_VALUE_FLAGS), rng.choice(DIFF_QUOTED_QUIET + DIFF_GOOD), rng.choice(DIFF_POOL), rng.choice(DIFF_POOL)]
        return rng.choice(DIFF_PREFIX) + create + " " + " ".join(words[:rng.randint(2, 4)])
    words = " ".join(rng.choice(DIFF_GOOD + DIFF_BAD[:12]) for _ in range(rng.randint(1, 3)))
    return rng.choice(DIFF_PREFIX) + create + " " + words + rng.choice(["", " --fill", " # c"])


def diff_random_chunk(args):
    seed, n = args
    rng = random.Random(seed)
    tally, hits = [0, 0], []
    with LockedTemp() as d:
        for _ in range(n):
            diff_one(diff_random_command(rng), d, tally, hits)
    return n, tally[0], tally[1], hits


def diff_exhaustive_chunk(args):
    first, length = args
    tally, hits = [0, 0], []
    with LockedTemp() as d:
        for tail in itertools.product(TOKENS, repeat=length - 1):
            for prefix in ("gh pr create ", "W=--web; HOME=--web; gh pr create "):
                diff_one(prefix + first + "".join(tail), d, tally, hits)
    return len(TOKENS) ** (length - 1), tally[0], tally[1], hits


def pool_sum(function, jobs):
    """Run `function` over `jobs` in forked workers; the sums of its (count, trusted, ran) and its hits joined."""
    totals, hits = [0, 0, 0], []
    with multiprocessing.get_context("fork").Pool(max(1, min(20, os.cpu_count() or 1))) as pool:
        for count, trusted, ran, found in pool.imap_unordered(function, jobs):
            totals[0] += count; totals[1] += trusted; totals[2] += ran; hits += found
    return totals[0], totals[1], totals[2], hits


def run_differential(module, seed, count):
    global _DIFF_MODULE
    _DIFF_MODULE = module
    per, rest = divmod(count, 20)
    return pool_sum(diff_random_chunk, [(seed * 100 + i, per + (rest if i == 0 else 0)) for i in range(20)])


def run_exhaustive(module, length):
    """Every string of up to `length` TOKENS after `gh pr create ` (and after a prefix that sets W and HOME to a flag)."""
    global _DIFF_MODULE
    _DIFF_MODULE = module
    return pool_sum(diff_exhaustive_chunk, [(t, n) for n in range(1, length + 1) for t in TOKENS])


class Whitelist(unittest.TestCase):
    """The words of a create are a short whitelist (`words_are_literal`): the shapes outside it are untrusted, the boring ones stay trusted,
    and a differential against real bash with a stand-in `gh` that prints its argv shows no trusted command runs with a quiet flag."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module("ptn_whitelist")

    def trusted(self, cmd):
        return self.m.trusted_create(cmd) is not None

    def assertAllUntrusted(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.trusted(cmd))

    def assertAllTrusted(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertTrue(self.trusted(cmd))

    def test_a_redirect_glued_to_a_word_cannot_hide_a_quiet_flag(self):
        # bash ends the word at `<` or `>`: `--web<f` is the flag `--web` and a redirect
        self.assertAllUntrusted(["gh pr create --web<f", "gh pr create -w<f", "gh pr create --dry-run<<<x", "gh pr create --help<&-",
                                 "gh pr create --web<<EOF\nx\nEOF", "gh pr create --title t --web</dev/null", "gh pr create --web>f",
                                 "gh pr create --fill>f", "gh pr create --title t -w>&2"])

    def test_a_case_pattern_cannot_end_a_substitution_early(self):
        self.assertAllUntrusted(["gh pr create --title \"$(case a in a) echo '\"' ;; esac)\" --web #'",
                                 "echo \"$(case a in a) echo x ;; esac)\"; gh pr create --fill", "echo `case a in a) x;; esac`; gh pr create --fill",
                                 "echo \"$(echo case)\"; gh pr create --fill", "echo \"$(echo case; x)\"; gh pr create --fill",
                                 "echo \"$(echo case&&x)\"; gh pr create --fill", "echo \"$(echo case|x)\"; gh pr create --fill",
                                 "echo \"$(echo case\ty)\"; gh pr create --fill", "echo \"$(echo case\nx)\"; gh pr create --fill",
                                 "echo \"$(case)\"; gh pr create --fill", "echo `echo case`; gh pr create --fill"])
        # only the bare word: other words, quoted ones and a heredoc body that holds it are no case statement
        self.assertAllTrusted(["echo \"$(echo cases)\"; gh pr create --fill", "echo \"$(echo xcase)\"; gh pr create --fill",
                               "echo \"$(echo 'case')\"; gh pr create --fill", "echo \"$(echo ca\\se)\"; gh pr create --fill",
                               "gh pr create --title t --body \"$(cat <<'EOF'\nin that case a) b\ncase x in\nEOF\n)\""])

    def test_an_argument_list_that_can_vanish_cannot_swallow_a_flag(self):
        # `"$@"` expands to no word at all: `-t "$@" -b "$W"` is `-t -b --web` when $W is --web
        self.assertAllUntrusted(['gh pr create -t "$@" -b "$W"', 'gh pr create -l "$@" -a "$W"', 'gh pr create -t "$*" -b "$W"', 'gh pr create --title "$@" --fill',
                                 'gh pr create --title="$@" --fill', 'gh pr create "$@"', 'gh pr create "$*"', 'gh pr create -t "${@}" --fill',
                                 'gh pr create -t "x $@" --fill', 'gh pr create -t $@ --fill', 'gh pr create -t $* --fill'])

    def test_a_leading_tilde_is_an_expansion(self):
        self.assertAllUntrusted(["HOME=--web; gh pr create ~", "export HOME=-w; gh pr create ~", "HOME=--dry-run; gh pr create ~/x", "gh pr create ~",
                                 "gh pr create --fill ~", "gh pr create -t ~ --fill", "gh pr create --title=~ --fill", "gh pr create -t x~y --fill"])

    def test_the_boring_shapes_stay_trusted(self):
        self.assertAllTrusted(["gh pr create --body \"$(cat <<'EOF'\nthe body, with --web and `x` and $(y) and $HOME in it\nEOF\n)\" --title t",
                               'gh pr create --title "x $Y"', "gh pr create --fill", "cd dir && git push && gh pr create --title t --body b",
                               "gh issue create --title 'a b' --body 'c'", "gh pr create --title t --body-file - <<'EOF'\nbody\nEOF", "gh pr create --fill 2>&1",
                               "gh pr create --fill 2>/dev/null", "W=1 gh -R x/y pr create --fill", "gh pr create --fill # a > b ~ $(x) --web",
                               "gh pr create --title \"feat(x): a; b > c & d | e\" --fill", "gh pr create --title 'a ~ b < c' --fill",
                               "gh.exe pr create --fill", "gh pr create\t--fill", "gh\tpr\tcreate\t-t\tx", 'gh pr create --title="a $B" --fill',
                               'gh pr create --title "${B}" --fill', 'gh pr create -t "$(cat <<\'E_1\'\nx\nE_1\n)"'])

    def test_the_forms_the_whitelist_refuses_are_untrusted(self):
        self.assertAllUntrusted(["gh pr create --fill > f", "gh pr create --fill >> f", "gh pr create --fill < f", "gh pr create --fill >&2",
                                 "gh pr create --fill <&3", "gh pr create --fill >| f", "gh pr create --fill &> f", "gh pr create --fill 2> f",
                                 "gh pr create --fill 2>>f", "gh pr create --fill 2>&1 > f", "gh pr create --fill <<EOF\nx\nEOF", "gh pr create --fill <<-EOF\nx\nEOF",
                                 "gh pr create --fill <<<x", "gh pr create --fill 2>$F", "gh pr create --fill 2>&$F", "gh pr create --fill 2>f", "gh pr create --fill 2>/dev/nul", "gh pr create --fill 2>>/dev/null",
                                 "gh pr create --fill 2>&2", "gh pr create --fill 2>/dev/null2", "gh pr create --fill 1>&2", "gh pr create --fill 2> /dev/null", "gh pr create --fill 2>&1x",
                                 "A=1>f gh pr create --fill", "A=~ gh pr create --fill", "A=$B gh pr create --fill", "A=`x` gh pr create --fill",
                                 'A="$(x)" gh pr create --fill', "gh pr create *", "gh pr create ?", "gh pr create [w]",
                                 "gh pr create {--web,}", "gh pr create a}", "gh pr create a]", "gh pr create !", "gh pr create -t !x --fill",
                                 "gh pr create $W", 'gh pr create "$W"', "gh pr create -t $T", "gh pr create --body \"$(git log -1)\"",
                                 "gh pr create --title \"`git log -1`\"", 'gh pr create "$(cat <<\'EOF\'\nx\nEOF\n)"', 'gh pr create --title "a$(cat <<\'EOF\'\nx\nEOF\n)"',
                                 "gh pr create --fill $(x)", "gh pr create --fill `x`", 'gh pr create -R "$R" --fill "$X"', 'gh pr create --fill "a $B"'])

    def test_a_create_whose_words_are_not_a_create_at_all_is_untrusted(self):
        # no `gh` word before the arguments: nothing for the whitelist to read (words_are_literal itself, not the segment pattern)
        for cmd in ("A=1 B=2", "echo x", "A=1", "", "x pr create --fill"):
            with self.subTest(cmd=cmd):
                segs = self.m.shape_of(cmd)
                self.assertFalse(segs and self.m.words_are_literal(segs[-1][3]))
        segs = self.m.shape_of("A=1 gh pr create --fill")
        self.assertTrue(self.m.words_are_literal(segs[-1][3]))

    def test_a_tab_is_a_blank_between_the_words(self):
        self.assertAllUntrusted(["gh pr create\t--web", "gh pr create --fill\t--web", "gh pr create --fill\t-w", "gh\tpr\tcreate\t--dry-run",
                                 "gh pr create\t-t\tx\t--help"])
        self.assertAllTrusted(["gh pr create -t\t'--web'", "gh pr create\t--title\t'--web'"])    # the value of -t is a title, never a flag

    def test_every_flag_that_takes_a_value_takes_the_next_word(self):
        for flag in sorted(self.m.VALUE_FLAGS):
            with self.subTest(flag=flag):
                self.assertTrue(self.trusted(f"gh pr create {flag} '--web'"))      # --web is its value
                self.assertFalse(self.trusted(f"gh pr create {flag} x --web"))     # and the next word is a flag again
                self.assertTrue(self.trusted(f"gh pr create {flag} '-w' --fill"))
                self.assertFalse(self.trusted(f"gh pr create {flag} --web"))       # a plain quiet word is never trusted, even where it would be a value
                self.assertFalse(self.trusted(f"gh pr create {flag}"))            # a value that never comes
        self.assertEqual(self.m.VALUE_FLAGS, frozenset(("--title", "-t", "--body", "-b", "--body-file", "-F", "--repo", "-R", "--base", "-B", "--head", "-H",
                                                        "--label", "-l", "--assignee", "-a")))
        for flag in ("-d", "--draft", "--fill", "-f"):                              # these take none
            with self.subTest(flag=flag):
                self.assertFalse(self.trusted(f"gh pr create {flag} '--web'"))     # no value to swallow it: the quoted --web is the flag
                self.assertFalse(self.trusted(f"gh pr create {flag} -w"))
                self.assertTrue(self.trusted(f"gh pr create {flag} --fill"))

    def test_the_value_of_a_flag_may_hold_a_name_only_inside_double_quotes(self):
        for flag in sorted(self.m.VALUE_FLAGS):
            with self.subTest(flag=flag):
                self.assertTrue(self.trusted(f'gh pr create {flag} "x $Y" --fill'))
                self.assertFalse(self.trusted(f"gh pr create {flag} x$Y --fill"))
                self.assertFalse(self.trusted(f"gh pr create {flag} $Y --fill"))
                self.assertTrue(self.trusted(f"gh pr create {flag} 'x $Y' --fill"))
                self.assertFalse(self.trusted(f'gh pr create {flag} "x `y`" --fill'))
        for flag in ("--title", "--body", "--body-file", "--repo", "--base", "--head", "--label", "--assignee"):
            with self.subTest(flag=flag):
                self.assertTrue(self.trusted(f'gh pr create {flag}="x $Y" --fill'))
                self.assertFalse(self.trusted(f"gh pr create {flag}=$Y --fill"))
                self.assertFalse(self.trusted(f"gh pr create {flag}=x`y` --fill"))
        self.assertFalse(self.trusted('gh pr create --fill="x $Y"'))                # not a flag that takes a value
        self.assertFalse(self.trusted('gh pr create -d"x $Y"'))

    def test_the_quiet_words_are_read_whole(self):
        # a cluster of short flags is quiet when it holds w or h, whatever the other letters are; a value after `=` and a newline in it change nothing
        self.assertAllUntrusted(["gh pr create -w", "gh pr create -h", "gh pr create -dw", "gh pr create -wd", "gh pr create -Dw", "gh pr create -wD", "gh pr create -dh",
                                 "gh pr create -hd", "gh pr create -xhx", "gh pr create -Zw", "gh pr create -wZ",
                                 "gh pr create --web=1", "gh pr create '--web=a\nb'", "gh pr create '--dry-run=a\nb'", "gh pr create '--help=\n'",
                                 "gh pr create --web=", "gh pr create --dry-run", "gh pr create --help"])
        self.assertAllTrusted(["gh pr create -d", "gh pr create -df", "gh pr create --draft", "gh pr create --fill --draft"])
        # a flag that gh does not have is untrusted (the scanner knows gh's own flags and nothing else), `--website` and `--webs` among them
        self.assertAllUntrusted(["gh pr create -dD", "gh pr create -x", "gh pr create --website", "gh pr create --fill --webs", "gh issue create -d", "gh issue create --fill"])

    QUIET_CLUSTERS = ("-wt1", "-wb1", "-wt-x", "-wR1", "-dwt1", "-fwt1", "-ewt1", "-hb1", "-hl1", "-wH.", "-w1", "-w.", "-w/x", "-w_", "-h1", "-wt1.2", "-dh/x",
                      "-fh_x", "-wb-1", '-w"t"1', "-\\wt1", "-w't'1")

    def test_a_short_flag_cluster_with_a_value_tail_still_holds_its_quiet_flag(self):
        # gh lets a flag that takes a value swallow the rest of its word, so `-wt1` is `--web` and `-t 1`: a w or an h among the leading letters of a
        # one-dash word is quiet whatever follows the letters (a digit, `-`, `.`, `/`, `_`)
        for verb in ("pr", "issue"):
            self.assertAllUntrusted([f"gh {verb} create {w}" for w in self.QUIET_CLUSTERS])
            self.assertAllUntrusted([f"gh {verb} create --title t {w} --body b" for w in self.QUIET_CLUSTERS])
            self.assertAllUntrusted([f"echo U; gh {verb} create --fill {w}" for w in self.QUIET_CLUSTERS])
        self.assertAllUntrusted(["gh pr create -t x -wt1", "gh pr create -d -hb1", "gh pr create -R x/y -wH."])

    def test_a_value_tail_that_holds_no_quiet_flag_stays_trusted(self):
        # the letters before the value's first character hold no w and no h: `-t1`, `-b.x` and `-dt1` are a title, a body and a draft with a title
        self.assertAllTrusted(["gh pr create -t1", "gh pr create -b1", "gh pr create -dt1", "gh pr create -fb.x", "gh pr create -R1", "gh pr create -l1_2",
                               "gh pr create -t1w", "gh pr create -t/h", "gh pr create -b-w", "gh pr create -t.h"])

    def test_the_boring_controls_stay_trusted(self):
        for verb in ("pr", "issue"):
            self.assertAllTrusted([f"gh {verb} create --title t --body b", f"gh {verb} create -t title -b body"])
        self.assertAllTrusted(["gh pr create -d", "gh pr create --fill", "gh pr create --title t --body b", "gh pr create -t title -b body", "gh pr create -d --fill"])

    def test_create_is_matched_as_a_whole_word_between_plain_blanks(self):
        # `\b` after `create` and `\s` between the words let `create--web`, an NBSP, a CR or a form feed through; bash sees one word for each of them
        self.assertAllUntrusted(["gh pr create--web --title t", "gh pr create--title t", "gh pr new--web", "gh pr create-x --fill", "gh pr create.x --fill",
                                 "gh pr\xa0create --title t", "gh\xa0pr create --title t", "gh pr\u2003create --title t", "gh pr create\r --title t",
                                 "gh pr create\r", "gh pr create\x0c--title t", "gh pr\rcreate --fill", "gh pr\x0bcreate --fill", "gh pr create\xa0--title t",
                                 "cd\xa0d && gh pr create --fill", "cd\rd && gh pr create --fill", "A=x\xa0y gh pr create --fill", "echo\xa0x; gh pr create --fill"])
        self.assertAllTrusted(["gh pr create", "gh pr new", "gh issue create", "gh pr create ", "gh pr create\t--fill", "gh pr create\n", "gh  pr   create  --fill",
                               "gh\tpr\tcreate", "cd d && gh pr create --fill", "A=1\tgh pr create --fill"])

    def test_the_segment_pattern_alone_takes_only_space_and_tab_between_its_words(self):
        # words_are_literal also refuses these, so the pattern is pinned on its own (it is the second line of defence)
        for text, expected in (("gh pr create --fill", True), ("gh pr new", True), ("gh pr create", True), ("A=1\tgh pr create", True), ("gh -R x pr create", True),
                               ("gh -R x\tpr create", True), ("gh\xa0pr create", False), ("gh pr\xa0create", False), ("gh pr create\xa0x", False),
                               ("gh pr create\rx", False), ("gh pr create\x0cx", False), ("gh pr create-x", False), ("gh pr createx", False),
                               ("A=x\xa0gh pr create", False), ("gh -R\xa0x pr create", False), ("gh -R x\xa0pr create", False), ("gh\x0bpr create", False)):
            with self.subTest(text=text):
                self.assertEqual(self.m.CREATE_SEGMENT.match(text) is not None, expected)

    def test_a_no_break_space_or_a_carriage_return_does_not_split_a_command_word_before_the_create(self):
        # `A=x<NBSP>rm y` is one assignment word for bash; split on any whitespace it would read as `A=x`, then `rm`... hidden from the allowlist
        self.assertAllUntrusted(["A=x\xa0rm y; gh pr create --fill", "A=x\xa0cd d; gh pr create --fill", "A=x\rcd d; gh pr create --fill",
                                 "echo\xa0x; gh pr create --fill", "cd\x0cd; gh pr create --fill", "git\xa0push && gh pr create --fill"])
        self.assertAllTrusted(["A=x cd d; gh pr create --fill", "A=x\tcd d; gh pr create --fill", "cd d && git\tpush && gh pr create --fill"])

    VALUE_FLAG_REPRODUCTIONS = ('echo U; gh pr create -m -t "-w"', 'echo U; gh pr create -dt -b "-w"', 'W=--web; echo U; gh pr create -dt -b "$W"',
                                'W=--web; echo U; gh pr create -ft -B "$W"', "echo U; gh pr create -r '-t' -hlm.w''", 'echo U; gh issue create --type -b "-w"')

    def test_a_value_flag_the_scanner_did_not_know_still_takes_the_next_word(self):
        # gh gives the next word to `-m`, `-p`, `-r`, `-T`, `--type` and the like, and to a cluster that ends in a value letter (`-dt`): a quoted
        # quiet word after one of them is a flag of its own, and a plain one is a value the scanner has to keep out of a flag position
        self.assertAllUntrusted(list(self.VALUE_FLAG_REPRODUCTIONS))
        for cmd in self.VALUE_FLAG_REPRODUCTIONS:
            with self.subTest(cmd=cmd):
                argv = bash_argv(cmd)
                self.assertTrue(argv_has_quiet_flag(argv), argv)               # real bash, a stand-in gh: the quiet flag does reach gh

    def test_every_flag_of_a_create_takes_what_gh_gives_it(self):
        for verb in ("pr", "issue"):
            table = self.m.GH_FLAGS[verb]
            keep = "--fill" if verb == "pr" else "--title t"
            for name in sorted(table["value_long"]):
                flag = "--" + name
                with self.subTest(verb=verb, flag=flag):
                    self.assertTrue(self.trusted(f"gh {verb} create {flag} '-w' {keep}"))        # the next word is its value, even a quoted -w
                    self.assertTrue(self.trusted(f"gh {verb} create {flag} -x {keep}"))          # and a plain one that is no quiet flag
                    self.assertFalse(self.trusted(f"gh {verb} create {flag} x -w"))               # the word after the value is a flag again
                    self.assertFalse(self.trusted(f"gh {verb} create {flag} x '-w'"))
                    self.assertFalse(self.trusted(f"gh {verb} create {flag} -w"))                 # a plain quiet word is never trusted
                    self.assertFalse(self.trusted(f"gh {verb} create {flag}"))                    # a value that never comes
                    self.assertTrue(self.trusted(f"gh {verb} create {flag}=x {keep}"))
                    self.assertFalse(self.trusted(f"gh {verb} create {flag}=x '-w'"))
                    self.assertEqual(self.trusted(f'gh {verb} create {flag} "$V" {keep}'), flag in self.m.VALUE_FLAGS)   # an expansion: only the old eight
                    self.assertEqual(self.trusted(f'gh {verb} create {flag}="$V" {keep}'), flag in self.m.VALUE_FLAGS)
            for name in sorted(table["bool_long"]):
                flag = "--" + name
                with self.subTest(verb=verb, flag=flag):
                    self.assertTrue(self.trusted(f"gh {verb} create {flag} --title t"))
                    self.assertFalse(self.trusted(f"gh {verb} create {flag} '-w'"))                # no value to swallow it
                    self.assertFalse(self.trusted(f"gh {verb} create {flag}=true --title t"))
            for ch in sorted(table["value_short"]):
                flag = "-" + ch
                with self.subTest(verb=verb, flag=flag):
                    self.assertTrue(self.trusted(f"gh {verb} create {flag} '-w' {keep}"))
                    self.assertFalse(self.trusted(f"gh {verb} create {flag} x -w"))
                    self.assertFalse(self.trusted(f"gh {verb} create {flag}"))
                    self.assertTrue(self.trusted(f"gh {verb} create {flag}x1 {keep}"))            # the rest of the word is the value
                    self.assertFalse(self.trusted(f"gh {verb} create {flag}x1 '-w'"))
                    self.assertEqual(self.trusted(f'gh {verb} create {flag} "$V" {keep}'), flag in self.m.VALUE_FLAGS)
                    for lead in sorted(table["bool_short"]):                                       # a cluster that ends in the value letter
                        cluster = f"-{lead}{ch}"
                        self.assertTrue(self.trusted(f"gh {verb} create {cluster} '-w' {keep}"), cluster)
                        self.assertFalse(self.trusted(f"gh {verb} create {cluster} x -w"), cluster)
                        self.assertFalse(self.trusted(f"gh {verb} create {cluster}"), cluster)
                        self.assertFalse(self.trusted(f'gh {verb} create {cluster} "-b" "$V"'), cluster)
            for ch in sorted(table["bool_short"]):
                with self.subTest(verb=verb, flag="-" + ch):
                    self.assertFalse(self.trusted(f"gh {verb} create -{ch} '-w'"))

    def test_a_flag_word_the_table_does_not_know_is_untrusted(self):
        # a whitelist, not a blacklist: the flags of the other kind of create, a made-up one, `--`, a lone dash and a bool flag given a value
        self.assertAllUntrusted(["gh pr create --type x", "gh pr create --parent 1", "gh pr create --blocking 1", "gh issue create --fill", "gh issue create -d",
                                 "gh issue create --base main", "gh issue create -r x", "gh issue create -B main", "gh issue create -H x", "gh pr create --bogus",
                                 "gh pr create --bogus x", "gh pr create --bogus=x", "gh pr create -x", "gh pr create -dx", "gh pr create --", "gh pr create -",
                                 "gh pr create --fill=true", "gh pr create -d=true", "gh pr create -t x -k", "gh pr create -t x --", "gh pr create -t=x -9",
                                 "gh pr create --title t --nope", "gh -x y pr create --fill", "gh --nope x pr create --fill"])
        self.assertAllTrusted(["gh pr create --title t --fill", "gh -R x/y pr create --fill", "gh --repo x/y issue create --title t"])

    def test_an_expansion_stands_only_in_the_value_of_the_original_eight_flags(self):
        self.assertAllUntrusted(['gh pr create -m "$M" --fill', 'gh pr create --milestone "$M" --fill', 'gh pr create --milestone="$M" --fill',
                                 'gh pr create -p "$P" --fill', 'gh pr create -r "$R" --fill', 'gh pr create -T "$T" --fill', 'gh pr create --reviewer "$R" --fill',
                                 'gh pr create --template "$T" --fill', 'gh pr create --recover "$R" --fill', 'gh pr create --attach "$A" --fill',
                                 'gh issue create --type "$T" --title t', 'gh issue create --parent "$P" --title t', 'gh issue create --blocked-by "$B" --title t',
                                 'gh issue create --blocking "$B" --title t', 'gh pr create -dm "$M" --fill', 'gh pr create -fp "$P" --fill'])
        self.assertAllTrusted(['gh pr create -a "$A" --fill', 'gh pr create -dt "$T" --fill', 'gh pr create -fB "$B" --fill', 'gh pr create -dH "$H" --fill',
                               'gh pr create -fl "$L" --fill', 'gh pr create -R "$R" --fill', 'gh pr create -F "$F" --fill', 'gh issue create -a "$A" --title t'])

    def test_the_table_is_the_set_of_flags_gh_2_103_lists(self):
        # the flags written out by hand from `gh pr create --help` and `gh issue create --help` of gh 2.103 (the installed gh of a test machine may be older,
        # and the loops above read the table itself), so that dropping or adding a flag in the hook's table fails here
        expected = {"pr": {"value_long": {"assignee", "attach", "base", "body", "body-file", "head", "label", "milestone", "project", "recover", "reviewer",
                                          "template", "title", "repo"},
                           "bool_long": {"draft", "editor", "fill", "fill-first", "fill-verbose", "no-maintainer-edit"},
                           "value_short": set("aBbFHlmprTtR"), "bool_short": set("def")},
                    "issue": {"value_long": {"assignee", "attach", "blocked-by", "blocking", "body", "body-file", "label", "milestone", "parent", "project",
                                             "recover", "template", "title", "type", "repo"},
                              "bool_long": {"editor"}, "value_short": set("abFlmpTtR"), "bool_short": {"e"}}}
        self.assertEqual({v: {k: set(x) for k, x in t.items()} for v, t in self.m.GH_FLAGS.items()}, expected)

    def test_a_flag_of_the_other_create_with_an_equals_sign_is_untrusted(self):
        self.assertAllUntrusted(["gh issue create --base=main --title t", "gh issue create --head=x --title t", "gh issue create --reviewer=x --title t",
                                 "gh pr create --type=x --fill", "gh pr create --parent=1 --fill", "gh pr create --blocking=1 --fill"])

    def test_a_cluster_with_a_w_or_an_h_before_a_value_letters_end_is_untrusted_even_where_gh_reads_a_value(self):
        # conservative on purpose: `-tw` is a title `w` for gh, but the scanner cannot tell the rest of a word from a flag, so it never trusts one
        self.assertAllUntrusted(["gh pr create -tw --fill", "gh pr create -bh --fill", "gh pr create -Rhub/x --fill", "gh pr create -dtw", "gh pr create -mh"])

    def test_the_table_lists_every_flag_the_installed_gh_lists(self):
        gh = shutil.which("gh")
        if not gh:
            self.skipTest("no gh on this machine")
        quiet = {"web", "dry-run", "help", "w", "h"}
        for verb in ("pr", "issue"):
            r = subprocess.run([gh, verb, "create", "--help"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                               env={"PATH": os.environ["PATH"], "HOME": tempfile.gettempdir(), "NO_COLOR": "1", "GH_PROMPT_DISABLED": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            table = self.m.GH_FLAGS[verb]
            seen = 0
            for line in r.stdout.splitlines():
                m = re.match(r"\s+(?:-(\w), )?--([\w-]+)(?: (\S+))?\s{2,}", line)
                if not m:
                    continue
                short, name, arg = m.groups()
                seen += 1
                with self.subTest(verb=verb, flag=name):
                    if name in quiet:
                        continue
                    if arg:
                        self.assertIn(name, table["value_long"])
                        if short:
                            self.assertIn(short, table["value_short"])
                    else:
                        self.assertIn(name, table["bool_long"])
                        if short and short not in quiet:
                            self.assertIn(short, table["bool_short"])
            self.assertGreater(seen, 8)

    def test_a_real_gh_gives_the_next_word_to_every_value_flag_in_the_table(self):
        # real gh, offline: an unknown flag is refused before anything else happens, so "unknown flag: --bogus" in stderr means that `--bogus`
        # was read as a flag, and its absence that the flag before it took it as its value
        gh = shutil.which("gh")
        if not gh:
            self.skipTest("no gh on this machine")
        with tempfile.TemporaryDirectory(prefix="ptn-gh-") as d:
            env = {"PATH": os.environ["PATH"], "HOME": d, "GH_CONFIG_DIR": d, "GH_TOKEN": "x", "HTTPS_PROXY": "http://127.0.0.1:9", "GH_PROMPT_DISABLED": "1",
                   "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}

            def refused(*args):
                r = subprocess.run([gh, *args], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, cwd=d, env=env)
                return "unknown flag: --bogus" in r.stderr, r.stderr

            self.assertTrue(refused("pr", "create", "--bogus")[0])                 # the control: a flag position refuses it
            for verb in ("pr", "issue"):
                table = self.m.GH_FLAGS[verb]
                for flag in ["--" + n for n in sorted(table["value_long"])] + ["-" + c for c in sorted(table["value_short"])]:
                    _seen, err = refused(verb, "create", flag, "x")
                    if "unknown flag" in err or "unknown shorthand" in err:
                        continue                                                    # a flag this (older) gh does not have
                    with self.subTest(verb=verb, flag=flag):
                        self.assertFalse(refused(verb, "create", flag, "--bogus")[0])
                for lead in sorted(table["bool_short"]):
                    for ch in sorted(table["value_short"]):
                        with self.subTest(verb=verb, cluster=lead + ch):
                            self.assertFalse(refused(verb, "create", f"-{lead}{ch}", "--bogus")[0])
                for flag in ["--" + n for n in sorted(table["bool_long"])] + ["-" + c for c in sorted(table["bool_short"])]:
                    seen, err = refused(verb, "create", flag, "--bogus")
                    if not seen and ("unknown flag" in err or "unknown shorthand" in err):
                        continue                                                    # a flag this (older) gh does not have
                    with self.subTest(verb=verb, flag=flag):
                        self.assertTrue(seen)                                       # a bool flag takes nothing: `--bogus` is a flag again

    def test_a_redirect_of_the_creates_output_is_untrusted_even_to_dev_null(self):
        self.assertAllUntrusted(["gh pr create --fill >/dev/null", "gh pr create --fill > /dev/null", "gh pr create --fill 1>/dev/null", "gh pr create --fill >>/dev/null",
                                 "gh issue create --fill >/dev/null"])
        self.assertAllTrusted(["gh pr create --fill 2>/dev/null"])

    def test_only_the_cat_heredoc_is_a_substitution_a_value_may_hold(self):
        body = "gh pr create --title t --body \"$(%s)\""
        for sub in ("sh <<'EOF'\nx\nEOF\n", "cat <<EOF\n$(x)\nEOF\n", "cat <<EOF\nx\nEOF\n", "cat <<'EOF'\nx\nEND\n", "cat <<'EOF'\nx\nEOF\n ", "cat <<'EOF'\nEOF\nmore\nEOF\n",
                    "cat <<'EOF'\nx\nEOF", "cat f", "cat <<'EOF'\nx\nEOF\necho --web\n", "git log -1"):
            with self.subTest(sub=sub):
                self.assertFalse(self.trusted(body % sub))
        self.assertTrue(self.trusted(body % "cat <<'EOF'\nx\nEOF\n"))
        self.assertTrue(self.trusted(body % "cat <<'EOF'\nEOF\n"))                           # an empty body
        self.assertTrue(self.trusted(body % "cat <<'E_1'\nEOF\nx\nE_1\n"))                     # EOF is a line of the body here, not the delimiter

    def test_heredoc_value_reads_the_whole_chunk(self):
        for chunk, expected in (("\"$(cat <<'EOF'\nx\nEOF\n)\"", True), ("\"$(cat <<'EOF'\n)\"", False), ("\"$(cat <<'EOF'\nEOF)\"", False),
                                ("\"$(cat <<'EOF'\nx\nEND\n)\"", False), ("\"$(cat <<'EOF'\nx\nEOF\n )\"", False), ("\"$(cat <<'EOF'\nEOF\nx\nEOF\n)\"", False),
                                ("\"$(sh <<'EOF'\nx\nEOF\n)\"", False), ("\"$(cat <<EOF\nx\nEOF\n)\"", False), ("\"a$(cat <<'EOF'\nx\nEOF\n)\"", False),
                                ("\"$(cat <<'EOF'\nx\nEOF\n)b\"", False), ("\"$(cat <<'EOF'\nEOF\n)\"", True)):
            with self.subTest(chunk=chunk):
                self.assertEqual(self.m.heredoc_value(chunk), expected)

    def test_words_are_literal_reads_a_word_list_that_holds_a_gh(self):
        def literal(cmd):
            segs = self.m.shape_of(cmd)
            return bool(self.m.words_are_literal(segs[-1][3]))
        for cmd, expected in (("A=1 gh pr create --fill", True), ("gh.exe pr create --fill", True), ("echo gh pr create --fill", False), ("x pr create --fill", False),
                              ('"gh" pr create --fill', False), ("g\\h pr create --fill", False), ("1A=x gh pr create --fill", False), ("A gh pr create --fill", False), ("ghx pr create --fill", False), ("gh2 pr create --fill", False), ("gh. pr create --fill", False),
                              ("A=1 B=2", False), ("echo x", False), ("A='x y' gh pr create --fill", True), ('A="x $Y" gh pr create --fill', True),
                              ("A=$Y gh pr create --fill", False), ("A=`y` gh pr create --fill", False), ('A="`y`" gh pr create --fill', False)):
            with self.subTest(cmd=cmd):
                self.assertEqual(literal(cmd), expected)

    def test_a_quoted_or_escaped_gh_is_no_create(self):
        self.assertAllUntrusted(['"gh" pr create --fill', "'gh' pr create --fill", "g\\h pr create --fill", "1A=x gh pr create --fill"])

    def test_the_word_case_is_only_a_hazard_inside_a_substitution(self):
        self.assertAllTrusted(["echo case; gh pr create --fill", "echo case && gh pr create --fill", "gh pr create --title case --fill",
                               "gh pr create --title t --body case", "echo \"case\"; gh pr create --fill"])
        # an operator ends the word before it: `<case` and `|case` start a word that is `case`
        self.assertAllUntrusted(["echo \"$(echo <case)\"; gh pr create --fill", "echo \"$(echo x|case)\"; gh pr create --fill",
                                 "echo \"$(echo x >&2 case)\"; gh pr create --fill", "echo `echo >case`; gh pr create --fill"])

    def test_assignments_before_a_command_do_not_hide_it_from_the_allowlist(self):
        self.assertAllTrusted(["W=1 echo x; gh pr create --fill", "A=1; gh pr create --fill", "A=1 B=2 git push && gh pr create --fill", "W=--web; echo U; gh pr create --fill"])
        self.assertAllUntrusted(["A=1 rm x; gh pr create --fill", "rm x; gh pr create --fill", "A=1 B=2 curl x && gh pr create --fill"])

    def test_the_bracket_and_brace_characters_are_refused_alone(self):
        self.assertAllUntrusted(["gh pr create [", "gh pr create {", "gh pr create x[", "gh pr create x{", "gh pr create ]", "gh pr create }", "gh pr create -t [ --fill",
                                 "gh pr create --title=[ --fill", "gh pr create --title={ --fill"])
        self.assertAllTrusted(["gh pr create --title '[x]' --fill", "gh pr create --title \"{x}\" --fill", "gh pr create --title \\[x --fill"])

    def test_the_stand_in_gh_shows_the_flags_bash_builds(self):
        # the claim behind the differential below: with `gh` a function that prints its argv, bash's argv holds the quiet flag for each finding
        for cmd, flag in (("gh pr create --web<f", "--web"), ("gh pr create --title t --web</dev/null", "--web"), ('W=--web; gh pr create -t "$@" -b "$W"', "--web"),
                          ("HOME=--web; gh pr create ~", "--web"), ("gh pr create --title \"$(case a in a) echo '\"' ;; esac)\" --web #'", "--web")):
            with self.subTest(cmd=cmd):
                self.assertIn(flag, bash_argv(cmd))

    def test_bash_agrees_with_the_whitelist_on_generated_commands(self):
        count = int(os.environ.get("PTN_DIFF_COUNT", "30000"))
        generated, trusted, ran, hits = run_differential(self.m, 20260101, count)
        self.assertEqual(generated, count)
        self.assertGreater(trusted, count // 10)        # the generator does reach the trusted shapes
        self.assertGreater(ran, trusted // 2)
        self.assertEqual(hits, [])

    def test_bash_agrees_with_the_whitelist_on_every_short_string(self):
        length = int(os.environ.get("PTN_EXH_LEN", "4"))
        strings, trusted, ran, hits = run_exhaustive(self.m, length)
        self.assertEqual(strings, sum(len(TOKENS) ** n for n in range(1, length + 1)))
        self.assertGreater(trusted, 0)
        self.assertEqual(hits, [])


class PickingRules(unittest.TestCase):
    """pick_item: a waiting item and an item of another project never count."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module("ptn_pick")

    def pick(self, *items, project=PROJECT, **extra):
        return self.m.pick_item(json.loads(answer(*items, **extra)), {ME}, project)

    def test_a_waiting_item_is_not_the_one(self):
        waits = [{"phase": "waiting"}, {"phase": "dead"}, {"wait_kind": "time"}, {"until": "2099-01-01"}, {"after": ["a1"]},
                 {"wake": "2099-01-01 09:00"}, {"phase": "alive", "until": "2099-01-01"}]
        for meta in waits:
            with self.subTest(meta=meta):
                item, why = self.pick(record("a1", "item-1", ME, f"{ME}: step", **meta))
                self.assertIsNone(item)
                self.assertEqual(why, "your open item as mover is waiting")

    def test_an_alive_item_or_one_with_empty_wait_fields_is_live(self):
        for meta in ({}, {"phase": "alive"}, {"phase": "Alive", "wait_kind": "", "until": None, "after": [], "wake": ""}):
            with self.subTest(meta=meta):
                self.assertIsNotNone(self.pick(record("a1", "item-1", ME, f"{ME}: step", **meta))[0])

    def test_waiting_items_are_named_in_the_plural(self):
        _item, why = self.pick(record("a1", "i1", ME, f"{ME}: a", phase="waiting"), record("a2", "i2", ME, f"{ME}: b", until="2099-01-01"))
        self.assertEqual(why, "your open items as mover are waiting")

    def test_a_waiting_item_counts_toward_neither_the_one_nor_the_several(self):
        live, parked = record("a1", "item-1", ME, f"{ME}: step"), record("a2", "item-2", ME, f"{ME}: other", phase="waiting")
        self.assertEqual(self.pick(live, parked)[0]["id"], "a1")
        self.assertEqual(self.pick(parked, live)[0]["id"], "a1")
        self.assertEqual(self.pick(live, record("a3", "item-3", ME, f"{ME}: more"), parked)[1], "2 open items name you as their mover")

    def test_an_item_of_another_project_is_skipped(self):
        other = record("a1", "item-1", ME, f"{ME}: step", project="projects/elsewhere")
        item, why = self.pick(other)
        self.assertIsNone(item)
        self.assertEqual(why, "your open item as mover is in another project")
        self.assertEqual(self.pick(other, record("a2", "i2", ME, f"{ME}: x", project="projects/else"))[1],
                         "your open items as mover are in another project")
        live = record("a3", "item-3", ME, f"{ME}: step")
        self.assertEqual(self.pick(other, live)[0]["id"], "a3")

    def test_the_project_is_compared_as_the_board_writes_it(self):
        for stored in ("projects/example", "Projects/Example", " projects/example ", "example"):
            with self.subTest(stored=stored):
                self.assertIsNotNone(self.pick(record("a1", "item-1", ME, f"{ME}: step", project=stored))[0])
        self.assertIsNotNone(self.pick(record("a1", "item-1", ME, f"{ME}: step"), project="example")[0])
        self.assertIsNone(self.pick(record("a1", "item-1", ME, f"{ME}: step", project="infra/example"))[0])

    def test_without_a_stored_project_the_server_decides(self):
        it = record("a1", "item-1", ME, f"{ME}: step")
        del it["record"]["metadata"]["project"]
        self.assertIsNotNone(self.pick(it)[0])


class Shapes(unittest.TestCase):
    """`shape_of` returns None for anything it does not fully model, and the segments of the line for what it does."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module("ptn_shapes")

    def test_what_is_modeled_is_cut_like_the_shell_cuts_it(self):
        cases = [("a && b; c | d\ne", ["a", "b", "c", "d", "e"]),
                 ('echo "a; b || c" && x', ['echo "a; b || c"', "x"]),
                 ("echo 'a & b'; y", ["echo 'a & b'", "y"]),
                 ("x # y || z\nw", ["x", "w"]),
                 ("cat <<EOF\nbody || & ((\nEOF\nnext", ["cat <<EOF", "next"]),
                 ("x $(a || b & c) y; z", ["x $(a || b & c) y", "z"]),
                 ("x `a || b` y", ["x `a || b` y"]),
                 ('x "$(a || "b c" )" y; z', ['x "$(a || "b c" )" y', "z"]),
                 ("a 2>&1 | b", ["a 2>&1", "b"]), ("a <<< word; b", ["a <<< word", "b"]), ("", []), (" ; ;\n", [])]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                segs = self.m.shape_of(cmd)
                self.assertIsNotNone(segs)
                self.assertEqual([t for _s, t, _b, _c in segs], want)

    def test_what_is_not_modeled_is_none(self):
        for cmd in ("a || b", "a & b", "a &", "a |& b", "a ;; b", "(a)", "a)", "((1))", "$((1))", "$'a'", '$"a"', "$[1]", "${a:-b}", "${a",
                    "a <(b)", "a >(b)", "a <<", "a << )", "a <<$X", "echo 'abc", 'echo "abc', "echo $(abc", "echo `abc", "echo $(a <<EOF)", 'echo "$( (a) )"', 'echo "$(a; (b) )"',
                    "cat <<EOF\nnever ends", "echo $(a $'b')", 'echo "$((1))"', 'echo "${a b}"', "echo `a $'b'`"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.m.shape_of(cmd))

    def test_the_commands_allowed_before_the_create_are_pinned(self):
        # a command word added here is a command the hook trusts to leave the create's output alone: a change to the list is a decision
        self.assertEqual(self.m.SAFE_BEFORE, frozenset({"cd", "git", "echo", "printf", "cat", "true", ":", "pwd", "ls", "test", "[", "export"}))

    def test_a_create_is_only_a_create_when_its_words_are_plain(self):
        for cmd in ('"gh" pr create --fill', "g\\h pr create --fill", "gh 'pr' create --fill", 'A="x gh pr create" --fill'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.m.trusted_create(cmd))
        self.assertIsNotNone(self.m.trusted_create('A="a b" gh -R "x y" pr create --title "t" --fill'))

    def test_the_words_of_a_create_are_read_as_bash_reads_them(self):
        # a backslash inside double quotes stays in the word: `"--w\eb"` is not `--web` (and, a flag gh does not have, it is untrusted below)
        for cmd in ("gh pr create --title '--web'", 'gh pr create --title "a --web b"', "gh pr create --title=--web",
                    "gh pr create --body-file - <<'EOF'\n--web\nEOF", "gh pr create -t '-w' -b \"-h\"", "gh pr create -B main -H feature --label bug", "gh pr create -R 2>&1 x/y --fill", "gh.exe pr create --fill", "A=1 gh.exe issue new --title 'a'",
                    "gh pr create --fill 2>/dev/null --title 'a'", 'gh pr create -t 2>/dev/null "--web"',
                    'gh pr create -B 2>&1 "$B" --fill',

                    'gh pr create --title "x $Y" --fill', 'gh pr create -t "$T" --body "$(cat <<\'EOF\'\nb\nEOF\n)"', "gh pr create --fill"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.m.trusted_create(cmd))
        for cmd in ('gh pr create "--w\\eb"', "gh pr create $W", 'gh pr create "$W"', "gh pr create {--web,}", "gh pr create --web$X", "gh pr create $(x)", "gh pr create `x`",
                    "gh pr create -$(echo w)", "gh pr create *", 'gh pr create "--web"', "gh pr create --we\\b", "gh pr create --title", "gh pr create -R",
                    "gh pr create --fill --title $Y", "gh pr create --fill -d$X", "gh $G pr create --fill", "gh.exe pr create $W", "W=--web gh.exe pr create $W", 'gh pr create -t"$T" --fill',
                    'gh pr create -R 2>/dev/null -t "--web"', "gh pr create -R < in.txt -t '-w'"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.m.trusted_create(cmd))

    def test_a_nul_byte_is_not_modeled(self):
        for cmd in ("a\x00b", "\x00", "gh pr create --fill\x00", "echo 'a\x00'"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.m.shape_of(cmd))

    def test_a_paren_and_a_heredoc_that_never_ends_are_not_modeled(self):
        for cmd in ("x=(a)", "x=(a); gh pr create --fill", "echo (a)", "gh pr create --fill <<EOF", "gh pr create --fill <<-'EOF'", "a <<EOF; b"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.m.shape_of(cmd))

    def test_a_command_longer_than_the_cap_is_not_modeled(self):
        self.assertEqual(self.m.MAX_COMMAND, 200_000)
        self.assertIsNotNone(self.m.shape_of("a" * self.m.MAX_COMMAND))
        self.assertIsNone(self.m.shape_of("a" * (self.m.MAX_COMMAND + 1)))

    def test_the_scan_is_linear_in_the_redirections_before_the_create(self):
        # each `>` used to join the whole segment so far: 50,000 of them took 13 s, past the hook's own 10 s limit
        for what, cmd in (("redirections", "echo " + "> " * 50_000 + "\ngh pr create --fill"),
                          ("stdout joins", "echo " + ">" * 50_000 + "\ngh pr create --fill"),
                          ("stderr joins", "echo " + "2>&1 " * 20_000 + "\ngh pr create --fill"),
                          ("fd words", "echo " + "2>f " * 30_000 + "\ngh pr create --fill")):
            with self.subTest(what=what):
                t = time.monotonic()
                segs = self.m.shape_of(cmd)
                took = time.monotonic() - t
                self.assertIsNotNone(segs)
                self.assertEqual(segs[-1][1], "gh pr create --fill")
                self.assertLess(took, 2, took)

    def test_substitutions_nested_past_the_recursion_limit_are_untrusted_not_an_error(self):
        deep = "gh pr create --fill --title " + "$(echo " * 3000 + ")" * 3000
        with self.assertRaises(RecursionError):
            self.m.shape_of(deep)                                      # the scanner itself does not catch it ...
        self.assertIsNone(self.m.trusted_create(deep))                 # ... the caller that decides what is trusted does


class ItemsThatDoNotCount(Base):
    def test_a_waiting_item_gets_a_project_wide_anchor_with_the_reason(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step", until="2099-01-01", wait_kind="time", phase="waiting")))
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (your open item as mover is waiting); not tied to a board item. {NOTE_TAG}")
        self.assertEqual(self.posted(), [{"project": PROJECT, "add": [PR], "sender": ME}])

    def test_an_item_of_another_project_gets_a_project_wide_anchor_with_the_reason(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step", project="projects/elsewhere")))
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (your open item as mover is in another project); not tied to a board item. {NOTE_TAG}")
        self.assertEqual(self.posted(), [{"project": PROJECT, "add": [PR], "sender": ME}])

    def test_a_scoped_add_refused_with_a_400_is_retried_once_project_wide(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step")))
        self.ctl("refuse.items", "stackshot: anchors not added [400] Refused: item a7 belongs to projects/elsewhere")
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (the server refused the item, so it is anchored project-wide); "
                               f"not tied to a board item. {NOTE_TAG}")
        self.assertEqual(self.posted(), [{"project": PROJECT, "add": [PR], "sender": ME, "items": ["a7"]},
                                         {"project": PROJECT, "add": [PR], "sender": ME}])
        self.assertNotIn("items", self.posted()[1])                                          # the retried add names no item
        self.assertIn("already anchored", self.run_hook(gh_event(PR)))                     # the marker was written after the retry

    def test_another_refusal_is_not_retried(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step")))
        self.ctl("refuse.items", "stackshot: anchors not added [403] no")
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, OLDER.format(url=PR) + " Not anchored automatically: the anchor call failed (stackshot: anchors not added [403] no).")
        self.assertEqual(len(self.posted()), 1)

    def test_a_retry_that_fails_too_falls_back_with_its_reason(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step")))
        self.ctl("refuse.items", "stackshot: anchors not added [400] Refused: nope")
        self.ctl("post.rc", "1")
        self.ctl("post.err", "stackshot: anchors not added [503] down")
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, OLDER.format(url=PR) + " Not anchored automatically: the anchor call failed (stackshot: anchors not added [503] down).")
        self.assertEqual(len(self.posted()), 2)

    def test_a_400_on_a_project_wide_add_is_not_retried(self):
        self.ctl("post.rc", "1")
        self.ctl("post.err", "stackshot: anchors not added [400] Refused: no project")
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, OLDER.format(url=PR) + " Not anchored automatically: the anchor call failed (stackshot: anchors not added [400] Refused: no project).")
        self.assertEqual(self.posted(), [{"project": PROJECT, "add": [PR], "sender": ME}])

    def test_a_400_with_a_waiting_item_sent_no_item_and_is_not_retried(self):
        # no item was named in the add (the only item waits), so a refusal has nothing to retry without
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step", until="2099-01-01", wait_kind="time", phase="waiting")))
        self.ctl("post.rc", "1")
        self.ctl("post.err", "stackshot: anchors not added [400] Refused: no project")
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, OLDER.format(url=PR) + " Not anchored automatically: the anchor call failed (stackshot: anchors not added [400] Refused: no project).")
        self.assertEqual(len(self.posted()), 1)
        self.assertNotIn("items", self.posted()[0])


class Markers(Base):
    """The marker file: a corrupt one never disables anchoring, and it is private."""

    def marker_file(self):
        return os.path.join(self.home, ".local", "state", "post-tool-note", "anchored.txt")

    def test_a_corrupt_marker_file_is_treated_as_empty_and_rewritten(self):
        self.write(self.marker_file(), "")
        with open(self.marker_file(), "wb") as f:
            f.write(b"\xff\xfe\x00garbage")
        self.assertIn(f"Anchored {PR} project-wide", self.run_hook(gh_event(PR)))
        with open(self.marker_file(), "rb") as f:
            raw = f.read()
        want = hashlib.sha1(f"sess-1|{PR}".encode()).hexdigest()[:20] + "\n"
        self.assertEqual(raw.decode("utf-8"), want)                                        # valid text now, the one marker
        self.assertIn("already anchored", self.run_hook(gh_event(PR)))
        self.assertEqual(len(self.posted()), 1)

    def test_the_marker_folder_and_file_are_private(self):
        self.run_hook(gh_event(PR))
        self.assertEqual(os.stat(os.path.dirname(self.marker_file())).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.marker_file()).st_mode & 0o777, 0o600)

    def test_a_rewritten_marker_file_is_private_too(self):
        os.makedirs(os.path.dirname(self.marker_file()))
        with open(self.marker_file(), "wb") as f:
            f.write(b"\xff\xfe\x00garbage")
        os.chmod(self.marker_file(), 0o644)
        self.run_hook(gh_event(PR))
        self.assertEqual(os.stat(self.marker_file()).st_mode & 0o777, 0o600)


class MarkerCap(unittest.TestCase):
    def setUp(self):
        cm = temp_home()
        self.home = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.m = load_module("ptn_markers")
        self.m.STATE = os.path.join(self.home, "state")
        self.m.MARKERS = os.path.join(self.m.STATE, "anchored.txt")

    def fill(self, n):
        os.makedirs(self.m.STATE)
        with open(self.m.MARKERS, "w", encoding="utf-8") as f:
            f.write("".join(f"{i:020x}\n" for i in range(n)))

    def lines(self):
        return S.read(self.m.MARKERS).split()

    def test_past_the_cap_the_newest_half_stays(self):
        self.fill(400)
        self.m.mark("f" * 20)
        lines = self.lines()
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[-1], "f" * 20)
        self.assertEqual(lines[0], f"{201:020x}")                                          # the 200 newest of the 401

    def test_up_to_the_cap_nothing_is_cut(self):
        self.fill(399)
        self.m.mark("f" * 20)
        self.assertEqual(len(self.lines()), 400)

    def test_lines_that_are_not_markers_are_dropped(self):
        self.fill(2)
        with open(self.m.MARKERS, "a", encoding="utf-8") as f:
            f.write("not a marker\n")
        self.m.mark("f" * 20)
        self.assertEqual(self.lines(), [f"{0:020x}", f"{1:020x}", "f" * 20])

    def test_an_unwritable_state_folder_is_silent(self):
        self.m.STATE = os.path.join(self.home, "file")
        self.m.MARKERS = os.path.join(self.m.STATE, "anchored.txt")
        with open(self.m.STATE, "w", encoding="utf-8") as f:
            f.write("a file, not a folder")
        self.m.mark("f" * 20)
        self.assertFalse(self.m.is_marked("f" * 20))


class Bounds(Base):
    """The limits of what is sent, and the time the attempt may take."""

    def test_a_long_session_name_is_cut_for_the_sender(self):
        name = "n" * 100
        self.write(self.roles, json.dumps({"sessions": {name: {"role": "back", "project": PROJECT}}, "ids": {}}))
        self.assertIn("Anchored", self.run_hook(gh_event(PR), name=name))
        self.assertEqual(self.posted()[0]["sender"], "n" * 80)

    def test_the_project_may_have_200_characters_not_201(self):
        for n, ok in ((200, True), (201, False)):
            with self.subTest(n=n):
                project = "projects/" + "p" * (n - 9)
                self.write(self.roles, json.dumps({"sessions": {ME: {"role": "back", "project": project}}, "ids": {}}))
                note = self.run_hook(gh_event(PR, session=f"s{n}"))
                if ok:
                    self.assertIn("Anchored", note)
                else:
                    self.assertEqual(note, OLDER.format(url=PR) + " Not anchored automatically: the session's roles entry names no usable project.")

    def test_a_reason_is_cut_at_140_characters(self):
        self.ctl("post.rc", "1")
        self.ctl("post.err", "x" * 300)
        note = self.run_hook(gh_event(PR))
        prefix = OLDER.format(url=PR) + " Not anchored automatically: "
        self.assertTrue(note.startswith(prefix) and note.endswith("."), note)
        self.assertEqual(len(note[len(prefix):-1]), 140)

    def test_the_body_goes_to_the_client_as_ascii_json(self):
        name = "widégets-builder"
        self.write(self.roles, json.dumps({"sessions": {name: {"role": "back", "project": PROJECT}}, "ids": {}}))
        self.assertIn("Anchored", self.run_hook(gh_event(PR), name=name))
        with open(self.out + ".posted", "rb") as f:
            raw = f.read()
        raw.decode("ascii")                                                                   # raises on any byte above 127
        self.assertIn(b"wid\\u00e9gets-builder", raw)
        self.assertEqual(self.posted()[0]["sender"], name)

    def test_the_default_timeout_of_a_call_is_3_5_seconds(self):
        self.ctl("sleep.get", "20")
        t = time.monotonic()
        note = self.run_hook(gh_event(PR), POST_TOOL_NOTE_CALL_TIMEOUT_S="")
        took = time.monotonic() - t
        self.assertEqual(note, OLDER.format(url=PR) + " Not anchored automatically: the read of your open items timed out.")
        self.assertTrue(3.3 <= took < 5.5, took)

    def test_the_whole_attempt_has_a_budget_of_7_5_seconds(self):
        """A read of 3 s and an add that never answers: the add is cut at what is left of the budget (the clamp), well before the
        alarm at 9 s, whatever the per-call timeout says."""
        self.ctl("sleep.get", "3")
        self.ctl("sleep.post", "30")
        t = time.monotonic()
        note = self.run_hook(gh_event(PR), POST_TOOL_NOTE_CALL_TIMEOUT_S="6")
        took = time.monotonic() - t
        self.assertEqual(note, OLDER.format(url=PR) + " Not anchored automatically: the anchor call timed out.")
        self.assertTrue(7.0 <= took < 8.8, took)


# A hook run in a child interpreter whose client call is replaced, to reach what a subprocess timeout cannot: an attempt that hangs.
RUNNER = '''
import importlib.util, json, os, sys, time
hooks, mode = sys.argv[1], sys.argv[2]
sys.path.insert(0, hooks)
spec = importlib.util.spec_from_file_location("ptn_run", os.path.join(hooks, "post-tool-note.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
ANSWER = json.dumps({"session": "x", "total": 0, "truncated": False, "unreadable": [], "items": []})
if mode == "boom":
    def boom(*a, **k):
        raise RuntimeError("boom")
    m.anchor = boom
elif mode == "hang":                                   # a client call that ignores its timeout
    m.call_client = lambda *a, **k: time.sleep(60)
elif mode == "real-client-hang":                      # the real client call (a stand-in that sleeps), ended by an alarm at 1 s
    m.ALARM_S = 1
elif mode == "hang-after-post":                        # the add is posted, then the marker write hangs
    m.call_client = lambda client, args, body, timeout: (0, ANSWER if args[0] == "get" else "", "")
    m.mark = lambda *a: time.sleep(60)
    m.ALARM_S = 1
sys.exit(m.main())
'''


class Backstops(Base):
    def child(self, mode, ev=None, timeout=25, env_extra=None):
        t = time.monotonic()
        r = subprocess.run([sys.executable, "-c", RUNNER, S.HOOKS, mode], input=json.dumps(ev or gh_event(PR)).encode(),
                           capture_output=True, env=self.env(**(env_extra or {})), timeout=timeout, cwd=self.home)
        return r, time.monotonic() - t

    def one_json_line(self, r):
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        lines = r.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]

    def test_an_error_inside_the_anchor_path_still_gives_the_older_note(self):
        r, _ = self.child("boom")
        self.assertEqual(self.one_json_line(r), OLDER.format(url=PR) + " Not anchored automatically: an unexpected error (RuntimeError).")

    def test_a_hung_attempt_is_ended_by_the_alarm_at_9_seconds(self):
        r, took = self.child("hang")
        self.assertEqual(self.one_json_line(r), OLDER.format(url=PR) + " Not anchored automatically: it timed out.")
        self.assertTrue(8.5 <= took < 11, took)

    def alive(self, pid):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False

    def test_the_alarm_ends_the_client_call_that_is_still_running(self):
        self.ctl("sleep.get", "25")
        r, took = self.child("real-client-hang", env_extra={"POST_TOOL_NOTE_CALL_TIMEOUT_S": "20"})
        self.assertEqual(self.one_json_line(r), OLDER.format(url=PR) + " Not anchored automatically: it timed out.")
        self.assertTrue(took < 8, took)
        with open(self.out + ".pid") as f:
            pid = int(f.read())

        def stray():                                   # only if the hook left it: the process is this test's own stand-in client
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    mine = self.checkout.encode() in f.read()
            except OSError:
                return
            if mine:
                os.kill(pid, 9)
        self.addCleanup(stray)
        for _ in range(30):
            if not self.alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(self.alive(pid), "the client call outlived the hook")

    def test_the_alarm_ends_what_the_client_call_started_too(self):
        """The whole process group goes, not only the client: a grandchild of the call that ignored the alarm would keep the hook's
        output pipe and the run open."""
        self.ctl("sleep.get", "25")
        self.ctl("grandchild", "1")
        r, took = self.child("real-client-hang", env_extra={"POST_TOOL_NOTE_CALL_TIMEOUT_S": "20"})
        self.assertEqual(self.one_json_line(r), OLDER.format(url=PR) + " Not anchored automatically: it timed out.")
        self.assertTrue(took < 8, took)
        with open(self.out + ".pid") as f:
            pid = int(f.read())
        with open(self.out + ".gpid") as f:
            gpid = int(f.read())

        def stray(p, marker):                          # only if the hook left it: a process of this test (its command line names its own files)
            try:
                with open(f"/proc/{p}/cmdline", "rb") as f:
                    mine = marker.encode() in f.read()
            except OSError:
                return
            if mine:
                os.kill(p, 9)
        self.addCleanup(stray, pid, self.checkout)
        self.addCleanup(stray, gpid, self.out + ".grandchild")
        for _ in range(30):
            if not self.alive(pid) and not self.alive(gpid):
                break
            time.sleep(0.1)
        self.assertFalse(self.alive(pid), "the client call outlived the hook")
        self.assertFalse(self.alive(gpid), "a process the client call started outlived the hook")

    def test_an_attempt_that_hangs_after_the_add_was_posted_says_it_was_anchored(self):
        r, _ = self.child("hang-after-post")
        note = self.one_json_line(r)
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (no open item names you as its mover); not tied to a board item. {NOTE_TAG}")
        self.assertNotIn("Not anchored", note)


class KillGroup(unittest.TestCase):
    """kill_group sends the signal to the call's process group, whether or not the client itself is still running."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module("ptn_kill")

    class Call:
        pid = 424242

        def __init__(self, returncode):
            self.returncode = returncode

        def poll(self):
            return self.returncode

    def test_a_running_call_has_its_group_killed(self):
        with mock.patch.object(self.m.os, "killpg") as killpg:
            self.m.kill_group(self.Call(None))
        killpg.assert_called_once_with(424242, self.m.signal.SIGKILL)

    def test_a_call_whose_leader_has_exited_still_has_its_group_killed(self):
        # a process it started can outlive it, and the group id is not reused while any member lives
        for code in (0, 1, -9):
            with self.subTest(code=code), mock.patch.object(self.m.os, "killpg") as killpg:
                self.m.kill_group(self.Call(code))
            killpg.assert_called_once_with(424242, self.m.signal.SIGKILL)

    def test_a_process_that_outlives_its_exited_leader_is_killed(self):
        """A client that prints, starts a process holding its pipes and exits: the call times out (the pipes stay open) with the leader
        already reaped, and the process it started must not be left running."""
        with tempfile.TemporaryDirectory() as d:
            lock = os.open(d, os.O_RDONLY)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                marker = os.path.join(d, "grandchild-marker")
                client = os.path.join(d, "client.py")
                with open(client, "w") as f:
                    f.write("import subprocess, sys\n"
                            f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)', {marker!r}])\n"
                            f"open({marker!r} + '.pid', 'w').write(str(g.pid))\n"
                            "print('printed', flush=True)\n")
                state = {}

                def stray():                               # only if the call left it: a process of this test (its command line names its own file)
                    pid = state.get("pid")
                    if not pid:
                        return
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as f:
                            mine = marker.encode() in f.read()
                    except OSError:
                        return
                    if mine:
                        os.kill(pid, 9)
                self.addCleanup(stray)
                t = time.monotonic()
                code, _out, _err = self.m.call_client(client, [], None, 1.5)
                self.assertIsNone(code)
                self.assertLess(time.monotonic() - t, 10)
                with open(marker + ".pid") as f:
                    state["pid"] = int(f.read())
                for _ in range(30):
                    try:
                        with open(f"/proc/{state['pid']}/stat") as f:
                            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                                break
                    except OSError:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("the process the call started outlived it")
            finally:
                os.close(lock)

    def test_a_group_that_is_gone_is_not_an_error(self):
        with mock.patch.object(self.m.os, "killpg", side_effect=ProcessLookupError):
            self.m.kill_group(self.Call(None))


class OutOfTime(Base):
    def test_no_anchor_call_is_made_with_less_than_a_third_of_a_second_left(self):
        m = load_module("ptn_time")
        calls = []

        def slow_read(client, args, body, timeout):
            calls.append(args[0])
            time.sleep(0.3)
            return 0, answer(), ""

        m.call_client = slow_read
        m.BUDGET_S = 0.5
        m.STATE = os.path.join(self.home, "state")
        m.MARKERS = os.path.join(m.STATE, "anchored.txt")
        with mock.patch.dict(os.environ, self.env(), clear=True):
            note, why = m.anchor(gh_event(PR), PR)
        self.assertEqual((note, why), ("", "out of time before the anchor call"))
        self.assertEqual(calls, ["get"])


if __name__ == "__main__":
    unittest.main()
