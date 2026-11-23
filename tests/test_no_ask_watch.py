"""no-ask-watch: a Stop hook that records a front agent's turn that nobody asked for (a peer note, a task notification) and that ended with
nothing for the owner ("Nothing here needs you."), by posting one `catch` record per turn to stackshot.

The hook runs as the CLI runs it (hook JSON on stdin). The pane's name (BUILD_STOPS_NAME), the roles file, the transcript, the state folder
(XDG_STATE_HOME) and a stand-in stackshot checkout whose client.py appends its arguments and stdin to a file are made under a temp home
(locked while the test runs, removed after, pass or fail). The only processes started are the hook itself and its detached flush; nothing is
killed, nothing touches the network, tmux or the real home directory. A real post is never made: the client is always the stand-in."""
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
from _support import env_for, temp_home

SCRIPT = os.path.join(S.HOOKS, "no-ask-watch.py")
PANE = "acme-front"
SID = "11111111-2222-3333-4444-555555555555"
NO_ASK = "Nothing here needs you. The fix is live."
PEER_TEXT = ("Another Claude session sent a message:\nfrom: stackshot (router)\n[stackshot message] from stackshot (router) · 2026-01-02 10:07\n"
             "[for owner] [stackshot] router: notice abc123 on projects/x is passed\nmore lines")
BODY_KEYS = {"hook", "id", "t", "pane", "kind", "sender", "outcome", "first"}

FAKE_CLIENT = '''import os, sys
out = os.environ["NAW_OUT"]
with open(out + ".args", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:4] != ["post", "catch", "-"]:        # the live client reads the body from stdin only when `-` is among the arguments
    sys.stderr.write("hook, id, t, pane and kind are required, as text\\n")
    sys.exit(1)
with open(out, "a") as f:
    f.write(sys.stdin.read() + "\\n=====\\n")
sys.exit(1 if os.path.exists(out + ".fail") else 0)
'''


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() if t is None else t))


def roles_doc(generated=None):
    return {"generated": (generated or iso())[:19] + "Z", "ids": {}, "sessions": {
        PANE: {"role": "front", "project": "projects/acme"},
        "acme-lane": {"role": "front", "project": "projects/acme"},
        "acme-chat": {"role": "front", "project": "projects/acme", "surface": "slack"},
        "acme-explicit": {"role": "front", "project": "projects/acme", "surface": "sala"},
        "acme-builder": {"role": "back", "project": "projects/acme", "keeper": True},
        "acme-steward": {"role": "keeper", "project": "projects/acme"},
    }}


# --- transcript entries, as Claude Code writes them -----------------------------------------------------------------------------
def user(kind, text, meta=False, **extra):
    e = {"type": "user", "isSidechain": False, "timestamp": iso(), "isMeta": meta, "message": {"role": "user", "content": text}}
    if kind:
        e["origin"] = {"kind": kind, **({"from": "unknown"} if kind == "peer" else {})}
    e.update(extra)
    return e


def peer(text=PEER_TEXT, **o):
    e = user("peer", text, meta=True)
    e["origin"].update(o)
    return e


def human(text="please look at this"):
    return user("human", text)


def queued(kind, text):
    return {"type": "attachment", "timestamp": iso(), "attachment": {"type": "queued_command", "prompt": text, "commandMode": "prompt",
                                                                       "origin": {"kind": kind}, "isMeta": True}}


def reply(text):
    return {"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_result():
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}


def end_of_turn():
    return {"type": "system", "subtype": "turn_duration", "durationMs": 1234, "timestamp": iso()}


def compaction():
    return {"type": "user", "isSidechain": False, "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
            "message": {"role": "user", "content": "This session is being continued from a previous conversation that ran out of context.\nSummary: x"}}


