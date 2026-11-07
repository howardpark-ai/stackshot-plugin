"""handoff-item-note: a front agent's message to its project's builder that names no item gets one note, never a block.

Each test runs the hook under a temp HOME (locked while it runs, removed after, pass or fail) with its own roles file, with no sala and
no stackshot checkout unless the test makes a stub. Nothing here starts a long-lived process or touches the real home directory."""
import json
import os
import unittest

import _support as S
from _support import context, decision, env_for, make_stub_stackshot, run_hook, temp_home

NOTE = "Hand-off with no item: file the item first and hand it over by id, with what done means."
PROJECT = "prj_testproj"
CFG = {"projects": {PROJECT: {"short": "widgets", "builder": "acme-builder"}}}
PLAIN = "Please add retries to the uploader and make it pass the suite."


class HandoffItemNote(unittest.TestCase):
    def world(self, home, ids=None):
        roles = os.path.join(home, "roles.json")
        sessions = {
            "acme-main": {"role": "front", "project": PROJECT},
            "acme-builder": {"role": "back", "project": PROJECT},
            "acme-helper": {"role": "back", "project": PROJECT},
            "acme-old": {"role": "back", "project": PROJECT, "retired": True},
            "acme-second": {"role": "front", "project": PROJECT},
            "other-main": {"role": "front", "project": "prj_other"},
            "other-builder": {"role": "back", "project": "prj_other"},
        }
        with open(roles, "w", encoding="utf-8") as f:
            json.dump({"sessions": sessions, "ids": ids or {}}, f)
        return roles

    def env(self, home, roles, me="acme-main", cfg=CFG, **extra):
        e = dict(BUILD_STOPS_ROLES=roles, BUILD_STOPS_NAME=me, **extra)
        if cfg is not None:
            e["STACKSHOT_PLUGIN_BUILD_STOPS"] = json.dumps(cfg)
        return env_for(home, **e)

    def run_it(self, ev, env):
        rc, out, err = run_hook("handoff-item-note.py", dict(ev, hook_event_name="PreToolUse", session_id="s1"), env)
        self.assertEqual(rc, 0, err)
        S.assert_no_owner(self, out + err, "the hook output")
        return out, err

    # the three routes
    def send(self, to, message):
        return {"tool_name": "SendMessage", "tool_input": {"to": to, "message": message}}

    def direct(self, to, text, **kw):
        return {"tool_name": "mcp__stackshot__message", "tool_input": {"to": to, "text": text, "sender": "acme-main", **kw}}

    def portal(self, *calls):
        code = "\n".join(f"await codemode.stackshot_message({c});" for c in calls)
        return {"tool_name": "mcp__portal__portal_codemode_execute", "tool_input": {"code": code}}

    @staticmethod
    def obj(to, text, extra=""):
        return "{to: %s, text: %s, sender: 'acme-main'%s}" % (json.dumps(to), json.dumps(text), ", " + extra if extra else "")

    def test_fires_once_on_every_route(self):
        with temp_home() as home:
            env = self.env(home, self.world(home))
            for ev in (self.send("acme-builder", PLAIN), self.direct("acme-builder", PLAIN),
                       self.portal(self.obj("acme-builder", PLAIN)),
                       self.portal(self.obj("acme-builder", PLAIN), self.obj("acme-builder", "And update the docs."))):
                out, err = self.run_it(ev, env)
                self.assertEqual(decision(out), "note", ev)               # a note, never a deny
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "PreToolUse")
                self.assertNotIn("permissionDecision", json.loads(out)["hookSpecificOutput"])
                self.assertEqual(context(out).count("Hand-off with no item"), 1, ev)   # once per call
                self.assertTrue(context(out).startswith(NOTE), context(out))
                self.assertNotIn("handoff-item-note", err)                 # the hook itself writes no line

    def test_works_with_a_stub_stackshot_checkout(self):
        with temp_home() as home:
            env = self.env(home, self.world(home), STACKSHOT_DIR=make_stub_stackshot(home))
            self.assertTrue(context(self.run_it(self.portal(self.obj("acme-builder", PLAIN)), env)[0]).startswith(NOTE))
            self.assertTrue(context(self.run_it(self.direct("acme-builder", PLAIN), env)[0]).startswith(NOTE))

    def test_silent_when_an_item_is_named(self):
        with temp_home() as home:
            env = self.env(home, self.world(home))
            for text in ("Build tracker#12: add retries.", "Please do tracker-tmp-x#3 next.", "See https://board.example/items/48 for it.",
                         "Item tracker#7 is yours."):
                for ev in (self.send("acme-builder", text), self.direct("acme-builder", text),
                           self.portal(self.obj("acme-builder", text))):
                    self.assertEqual(self.run_it(ev, env)[0], "", (text, ev))
            # the pattern is a setting: another board's form is read, and the default form then no longer counts
            env2 = self.env(home, self.world(home), STACKSHOT_PLUGIN_ITEM_REF_PATTERNS=json.dumps([r"\bT-\d+\b"]))
            self.assertEqual(self.run_it(self.send("acme-builder", "Do T-45 now."), env2)[0], "")
            self.assertEqual(decision(self.run_it(self.send("acme-builder", "Do tracker#45 now."), env2)[0]), "note")
            # an invalid pattern is skipped, and with none left the default is used
            env3 = self.env(home, self.world(home), STACKSHOT_PLUGIN_ITEM_REF_PATTERNS=json.dumps(["("]))
            self.assertEqual(self.run_it(self.send("acme-builder", "Do tracker#45 now."), env3)[0], "")

    def test_silent_for_fyi_and_for_text_it_cannot_read(self):
        with temp_home() as home:
            env = self.env(home, self.world(home))
            self.assertEqual(self.run_it(self.direct("acme-builder", PLAIN, fyi=True), env)[0], "")
            self.assertEqual(decision(self.run_it(self.direct("acme-builder", PLAIN, fyi=False), env)[0]), "note")
            self.assertEqual(self.run_it(self.portal(self.obj("acme-builder", PLAIN, "fyi: true")), env)[0], "")
            var = "{to: 'acme-builder', text: msg, sender: 'acme-main'}"       # a variable: cannot be read, so silent
            self.assertEqual(self.run_it(self.portal(var), env)[0], "")
            self.assertEqual(self.run_it(self.portal("{to, text}"), env)[0], "")

    def test_silent_for_a_background_sender(self):
        with temp_home() as home:
            roles = self.world(home)
            for me in ("acme-builder", "acme-helper", "other-builder"):
                env = self.env(home, roles, me=me)
                for ev in (self.send("acme-builder", PLAIN), self.direct("acme-helper", PLAIN),
                           self.portal(self.obj("acme-builder", PLAIN))):
                    self.assertEqual(self.run_it(ev, env)[0], "", (me, ev))
            unknown = self.env(home, roles, me="nobody")                       # a session the roles file does not know
            self.assertEqual(self.run_it(self.send("acme-builder", PLAIN), unknown)[0], "")

    def test_silent_for_a_recipient_outside_the_project_or_not_a_builder(self):
        with temp_home() as home:
            roles = self.world(home)
            env = self.env(home, roles)
            for to in ("other-builder", "other-main", "acme-second", "acme-old", "acme-main", "nobody", "projects/widgets", ""):
                for ev in (self.send(to, PLAIN), self.direct(to, PLAIN), self.portal(self.obj(to, PLAIN))):
                    self.assertEqual(self.run_it(ev, env)[0], "", (to, ev))
            # another background agent of the project: not the builder, so silent while the setting names the builder ...
            self.assertEqual(self.run_it(self.send("acme-helper", PLAIN), env)[0], "")
            # ... and any background agent of the project counts where the builder cannot be told apart
            for cfg in (None, {"projects": {PROJECT: {"short": "widgets"}}}):
                loose = self.env(home, roles, cfg=cfg)
                self.assertEqual(decision(self.run_it(self.send("acme-helper", PLAIN), loose)[0]), "note")
                self.assertEqual(decision(self.run_it(self.send("acme-builder", PLAIN), loose)[0]), "note")
                self.assertEqual(self.run_it(self.send("other-builder", PLAIN), loose)[0], "")

    def test_builder_by_sala_id_and_recipient_by_sala_id(self):
        with temp_home() as home:
            roles = self.world(home, ids={"acme-builder": "id-1", "acme-helper": "id-2"})
            cfg = {"projects": {PROJECT: {"short": "widgets", "builder": "its-old-name", "builder_id": "id-1"}}}
            env = self.env(home, roles, cfg=cfg)
            self.assertEqual(decision(self.run_it(self.send("acme-builder", PLAIN), env)[0]), "note")
            self.assertEqual(decision(self.run_it(self.send("id-1", PLAIN), env)[0]), "note")
            self.assertEqual(self.run_it(self.send("acme-helper", PLAIN), env)[0], "")
            self.assertEqual(self.run_it(self.send("id-2", PLAIN), env)[0], "")

    def test_silent_with_no_stackshot(self):
        with temp_home() as home:
            for ev in (self.send("acme-builder", PLAIN), self.direct("acme-builder", PLAIN), self.portal(self.obj("acme-builder", PLAIN))):
                env = env_for(home, BUILD_STOPS_NAME="acme-main")              # no roles file, no setting, no checkout, no sala
                self.assertEqual(self.run_it(ev, env), ("", ""), ev)
                env = env_for(home, BUILD_STOPS_NAME="acme-main", BUILD_STOPS_ROLES=os.path.join(home, "missing.json"))
                self.assertEqual(self.run_it(ev, env), ("", ""), ev)
            garbage = os.path.join(home, "roles.json")
            with open(garbage, "w", encoding="utf-8") as f:
                f.write("not json")
            env = env_for(home, BUILD_STOPS_NAME="acme-main", BUILD_STOPS_ROLES=garbage)
            self.assertEqual(self.run_it(self.send("acme-builder", PLAIN), env), ("", ""))
            rc, out, _ = run_hook("handoff-item-note.py", "not json", env)
            self.assertEqual((rc, out), (0, ""))

    def test_other_calls_pass_untouched(self):
        with temp_home() as home:
            env = self.env(home, self.world(home))
            for ev in ({"tool_name": "Bash", "tool_input": {"command": "echo to acme-builder"}},
                       {"tool_name": "mcp__github__create_issue", "tool_input": {"title": "x"}},
                       {"tool_name": "mcp__portal__portal_codemode_execute", "tool_input": {"code": "await codemode.gbrain_search({query: 'x'})"}},
                       {"tool_name": "mcp__portal__portal_codemode_execute", "tool_input": {"code": "await codemode.slack_message({to: 'acme-builder', text: 'hi'})"}}):
                self.assertEqual(self.run_it(ev, env)[0], "", ev)


if __name__ == "__main__":
    unittest.main()
