"""post-tool-note: after a session creates a pull request or an issue, the hook anchors it through stackshot's machine client and says
so in one line; whatever goes wrong, it falls back to the older note with the reason after it.

The hook runs as the CLI runs it (hook JSON on stdin). The session's name (BUILD_STOPS_NAME), the roles file, the state folder
(XDG_STATE_HOME) and a stand-in stackshot checkout whose fleet/client.py answers `get open-records` from a file and records every call
(arguments and stdin) are made under a temp home, locked while the test runs and removed after, pass or fail. The only processes started
are the hook and the stand-in client; nothing is killed, nothing touches the network, tmux or the real home directory. A real call to a
server is never made: the client is always the stand-in."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
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
NOT_FROM_CREATE = "the link did not come from the create call itself"
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
        for n, command in enumerate(["gh pr create --fill 2>&1", "gh pr create --fill 2>/dev/null", "gh pr create --fill 2>> err.log",
                                     "gh pr create --fill --title 'a -w b' < body.md"]):
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


class Segments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module("ptn_segments")

    def test_cuts_and_what_is_never_a_segment(self):
        cases = [("a && b; c | d\ne", ["a", "b", "c", "d", "e"]),
                 ('echo "a; b | c" && x', ['echo "a; b | c"', "x"]),
                 ("echo 'a && b'; y", ["echo 'a && b'", "y"]),
                 ("x # y; z\nw", ["x", "w"]),
                 ("echo a#b; z", ["echo a#b", "z"]),
                 ("cat <<EOF\nbody; | gh pr create\nEOF\nnext", ["cat <<EOF", "next"]),
                 ("cat <<-'EOF'\n\tbody\n\tEOF\nnext", ["cat <<-'EOF'", "next"]),
                 ("x $(a; b) y; z", ["x $(a; b) y", "z"]),
                 ('x "$(a; "b c" )" y; z', ['x "$(a; "b c" )" y', "z"]),
                 ("x `a; b` y", ["x `a; b` y"]),
                 ("a \\\n b; c", ["a \\\n b", "c"]),
                 ("a 2>&1 | b", ["a 2>&1", "b"]),
                 ("", []), (" ; ;\n", [])]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.m.real_segments(cmd), want)

    def test_unclosed_quotes_and_substitutions_do_not_hang_or_raise(self):
        for cmd in ("echo 'abc", 'echo "abc', "echo $(abc", "echo `abc", "cat <<EOF\nnever ends", "echo \\", "cat <<"):
            with self.subTest(cmd=cmd):
                self.assertIsInstance(self.m.real_segments(cmd), list)


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
                self.assertEqual([t for _s, t, _b, _o in segs], want)

    def test_what_is_not_modeled_is_none(self):
        for cmd in ("a || b", "a & b", "a &", "a |& b", "a ;; b", "(a)", "a)", "((1))", "$((1))", "$'a'", '$"a"', "$[1]", "${a:-b}", "${a",
                    "a <(b)", "a >(b)", "a <<", "a << )", "a <<$X", "echo 'abc", 'echo "abc', "echo $(abc", "echo `abc", "echo $(a <<EOF)",
                    "cat <<EOF\nnever ends", "echo $(a $'b')", 'echo "$((1))"', 'echo "${a b}"', "echo `a $'b'`"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.m.shape_of(cmd))

    def test_the_stdout_of_a_segment_is_told_from_its_stderr(self):
        for cmd, out in (("a > f", True), ("a >> f", True), ("a 1> f", True), ("a >&2", True), ("a &> f", True), ("a >| f", True), ("a3> f", True),
                         ("a 2> f", False), ("a 2>> f", False), ("a 2>&1", False), ("a < f", False), ("a <&3", False), ("a 'x > y'", False)):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.m.shape_of(cmd)[0][3], out)


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

    def test_an_attempt_that_hangs_after_the_add_was_posted_says_it_was_anchored(self):
        r, _ = self.child("hang-after-post")
        note = self.one_json_line(r)
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (no open item names you as its mover); not tied to a board item. {NOTE_TAG}")
        self.assertNotIn("Not anchored", note)


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