def sha1(t, pane, sender, kind):
    return hashlib.sha1(f"{t}|{pane}|{sender}|{kind}".encode()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        cm = temp_home()
        self.home = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.roles = os.path.join(self.home, "roles.json")
        self.write(self.roles, json.dumps(roles_doc()))
        self.out = os.path.join(self.home, "posted.txt")
        self.state = os.path.join(self.home, "xdg-state")
        self.dir = os.path.join(self.state, "no-ask-watch")
        self.tr = os.path.join(self.home, "transcript.jsonl")
        self.checkout = self.make_checkout()

    # -- files
    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def make_checkout(self, name="stackshot"):
        """A stand-in stackshot checkout whose fleet/client.py records its arguments and stdin ($NAW_OUT)."""
        d = os.path.join(self.home, name)
        os.makedirs(os.path.join(d, "stackshot", "fleet"))
        for init in (os.path.join(d, "stackshot", "__init__.py"), os.path.join(d, "stackshot", "fleet", "__init__.py")):
            self.write(init, "")
        self.write(os.path.join(d, "stackshot", "fleet", "client.py"), FAKE_CLIENT)
        return d

    def write_transcript(self, *entries, raw=None):
        self.write(self.tr, raw if raw is not None else "".join(json.dumps(e) + "\n" for e in entries))

    # -- running
    def env(self, name=PANE, sync=True, **extra):
        env = env_for(self.home, BUILD_STOPS_ROLES=self.roles, XDG_STATE_HOME=self.state, SALA_HEARTBEAT_STATE_DIR=os.path.join(self.home, "no-sala-store"),
                      BUILD_STOPS_PROJECTS=os.path.join(self.home, "no-projects.json"), STACKSHOT_DIR=self.checkout, NAW_OUT=self.out)
        if sync:
            env["NO_ASK_WATCH_SYNC"] = "1"          # the detached flush runs in the hook's own process, so the test can read the result at once
        if name is not None:
            env["BUILD_STOPS_NAME"] = name
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def hook_input(self, last, **ev):
        d = {"session_id": SID, "hook_event_name": "Stop", "stop_hook_active": False, "transcript_path": self.tr, **ev}
        if last is not None:
            d["last_assistant_message"] = last
        return d

    def run_hook(self, last=NO_ASK, name=PANE, sync=True, raw=None, extra=None, expect_stderr=False, **ev):
        r = subprocess.run([sys.executable, SCRIPT], input=raw if raw is not None else json.dumps(self.hook_input(last, **ev)).encode(),
                           capture_output=True, env=self.env(name, sync, **(extra or {})), timeout=30, cwd=self.home)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b"")                                        # never blocks, never prints
        if not expect_stderr:
            self.assertEqual(r.stderr, b"")
        return r

    def turn(self, *entries, last=NO_ASK, **kw):
        self.write_transcript(*entries, reply(last))
        return self.run_hook(last, **kw)

    def replay(self, *args, **extra):
        return subprocess.run([sys.executable, SCRIPT, "--replay", *args], capture_output=True, text=True, env=self.env(**extra), timeout=60, cwd=self.home)

    # -- reading what happened
    def posted(self):
        if not os.path.exists(self.out):
            return []
        return [json.loads(x) for x in S.read(self.out).split("=====") if x.strip()]

    def calls(self):
        p = self.out + ".args"
        return S.read(p).splitlines() if os.path.exists(p) else []

    def fail_posts(self, on=True):
        if on:
            self.write(self.out + ".fail", "")
        elif os.path.exists(self.out + ".fail"):
            os.unlink(self.out + ".fail")

    def log(self):
        p = os.path.join(self.dir, "firings.jsonl")
        return [json.loads(x) for x in S.read(p).splitlines()] if os.path.exists(p) else []

    def outcomes(self):
        return [x["outcome"] for x in self.log()]

    def st(self):
        return json.loads(S.read(os.path.join(self.dir, "state.json")))

    def pending(self):
        p = os.path.join(self.dir, "pending.jsonl")
        return [json.loads(x) for x in S.read(p).splitlines() if x.strip()] if os.path.exists(p) else []

    def load_module(self, name="naw_mod"):
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m


