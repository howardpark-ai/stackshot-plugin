"""post-tool-note: after a session creates a pull request or an issue, the hook anchors it through stackshot's machine client and says
so in one line; whatever goes wrong, it falls back to the older note with the reason after it.

The hook runs as the CLI runs it (hook JSON on stdin). The session's name (BUILD_STOPS_NAME), the roles file, the state folder
(XDG_STATE_HOME) and a stand-in stackshot checkout whose fleet/client.py answers `get open-records` from a file and records every call
(arguments and stdin) are made under a temp home, locked while the test runs and removed after, pass or fail. The only processes started
are the hook and the stand-in client; nothing is killed, nothing touches the network, tmux or the real home directory. A real call to a
server is never made: the client is always the stand-in."""
import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest

import _support as S
from _support import context, env_for, temp_home

SCRIPT = os.path.join(S.HOOKS, "post-tool-note.py")
ME = "widgets-builder"
PROJECT = "projects/example"
PR = "https://github.com/acme/widgets/pull/7"
ISSUE = "https://github.com/acme/widgets/issues/12"
NOTE_TAG = "(post-tool-note.py)"
OLDER = "Created {url}. Anchor it now: stackshot anchors(project=..., add=[{url}]) " + NOTE_TAG + "."

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
        time.sleep(float(ctl("sleep.get")))
    if ctl("get.rc") not in (None, "0"):
        print(ctl("get.out") or "[401] no token on this machine")
        sys.exit(int(ctl("get.rc")))
    print(ctl("get.out") if ctl("get.out") is not None else '{"session": "x", "total": 0, "truncated": false, "unreadable": [], "items": []}')
    sys.exit(0)
if args[:3] == ["post", "anchor-add", "-"]:
    with open(out + ".posted", "a") as f:
        f.write(sys.stdin.read() + "\\n=====\\n")
    if ctl("sleep.post"):
        time.sleep(float(ctl("sleep.post")))
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
                                                       "metadata": {"mover": mover, "next": next_text, **meta}}, "anchors": []}


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
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (no open item names you as its mover); nothing more is needed. {NOTE_TAG}")
        self.assertEqual(self.posted(), [{"project": PROJECT, "add": [PR], "sender": ME}])

    def test_an_empty_list_is_zero_items_too(self):
        note = self.run_hook(gh_event(PR))
        self.assertIn("project-wide", note)
        self.assertIn("no open item names you", note)
        self.assertNotIn("items", self.posted()[0])

    def test_several_items_make_a_project_wide_anchor(self):
        self.ctl("get.out", answer(record("a1", "item-1", ME, f"{ME}: one"), record("a2", "item-2", ME, f"{ME}: two")))
        note = self.run_hook(gh_event(PR))
        self.assertEqual(note, f"Anchored {PR} project-wide on {PROJECT} (2 open items name you as their mover); nothing more is needed. {NOTE_TAG}")
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

    def test_a_url_with_trailing_text_is_anchored_clean(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step")))
        note = self.run_hook(gh_event(PR, trailing=" (draft) see also https://example.com/x?y=1"))
        self.assertIn(f"Anchored {PR} to item-7", note)
        self.assertEqual(self.posted()[0]["add"], [PR])
        note = self.run_hook(gh_event(PR + "/files"))             # the pull request's own page under it: still the pull request
        self.assertIn("already anchored", note)

    def test_the_same_url_is_anchored_once_per_session(self):
        self.ctl("get.out", answer(record("a7", "item-7", ME, f"{ME}: step")))
        self.assertIn("Anchored", self.run_hook(gh_event(PR)))
        again = self.run_hook(gh_event(PR))
        self.assertEqual(again, f"{PR} was already anchored in this session; nothing more is needed. {NOTE_TAG}")
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


if __name__ == "__main__":
    unittest.main()