class Post(Base):
    def test_peer_note_ending_with_no_ask_posts_one_catch(self):
        self.turn(peer())
        self.assertEqual(self.calls(), ["post catch -"])                            # the client's route, the body on stdin
        [b] = self.posted()
        self.assertEqual(set(b), BODY_KEYS)
        self.assertEqual((b["hook"], b["pane"], b["kind"], b["sender"], b["outcome"]), ("no-ask-watch", PANE, "peer", "stackshot (router)", "sent"))
        self.assertEqual(b["first"], NO_ASK)                                      # the sign-off, not the trigger
        self.assertEqual(self.outcomes(), ["sent"])
        self.assertEqual(self.st()["keys"][f"{PANE}|stackshot (router)"]["folded"], 0)
        self.assertEqual(self.pending(), [])

    def test_the_client_is_called_with_a_dash_so_it_reads_the_body_from_stdin(self):
        client = os.path.join(self.checkout, "stackshot", "fleet", "client.py")
        for argv in (["post", "catch"], ["post", "catch", "--"], ["catch", "-"]):
            with self.subTest(argv=argv):
                r = subprocess.run([sys.executable, client, *argv], input=b"{}", capture_output=True, env=self.env(), timeout=30)
                self.assertEqual(r.returncode, 1)                                 # the stand-in refuses what the live client refuses
                self.assertEqual(self.posted(), [])

    def test_a_post_without_the_dash_fails_and_nothing_is_marked_sent(self):
        m = self.load_module("naw_mod_dash")
        real = subprocess.run
        calls = []

        def no_dash(argv, **kw):
            argv = [a for a in argv if a != "-"]
            calls.append(argv)
            return real(argv, **kw)

        body = m.body_of("2026-01-02T10:00:00Z", PANE, "peer", "stackshot (router)", "sent", "x")
        self.write(os.path.join(self.dir, "pending.jsonl"), json.dumps({"id": body["id"], "next_at": 0, "body": body}) + "\n")
        env = {k: v for k, v in self.env().items() if k in ("STACKSHOT_DIR", "NAW_OUT", "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        with mock.patch.dict(os.environ, env), mock.patch.object(m, "STATE", self.dir), mock.patch.object(m, "LOG_FILE", os.path.join(self.dir, "firings.jsonl")), \
                mock.patch.object(m.subprocess, "run", side_effect=no_dash):
            m.flush()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-", calls[0])
        self.assertEqual(self.posted(), [])
        [row] = self.pending()                                                  # still queued, with its back-off
        self.assertEqual(row["id"], body["id"])
        self.assertGreater(row["next_at"], time.time() + 200)
        self.assertEqual(self.outcomes(), ["send-failed"])

    def test_sender_drops_every_non_printable_character(self):
        m = self.load_module("naw_mod_sender")
        for name, want in (("acme\x1b[31m-lane", "acme[31m-lane"), ("ac\u200bme\u200d-la\ufeffne", "acme-lane"), ("\u202eacme\u202c-lane\u200e", "acme-lane"),
                           ("acme\u2028-\u2029lane", "acme - lane"), ("acme\x00\x07-lane\x7f", "acme-lane"), ("  acme \t\n lane ", "acme lane"),
                           ("\u200b\u200b", "peer"), ("\x1b", "peer"), ("Unknown", "peer"), ("acme\ud800lane", "acmelane")):
            with self.subTest(name=name):
                got = m.sender_of("peer", "", {"name": name})
                self.assertEqual(got, want)
                self.assertTrue(got.isprintable())
        got = m.sender_of("peer", 'x from-name="ac\u200bme\x1b-lane" y', {})
        self.assertEqual(got, "acme-lane")
        self.assertTrue(m.sender_of("peer", "", {"name": "a\u200b" * 100}).isprintable())

    def test_a_sender_with_hidden_characters_posts_a_printable_sender(self):
        self.turn(peer(name="acme\u200b-lane\x1b\u202e"))
        [b] = self.posted()
        self.assertEqual(b["sender"], "acme-lane")
        self.assertEqual(b["id"], sha1(b["t"], PANE, "acme-lane", "peer"))

    def test_a_reply_with_no_phrase_returns_before_the_transcript_is_read(self):
        m = self.load_module("naw_mod_early")
        reads = []
        with mock.patch.dict(os.environ, {"BUILD_STOPS_NAME": PANE, "BUILD_STOPS_ROLES": self.roles, "XDG_STATE_HOME": self.state}), \
                mock.patch.object(m, "STATE", self.dir), mock.patch.object(m, "LOG_FILE", os.path.join(self.dir, "firings.jsonl")), \
                mock.patch.object(m, "pane_is_front", return_value=(PANE, True)), mock.patch.object(m, "read_turn", side_effect=lambda p: reads.append(p)):
            m.run(self.hook_input("All done, the change is live."))
            self.assertEqual(reads, [])                                          # a phrase-free reply: no transcript read
            m.run(self.hook_input(NO_ASK))
            self.assertEqual(len(reads), 1)                                      # a reply with a phrase still reads it
            m.run(self.hook_input(None))
            self.assertEqual(len(reads), 2)                                      # no reply in the input: the transcript is the source
        self.assertEqual(self.posted(), [])

    def test_id_is_the_sha1_of_t_pane_sender_kind_and_t_is_the_logged_one(self):
        self.turn(peer())
        [b] = self.posted()
        [line] = self.log()
        self.assertEqual(b["t"], line["t"])
        self.assertRegex(b["t"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(b["id"], sha1(line["t"], PANE, "stackshot (router)", "peer"))
        self.assertEqual(self.load_module().catch_id("T", "p", "s", "k"), sha1("T", "p", "s", "k"))

    def test_log_line_keeps_its_format(self):
        self.turn(peer())
        [line] = self.log()
        self.assertEqual(set(line), {"t", "pane", "kind", "sender", "outcome"})
        self.assertEqual((line["pane"], line["kind"], line["sender"], line["outcome"]), (PANE, "peer", "stackshot (router)", "sent"))

    def test_state_folder_follows_xdg_state_home_and_nothing_lands_in_the_home(self):
        self.turn(peer())
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "firings.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "state", "no-ask-watch")))

    def test_task_notification_trigger_reports_with_its_kind_as_sender(self):
        self.turn(user("task-notification", "<task-notification><task-id>b1</task-id>done</task-notification>", meta=True))
        [b] = self.posted()
        self.assertEqual((b["kind"], b["sender"]), ("task-notification", "task-notification"))

    def test_cross_session_message_names_its_sender_from_the_origin(self):
        text = ('Another Claude session sent a message:\n<cross-session-message from="uds:/run/user/1000/cc-socks/815699.sock" from-name="acme-lane" '
                'from-mode="bypass">\nit is yours now: talk it through\n</cross-session-message>')
        self.turn(peer(text, name="acme-lane", body="it is yours now"))
        self.assertEqual(self.posted()[0]["sender"], "acme-lane")

    def test_sender_is_read_from_the_from_line_when_the_origin_has_no_name(self):
        self.turn(peer("Another Claude session sent a message:\nfrom: acme-other\n[stackshot message] from acme-other · t\nhello"))
        self.assertEqual(self.posted()[0]["sender"], "acme-other")

    def test_queued_peer_note_mid_turn_is_a_trigger(self):
        self.write_transcript(reply("working"), queued("peer", "from: acme-other\n[stackshot message] from acme-other · x\nbody line"), reply(NO_ASK))
        self.run_hook()
        [b] = self.posted()
        self.assertEqual(b["sender"], "acme-other")

    def test_first_is_the_sign_off_clipped_to_200_with_long_tokens_masked(self):
        secret = "ghp_" + "a1B2c3D4" * 5
        self.turn(peer(), last="Nothing here needs you. key " + secret + " " + "word " * 80)
        [b] = self.posted()
        self.assertNotIn(secret, json.dumps(b))
        self.assertTrue(b["first"].startswith("Nothing here needs you. key <token> word"))
        self.assertEqual(len(b["first"]), 200)
        self.turn(peer(), last="Nothing here needs you. " + "x " * 250, name="acme-lane")
        self.assertEqual(len(self.posted()[1]["first"]), 200)

    def test_every_default_phrase_matches_case_insensitively(self):
        for last in ("Nothing here needs you.", "NOTHING NEW FOR YOU here.", "There\u2019s nothing you need to do. It is live.",
                     "There's nothing you need to do.", "Nothing needs you right now.", "Nothing new for you. The fix is live."):
            with self.subTest(last=last):
                n = len(self.posted())
                self.turn(peer(), last=last, name="acme-lane" if n % 2 else PANE)
                self.assertEqual(len(self.posted()), n + 1)

    def test_default_phrases_are_the_four(self):
        m = self.load_module()
        self.assertEqual({p.lower() for p in m.DEFAULT_PHRASES},
                         {"nothing here needs you", "nothing new for you", "there's nothing you need to do", "nothing needs you"})

    def test_no_names_of_a_deployment_in_the_hook(self):
        text = S.read(SCRIPT)
        self.assertNotIn("Asia/Seoul", text)
        self.assertNotIn("TARGET", text)


class Phrases(Base):
    def test_setting_replaces_the_phrases_from_the_environment(self):
        self.turn(peer(), last="All quiet on this one.", extra={"STACKSHOT_PLUGIN_NO_ASK_PHRASES": "all quiet, nothing to add"})
        self.assertEqual(len(self.posted()), 1)
        self.turn(peer(), last="Nothing to add, sorry.", extra={"STACKSHOT_PLUGIN_NO_ASK_PHRASES": "all quiet, nothing to add"}, name="acme-lane")
        self.assertEqual(len(self.posted()), 2)                                    # a comma separates two phrases
        self.turn(peer(), last=NO_ASK, extra={"STACKSHOT_PLUGIN_NO_ASK_PHRASES": "all quiet, nothing to add"}, name="acme-explicit")
        self.assertEqual(len(self.posted()), 2)                                    # a default phrase no longer matches

    def test_setting_from_plugin_json_as_a_list_with_a_comma_in_a_phrase(self):
        self.write(os.path.join(self.home, ".config", "stackshot", "plugin.json"), json.dumps({"no_ask_phrases": ["Nothing, really, to do"]}))
        self.turn(peer(), last="Nothing, really, to do here.")
        self.assertEqual(len(self.posted()), 1)
        self.turn(peer(), last=NO_ASK, name="acme-lane")
        self.assertEqual(len(self.posted()), 1)

    def test_an_empty_list_turns_the_hook_off_and_an_empty_phrase_matches_nothing(self):
        self.write(os.path.join(self.home, ".config", "stackshot", "plugin.json"), json.dumps({"no_ask_phrases": []}))
        self.turn(peer())
        self.assertEqual((self.posted(), self.log()), ([], []))
        self.write(os.path.join(self.home, ".config", "stackshot", "plugin.json"), json.dumps({"no_ask_phrases": ["", "  "]}))
        self.turn(peer(), last="anything at all")
        self.assertEqual((self.posted(), self.log()), ([], []))

    def test_json_list_in_the_environment(self):
        self.turn(peer(), last="Nothing, really, to do here.", extra={"STACKSHOT_PLUGIN_NO_ASK_PHRASES": json.dumps(["Nothing, really, to do"])})
        self.assertEqual(len(self.posted()), 1)


class Fold(Base):
    def test_a_repeat_within_the_hour_is_posted_too_as_folded(self):
        self.turn(peer())
        time.sleep(1.1)                                                            # another second, so the ids differ
        self.turn(peer())
        time.sleep(1.1)
        self.turn(peer())
        bodies = self.posted()
        self.assertEqual([b["outcome"] for b in bodies], ["sent", "folded", "folded"])
        self.assertEqual(len({b["id"] for b in bodies}), 3)
        self.assertEqual(self.outcomes(), ["sent", "folded", "folded"])
        self.assertEqual(self.st()["keys"][f"{PANE}|stackshot (router)"]["folded"], 2)

    def test_after_the_hour_the_next_catch_is_sent_again(self):
        self.turn(peer())
        st = self.st()
        st["keys"][f"{PANE}|stackshot (router)"]["last_sent"] -= 3700
        self.write(os.path.join(self.dir, "state.json"), json.dumps(st))
        self.turn(peer())
        self.assertEqual(self.outcomes(), ["sent", "sent"])
        self.assertEqual(self.st()["keys"][f"{PANE}|stackshot (router)"]["folded"], 0)

    def test_key_is_pane_plus_sender(self):
        self.turn(peer())
        self.turn(peer("Another Claude session sent a message:\nfrom: acme-other\n[stackshot message] from acme-other · t\nhello"))
        self.assertEqual(self.outcomes(), ["sent", "sent"])                        # same pane, another sender
        self.turn(peer(), name="acme-lane")
        self.assertEqual(self.outcomes(), ["sent", "sent", "sent"])                # same sender, another pane


class Passes(Base):
    def test_pass_when_the_pane_is_not_front(self):
        self.turn(peer(), name="acme-builder")
        self.turn(peer(), name="acme-steward")
        self.turn(peer(), name="a-pane-roles-does-not-know")
        self.assertEqual(self.posted(), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state.json")))

    def test_pass_when_the_front_agent_is_on_another_surface(self):
        self.turn(peer(), name="acme-chat")
        self.assertEqual(self.posted(), [])
        self.turn(peer(), name="acme-explicit")                                    # surface written out as sala: still watched
        self.assertEqual(len(self.posted()), 1)

    def test_pass_when_the_pane_is_front_only_by_its_name_suffix(self):
        self.turn(peer(), name="some-new-main")                                    # only the roles file says front
        self.assertEqual(self.posted(), [])

    def test_pass_when_roles_file_is_stale_or_unreadable(self):
        self.write(self.roles, json.dumps(roles_doc(iso(time.time() - 3 * 3600))))
        self.turn(peer())
        self.write(self.roles, "{not json")
        self.turn(peer())
        self.assertEqual(self.posted(), [])

    def test_pass_when_the_owner_started_the_turn(self):
        self.turn(human())
        self.assertEqual(self.posted(), [])
        self.assertIn("pass-human", self.outcomes())

    def test_pass_when_a_human_message_arrives_mid_turn(self):
        self.write_transcript(peer(), reply("on it"), tool_result(), queued("human", "wait, also check this"), reply(NO_ASK))
        self.run_hook()
        self.assertEqual(self.posted(), [])

    def test_pass_when_a_human_entry_follows_the_peer_one_as_a_user_entry(self):
        self.turn(peer(), human("and one more thing"))
        self.assertEqual(self.posted(), [])

    def test_missing_or_unknown_origin_counts_as_the_owner(self):
        self.turn(user(None, "typed by someone"))
        self.turn(user("some-future-kind", "x", meta=True))
        self.assertEqual(self.posted(), [])

    def test_compaction_continuation_is_not_a_trigger_and_not_the_owner(self):
        self.turn(compaction())
        self.assertEqual(self.posted(), [])
        self.assertIn("pass-no-trigger", self.outcomes())
        self.turn(peer(), compaction())
        self.assertEqual(len(self.posted()), 1)                                    # the earlier trigger of the turn stands

    def test_tool_results_and_sidechain_entries_are_not_the_owner(self):
        side = human("a subagent's prompt")
        side["isSidechain"] = True
        self.turn(peer(), tool_result(), side)
        self.assertEqual(len(self.posted()), 1)

    def test_plugin_entries_do_not_start_a_turn(self):
        self.turn(user("plugin", "<command-name>/exit</command-name>"))
        self.assertEqual(self.posted(), [])

    def test_only_the_current_turn_counts(self):
        self.write_transcript(human("an old question"), reply("answer"), end_of_turn(), peer(), reply(NO_ASK))
        self.run_hook()
        self.assertEqual(len(self.posted()), 1)                                    # the owner's message belongs to the turn before
        self.write_transcript(peer(), reply("a"), end_of_turn(), human("now this"), reply(NO_ASK))
        self.run_hook()
        self.assertEqual(len(self.posted()), 1)                                    # and the peer note to the turn before this one

    def test_turn_duration_already_written_for_the_current_turn(self):
        self.write_transcript(end_of_turn(), peer(), reply(NO_ASK), end_of_turn())
        self.run_hook()
        self.assertEqual(len(self.posted()), 1)

    def test_pass_when_the_turn_start_is_beyond_the_tail(self):
        pad = (json.dumps(tool_result()) + "\n") * 40000                           # about 4.4 MB with no turn boundary in the window
        self.write_transcript(raw=json.dumps(human()) + "\n" + pad + json.dumps(queued("peer", "from: a\nx")) + "\n" + json.dumps(reply(NO_ASK)) + "\n")
        self.assertGreater(os.path.getsize(self.tr), 4 * 1024 * 1024)
        self.run_hook()
        self.assertEqual(self.posted(), [])

    def test_phrase_inside_the_first_400_characters_matches_not_only_at_the_start(self):
        self.turn(peer(), last="The fix is live. Nothing here needs you.")
        self.assertEqual(len(self.posted()), 1)

    def test_pass_when_the_phrase_is_absent_or_past_the_start(self):
        self.turn(peer(), last="Done. " + "x" * 400 + " Nothing here needs you.")
        self.turn(peer(), last="Merged the pull request.")
        self.assertEqual(self.posted(), [])

    def test_pass_when_the_reply_asks_something(self):
        self.turn(peer(), last="Nothing here needs you, unless you want the sweep run now?")
        self.turn(peer(), last="Nothing here needs you. \uff1f")
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.outcomes(), ["pass-question"] * 2)

    def test_pass_on_subagent_and_second_stop(self):
        self.write_transcript(peer(), reply(NO_ASK))
        self.run_hook(NO_ASK, agent_id="agent-1")
        self.run_hook(NO_ASK, stop_hook_active=True)
        self.assertEqual(self.posted(), [])

    def test_pass_silently_when_input_or_transcript_is_unreadable(self):
        for how in ("missing", "garbage", "empty", "no-json-input", "no-name"):
            with self.subTest(how=how):
                if how == "missing":
                    self.run_hook(transcript_path=os.path.join(self.home, "none.jsonl"))
                elif how == "garbage":
                    self.write_transcript(raw="\x00\x01 not json\n{broken\n")
                    self.run_hook()
                elif how == "empty":
                    self.write_transcript(raw="")
                    self.run_hook()
                elif how == "no-json-input":
                    self.run_hook(raw=b"not json at all")
                else:
                    self.write_transcript(peer(), reply(NO_ASK))
                    self.run_hook(name=None, extra={"BUILD_STOPS_NAME": ""})
                self.assertEqual(self.posted(), [])

    def test_missing_last_message_falls_back_to_the_transcript(self):
        self.write_transcript(peer(), reply("thinking"), reply(NO_ASK))
        self.run_hook(None)
        [b] = self.posted()
        self.assertEqual(b["first"], NO_ASK)


class State(Base):
    def test_state_is_written_atomically_and_leaves_no_temp_files(self):
        self.turn(peer())
        self.assertEqual(sorted(p for p in os.listdir(self.dir) if p.startswith("state.json")), ["state.json"])
        self.assertEqual([p for p in os.listdir(self.dir) if p.endswith(".tmp")], [])
        json.loads(S.read(os.path.join(self.dir, "state.json")))

    def test_failed_replace_keeps_the_old_state_and_removes_the_temp_file(self):
        self.turn(peer())
        before = S.read(os.path.join(self.dir, "state.json"))
        m = self.load_module("naw_mod2")
        with mock.patch.object(m, "STATE", self.dir), mock.patch.object(m.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                m.save_state({"keys": {"x|y": {"last_sent": 1, "folded": 0}}})
        self.assertEqual(S.read(os.path.join(self.dir, "state.json")), before)
        self.assertEqual([p for p in os.listdir(self.dir) if ".tmp" in p], [])

    def test_corrupt_state_is_treated_as_empty(self):
        self.write(os.path.join(self.dir, "state.json"), "{corrupt")
        self.turn(peer())
        self.assertEqual(len(self.posted()), 1)
        self.assertTrue(self.st()["keys"])

    def test_old_keys_are_pruned(self):
        self.write(os.path.join(self.dir, "state.json"), json.dumps({"keys": {"old|pane": {"last_sent": time.time() - 3 * 86400, "folded": 0}}}))
        self.turn(peer())
        self.assertNotIn("old|pane", self.st()["keys"])

    def test_concurrent_runs_on_one_key_record_one_sent_and_the_rest_folded(self):
        self.write_transcript(peer(), reply(NO_ASK))
        inp = json.dumps(self.hook_input(NO_ASK)).encode()
        procs = [subprocess.Popen([sys.executable, SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(),
                                  cwd=self.home) for _ in range(5)]
        for p in procs:
            p.communicate(inp, timeout=30)
            self.assertEqual(p.returncode, 0)
        self.assertEqual(self.st()["keys"][f"{PANE}|stackshot (router)"]["folded"], 4)
        self.assertEqual(sorted(self.outcomes()), ["folded"] * 4 + ["sent"])
        self.write_transcript(human(), reply("ok"))
        self.run_hook("ok", name="acme-lane")                                       # any front pane's next firing posts what a busy flusher left
        self.assertEqual(self.pending(), [])
        self.assertEqual([b["outcome"] for b in self.posted()].count("sent"), 1)    # the five share one second, so their ids may coincide


class Retry(Base):
    def test_failed_post_is_kept_and_retried_at_the_next_firing(self):
        self.fail_posts()
        self.turn(peer())
        [row] = self.pending()                                                      # kept, not dropped
        self.assertEqual(set(row), {"id", "next_at", "body"})
        self.assertEqual(row["id"], row["body"]["id"])
        self.assertIn("send-failed", self.outcomes())
        first = self.posted()[0]
        self.fail_posts(False)
        row["next_at"] = 0                                                          # its back-off is over
        self.write(os.path.join(self.dir, "pending.jsonl"), json.dumps(row) + "\n")
        self.turn(human("a turn that posts nothing"), name="acme-lane", last="ok")  # any front pane's next firing retries
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.posted(), [first, first])                             # the failed attempt and the retry carry the same record
        self.turn(human("again"), last="ok")
        self.assertEqual(len(self.posted()), 2)                                     # delivered once after the failure, never twice

    def test_a_pending_post_is_not_retried_before_its_back_off(self):
        self.fail_posts()
        self.turn(peer())
        n = len(self.calls())
        self.fail_posts(False)
        self.turn(human("x"), last="ok")
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(len(self.pending()), 1)
        self.assertGreater(self.pending()[0]["next_at"], time.time() + 200)

    def test_a_row_of_the_message_based_hook_is_dropped_and_the_log_replays_it(self):
        self.write(os.path.join(self.dir, "pending.jsonl"), json.dumps({"id": "1-2", "pane": PANE, "kind": "peer", "sender": "s", "next_at": 0, "text": "old"}) + "\n")
        self.turn(human("x"), last="ok")
        self.assertEqual((self.pending(), self.posted()), ([], []))

    def test_without_sync_the_post_is_detached_and_arrives(self):
        self.write_transcript(peer(), reply(NO_ASK))
        t0 = time.time()
        self.run_hook(NO_ASK, sync=False)
        self.assertLess(time.time() - t0, 10)
        for _ in range(100):
            if self.posted() and self.pending() == []:
                break
            time.sleep(0.1)
        self.assertEqual(len(self.posted()), 1)
        self.assertEqual(self.pending(), [])


class NoCheckout(Base):
    def test_without_a_checkout_the_log_is_kept_and_one_line_says_so(self):
        empty = os.path.join(self.home, "no-stackshot-here")
        os.makedirs(empty)
        r = self.turn(peer(), extra={"STACKSHOT_DIR": empty}, expect_stderr=True)
        err = r.stderr.decode().strip()
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("no stackshot checkout", err)
        self.assertEqual(self.outcomes(), ["sent"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "pending.jsonl")))   # nothing queued for a client that is not there
        self.assertEqual(self.posted(), [])

    def test_the_log_posts_later_when_a_checkout_appears(self):
        empty = os.path.join(self.home, "no-stackshot-here")
        os.makedirs(empty)
        self.turn(peer(), extra={"STACKSHOT_DIR": empty}, expect_stderr=True)
        r = self.replay()
        self.assertEqual(r.returncode, 0, r.stderr)
        [b] = self.posted()
        self.assertEqual((b["pane"], b["outcome"], b["first"]), (PANE, "sent", ""))

    def test_replay_without_a_checkout_says_so_and_exits_1(self):
        empty = os.path.join(self.home, "no-stackshot-here")
        os.makedirs(empty)
        r = self.replay(STACKSHOT_DIR=empty)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no stackshot checkout", r.stderr)
        self.assertEqual(self.posted(), [])


class Replay(Base):
    def line(self, t, outcome="sent", pane=PANE, kind="peer", sender="stackshot (router)"):
        return json.dumps({"t": t, "pane": pane, "kind": kind, "sender": sender, "outcome": outcome})

    def log_file(self, *lines, name="old-firings.jsonl"):
        p = os.path.join(self.home, name)
        self.write(p, "\n".join(lines) + "\n")
        return p

    def test_replay_posts_the_sent_and_folded_lines_of_the_file(self):
        p = self.log_file(self.line("2026-01-02T10:00:00Z"), self.line("2026-01-02T10:10:00Z", "folded"), self.line("2026-01-02T10:20:00Z", "send-failed"),
                          self.line("2026-01-02T10:30:00Z", "pass-human", kind="", sender=""), "not json", self.line("2026-01-02T10:40:00Z", "folded", pane="acme-lane"))
        r = self.replay(p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("3 posted", r.stdout)
        bodies = self.posted()
        self.assertEqual([(b["t"], b["outcome"], b["pane"]) for b in bodies],
                         [("2026-01-02T10:00:00Z", "sent", PANE), ("2026-01-02T10:10:00Z", "folded", PANE), ("2026-01-02T10:40:00Z", "folded", "acme-lane")])
        for b in bodies:
            self.assertEqual(set(b), BODY_KEYS)
            self.assertEqual((b["hook"], b["first"], b["kind"], b["sender"]), ("no-ask-watch", "", "peer", "stackshot (router)"))
            self.assertEqual(b["id"], sha1(b["t"], b["pane"], b["sender"], b["kind"]))
        self.assertEqual(self.calls(), ["post catch -"] * 3)

    def test_default_file_is_the_firings_log(self):
        self.turn(peer())
        live = self.posted()
        r = self.replay()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.posted()), 2)
        self.assertEqual(self.posted()[1]["id"], live[0]["id"])                      # the same id as the live post: the server dedupes on it
        self.assertEqual(self.posted()[1]["t"], live[0]["t"])

    def test_a_replay_twice_posts_the_same_ids(self):
        p = self.log_file(self.line("2026-01-02T10:00:00Z"), self.line("2026-01-02T11:00:00Z"))
        self.replay(p)
        self.replay(p)
        ids = [b["id"] for b in self.posted()]
        self.assertEqual(ids[:2], ids[2:])
        self.assertEqual(len(set(ids)), 2)

    def test_a_line_within_a_few_seconds_of_a_duplicate_in_the_same_pane_is_skipped(self):
        p = self.log_file(self.line("2026-01-02T10:00:00Z"), self.line("2026-01-02T10:00:03Z", "folded"), self.line("2026-01-02T10:00:09Z"),
                          self.line("2026-01-02T10:00:10Z", pane="acme-lane"), self.line("2026-01-02T10:00:11Z", sender="acme-other"))
        r = self.replay(p)
        self.assertIn("1 skipped as duplicates", r.stdout)
        self.assertEqual([(b["t"], b["pane"], b["sender"]) for b in self.posted()],
                         [("2026-01-02T10:00:00Z", PANE, "stackshot (router)"), ("2026-01-02T10:00:09Z", PANE, "stackshot (router)"),
                          ("2026-01-02T10:00:10Z", "acme-lane", "stackshot (router)"), ("2026-01-02T10:00:11Z", PANE, "acme-other")])

    def test_failed_posts_exit_1_and_a_run_of_failures_stops_the_replay(self):
        self.fail_posts()
        p = self.log_file(*[self.line(f"2026-01-02T10:{m:02d}:00Z") for m in range(10)])
        r = self.replay(p)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.calls()), 3)
        self.assertIn("stopped after repeated failures", r.stdout)

    def test_a_missing_file_exits_1(self):
        r = self.replay(os.path.join(self.home, "none.jsonl"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot read", r.stderr)
        self.assertEqual(self.posted(), [])


class Registration(unittest.TestCase):
    def test_stop_entry_in_hooks_json(self):
        hooks = json.loads(S.read(os.path.join(S.ROOT, "hooks", "hooks.json")))["hooks"]["Stop"]
        found = [(g["matcher"], h) for g in hooks for h in g["hooks"] if "no-ask-watch.py" in h["command"]]
        self.assertEqual(len(found), 1)
        matcher, h = found[0]
        self.assertEqual((matcher, h["type"], h["timeout"]), ("*", "command", 10))
        self.assertEqual(h["command"], 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/no-ask-watch.py"')
        commands = " ".join(h["command"] for g in hooks for h in g["hooks"])
        self.assertIn("claude-project-page.sh", commands)                           # the existing Stop entries stay
        self.assertIn("gbrain-writeback-hook.sh", commands)

    def test_manifests_carry_the_same_version(self):
        plugin = json.loads(S.read(os.path.join(S.ROOT, ".claude-plugin", "plugin.json")))
        market = json.loads(S.read(os.path.join(S.ROOT, ".claude-plugin", "marketplace.json")))
        self.assertEqual(plugin["version"], market["plugins"][0]["version"])


if __name__ == "__main__":
    unittest.main()
