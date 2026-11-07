"""A neutral install: every hook runs with sample JSON on stdin under a temp HOME, with no sala, with and without a stub stackshot
checkout. Each hook must give the right allow / deny answer, never fail, and never print a word of the author's own setup."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

import _support as S
from _support import context, decision, env_for, make_git_stub, make_stub_stackshot, reason, run_hook, temp_home


class Neutral(unittest.TestCase):
    """Base: every hook call is also checked for owner strings in its output."""

    def hook(self, name, payload, env, args=()):
        rc, out, err = run_hook(name, payload, env, args)
        self.assertEqual(rc, 0, f"{name} exited {rc}: {err}")
        S.assert_no_owner(self, out + err, f"{name} output")
        return out, err


class SubagentModelGate(Neutral):
    def test_gate(self):
        with temp_home() as home:
            env = env_for(home)
            call = lambda **kw: self.hook("subagent-model-gate.py", {"tool_name": "Agent", "tool_input": kw}, env)[0]  # noqa: E731
            self.assertEqual(decision(call(subagent_type="stackshot:deep-engineer", prompt="fix it")), "deny")
            self.assertEqual(decision(call(subagent_type="deep-engineer", prompt="fix it")), "deny")
            self.assertEqual(decision(call(subagent_type="stackshot:deep-engineer", prompt="- **Escalation:** executor failed twice")), "allow")
            self.assertEqual(decision(call(subagent_type="stackshot:executor", prompt="x", model="opus")), "deny")
            self.assertEqual(decision(call(subagent_type="general-purpose", prompt="x")), "note")
            self.assertEqual(decision(call(subagent_type="general-purpose", prompt="x", model="sonnet")), "allow")
            self.assertEqual(decision(call(subagent_type="stackshot:executor", prompt="x")), "allow")
            self.assertIn("stackshot:executor", reason(call(subagent_type="deep-engineer", prompt="x")))
            # a namespace added by setting is accepted too; an unknown one is an ordinary type
            env2 = env_for(home, STACKSHOT_PLUGIN_AGENT_NAMESPACES="stackshot,mine")
            out = self.hook("subagent-model-gate.py", {"tool_name": "Task", "tool_input": {"subagent_type": "mine:deep-engineer", "prompt": "x"}}, env2)[0]
            self.assertEqual(decision(out), "deny")
            self.assertEqual(decision(call(subagent_type="other:deep-engineer", prompt="x")), "allow")
            # garbage in: silent allow
            self.assertEqual(self.hook("subagent-model-gate.py", "not json", env)[0], "")
            self.assertEqual(self.hook("subagent-model-gate.py", {"tool_name": "Bash", "tool_input": {}}, env)[0], "")


class WaitLoopGuard(Neutral):
    def test_guard(self):
        with temp_home() as home:
            env = env_for(home)
            bash = lambda c: self.hook("wait-loop-guard.py", {"tool_name": "Bash", "tool_input": {"command": c}}, env)[0]  # noqa: E731
            self.assertEqual(decision(bash('until ! pgrep -f "bash tests/run.sh" >/dev/null; do sleep 5; done')), "deny")
            self.assertEqual(decision(bash("while true; do sleep 5; done")), "deny")
            self.assertEqual(decision(bash("timeout 600 bash -c 'until false; do sleep 5; done'")), "allow")
            self.assertEqual(decision(bash("ls -la")), "allow")
            self.assertEqual(decision(bash("# wait-loop-guard: ok\nwhile true; do sleep 5; done")), "allow")


class FrontWatchGate(Neutral):
    def test_floor_applies_to_everyone_without_stackshot(self):
        with temp_home() as home:
            env = env_for(home)
            bash = lambda c, **kw: self.hook("front-watch-gate.py", {"tool_name": "Bash", "tool_input": {"command": c, **kw}}, env)[0]  # noqa: E731
            out = bash("gh run watch 12345")
            self.assertEqual(decision(out), "deny")
            self.assertNotIn("ci-runs", reason(out))                   # no hint without the setting
            self.assertEqual(decision(bash("gh run watch 12345 --interval 60")), "allow")
            self.assertEqual(decision(bash("while true; do gh run view 1; sleep 5; done")), "deny")
            self.assertEqual(decision(bash("gh run view 1")), "allow")
            self.assertEqual(decision(bash("ls")), "allow")
            env2 = env_for(home, STACKSHOT_PLUGIN_CI_RUNS_HINT="`ci-runs <repo> --sha <sha>` reads a local file.")
            out = self.hook("front-watch-gate.py", {"tool_name": "Bash", "tool_input": {"command": "gh run watch 9"}}, env2)[0]
            self.assertIn("ci-runs <repo>", reason(out))

    def test_rule_one_needs_roles_and_says_so_without(self):
        with temp_home() as home:
            env = env_for(home)
            wake = {"tool_name": "ScheduleWakeup", "tool_input": {"reason": "check later", "prompt": "x"}}
            out, err = self.hook("front-watch-gate.py", wake, env)
            self.assertEqual(decision(out), "allow")                    # no roles file: the rule is off ...
            self.assertIn("roles file", err)                            # ... and says so in one line
            self.assertEqual(len(err.strip().splitlines()), 1)

    def test_rule_one_with_roles(self):
        with temp_home() as home:
            roles = os.path.join(home, "roles.json")
            with open(roles, "w", encoding="utf-8") as f:
                json.dump({"sessions": {"acme-main": {"role": "front", "project": "p1"}, "acme-builder": {"role": "back", "project": "p1"}}}, f)
            front = env_for(home, BUILD_STOPS_ROLES=roles, BUILD_STOPS_NAME="acme-main")
            back = env_for(home, BUILD_STOPS_ROLES=roles, BUILD_STOPS_NAME="acme-builder")
            wake = {"tool_name": "ScheduleWakeup", "tool_input": {"reason": "check later", "prompt": "x"}}
            self.assertEqual(decision(self.hook("front-watch-gate.py", wake, front)[0]), "deny")
            self.assertEqual(decision(self.hook("front-watch-gate.py", wake, back)[0]), "allow")
            excused = {"tool_name": "ScheduleWakeup", "tool_input": {"reason": "watch-reason: no push exists", "prompt": "x"}}
            self.assertEqual(decision(self.hook("front-watch-gate.py", excused, front)[0]), "allow")
            bg = {"tool_name": "Bash", "tool_input": {"command": "until false; do sleep 5; done", "run_in_background": True}}
            self.assertEqual(decision(self.hook("front-watch-gate.py", bg, front)[0]), "deny")
            plain = {"tool_name": "Bash", "tool_input": {"command": "make test", "run_in_background": True}}
            self.assertEqual(decision(self.hook("front-watch-gate.py", plain, front)[0]), "allow")
            sub = dict(wake, agent_id="a1")                             # a subagent is never held to rule 1
            self.assertEqual(decision(self.hook("front-watch-gate.py", sub, front)[0]), "allow")


class BuildStops(Neutral):
    def setup_world(self, home):
        roles = os.path.join(home, "roles.json")
        with open(roles, "w", encoding="utf-8") as f:
            json.dump({"sessions": {"acme-main": {"role": "front", "project": "prj_testproj"},
                                    "acme-builder": {"role": "back", "project": "prj_testproj"}}}, f)
        repo = make_git_stub(os.path.join(home, "work", "widgets"), "https://example.com/acme/widgets.git")
        cfg = {"projects": {"prj_testproj": {"short": "widgets", "builder": "acme-builder", "release": "ship.sh",
                                              "repos": ["acme/widgets"]}}}
        return roles, repo, cfg

    def test_off_without_setting_says_so_at_start(self):
        with temp_home() as home:
            env = env_for(home)
            out, err = self.hook("build-stops.py", {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"}, env)
            self.assertEqual(out, "")
            self.assertEqual(len(err.strip().splitlines()), 1)
            edit = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}}
            out, err = self.hook("build-stops.py", edit, env)
            self.assertEqual((out, err), ("", ""))                      # a tool call stays silent

    def test_stops_with_setting(self):
        with temp_home() as home:
            roles, repo, cfg = self.setup_world(home)
            front = env_for(home, BUILD_STOPS_ROLES=roles, BUILD_STOPS_NAME="acme-main", STACKSHOT_PLUGIN_BUILD_STOPS=json.dumps(cfg))
            back = env_for(home, BUILD_STOPS_ROLES=roles, BUILD_STOPS_NAME="acme-builder", STACKSHOT_PLUGIN_BUILD_STOPS=json.dumps(cfg))
            start = {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"}
            self.assertIn("acme-builder", context(self.hook("build-stops.py", start, front)[0]))
            self.assertEqual(self.hook("build-stops.py", start, back)[0], "")
            target = os.path.join(repo, "src", "a.py")
            edit = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Edit", "cwd": home, "tool_input": {"file_path": target}}
            self.assertEqual(decision(self.hook("build-stops.py", edit, front)[0]), "deny")
            self.assertEqual(decision(self.hook("build-stops.py", edit, back)[0]), "allow")
            elsewhere = dict(edit, tool_input={"file_path": os.path.join(home, "notes.txt")})
            self.assertEqual(decision(self.hook("build-stops.py", elsewhere, front)[0]), "allow")
            write = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "cwd": home,
                     "tool_input": {"command": f"echo hi > {target}"}}
            self.assertEqual(decision(self.hook("build-stops.py", write, front)[0]), "deny")
            read = dict(write, tool_input={"command": f"cat {target}"})
            self.assertEqual(decision(self.hook("build-stops.py", read, front)[0]), "allow")
            commit = dict(write, cwd=repo, tool_input={"command": "git commit -m x"})
            out = self.hook("build-stops.py", commit, front)[0]
            self.assertEqual(decision(out), "note")                     # a warning, not a block
            self.assertIn("acme-builder", context(out))

    def test_merge_notice_goes_to_the_maintainer(self):
        with temp_home() as home:
            roles, repo, cfg = self.setup_world(home)
            pushed = os.path.join(home, "pushed.txt")
            env = env_for(home, BUILD_STOPS_ROLES=roles, BUILD_STOPS_NAME="acme-main", STACKSHOT_PLUGIN_BUILD_STOPS=json.dumps(cfg),
                          BUILD_STOPS_PUSH=f"{sys.executable} {os.path.join(home, 'push.py')}", PUSH_OUT=pushed)
            with open(os.path.join(home, "push.py"), "w", encoding="utf-8") as f:
                f.write('import os, sys\nopen(os.environ["PUSH_OUT"], "w").write(" ".join(sys.argv[1:]))\n')
            ev = {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Bash", "cwd": repo,
                  "tool_input": {"command": "git merge feature"}, "tool_response": {"stdout": "Merge made by the 'ort' strategy.", "stderr": ""}}
            self.hook("build-stops.py", ev, env)
            deadline = time.time() + 10
            while not os.path.exists(pushed) and time.time() < deadline:
                time.sleep(0.1)                                         # the push child is detached and ends by itself
            self.assertTrue(os.path.exists(pushed))
            text = S.read(pushed)
            self.assertIn("acme-builder", text)
            self.assertIn("ship.sh", text)
            failed = dict(ev, tool_response={"stdout": "", "stderr": "fatal: not something we can merge"})
            os.unlink(pushed)
            self.hook("build-stops.py", failed, env)
            time.sleep(0.5)
            self.assertFalse(os.path.exists(pushed))


class BoardRepoGuard(Neutral):
    def write_config(self, home, repo="acme/board", aliases=(), repo_id=None):
        d = os.path.join(home, ".config", "stackshot")
        os.makedirs(d, exist_ok=True)
        al = ", ".join(f'"{a}"' for a in aliases)
        rid = f"issues_repo_id = {repo_id}\n" if repo_id is not None else ""
        with open(os.path.join(d, "stackshot.toml"), "w", encoding="utf-8") as f:
            f.write(f'[github]\nsync = true\nissues_repo = "{repo}"\nissues_repo_aliases = [{al}]\n{rid}')

    def test_gh_without_config_allows(self):
        with temp_home() as home:
            env = env_for(home)
            ev = {"tool_name": "Bash", "tool_input": {"command": "gh issue comment 1 -R acme/board -b hi"}}
            self.assertEqual(self.hook("board-repo-guard.py", ev, env)[0], "")

    def test_gh_with_config(self):
        with temp_home() as home:
            self.write_config(home, aliases=["acme/old-board"])
            env = env_for(home)
            bash = lambda c: self.hook("board-repo-guard.py", {"tool_name": "Bash", "tool_input": {"command": c}}, env)[0]  # noqa: E731
            out = bash("gh issue comment 1 -R acme/board -b hi")
            self.assertEqual(decision(out), "deny")
            self.assertIn("acme/board", reason(out))
            self.assertEqual(decision(bash("gh issue close 4 --repo acme/old-board")), "deny")
            self.assertEqual(decision(bash("gh api -X POST repos/acme/board/issues -f title=x")), "deny")
            self.assertEqual(decision(bash("gh issue view 1 -R acme/board")), "allow")
            self.assertEqual(decision(bash("gh issue comment 1 -R other/repo -b hi")), "allow")
            self.assertEqual(decision(bash("echo gh issue comment -R acme/board")), "allow")
            self.assertEqual(decision(bash("ls")), "allow")

    REPO_ID = 987654321                             # made up; the config below is written to a temp folder

    def id_bash(self, home):
        env = env_for(home)
        return lambda c: self.hook("board-repo-guard.py", {"tool_name": "Bash", "tool_input": {"command": c}}, env)[0]  # noqa: E731

    def test_gh_api_by_repo_id_refused(self):
        """`[github] issues_repo_id` set: a gh api write on repositories/<id>/... is refused like the name form."""
        with temp_home() as home:
            self.write_config(home, aliases=["acme/old-board"], repo_id=self.REPO_ID)
            bash = self.id_bash(home)
            rid = self.REPO_ID
            refused = [
                f"gh api -X POST repositories/{rid}/issues -f title=x",
                f"gh api -X POST /repositories/{rid}/issues -f title=x",
                f"gh api repositories/{rid}/issues -X POST -f title=x",
                f"gh api --method PATCH repositories/{rid}/issues/3 -f state=closed",
                f"gh api --method=DELETE /repositories/{rid}/labels/bug",
                f"gh api -XPUT repositories/{rid}/issues/3/lock",
                f"gh api repositories/{rid}/issues -f title=x",                       # fields alone make gh POST
                f"gh api -f title=x /repositories/{rid}/issues/3/comments",
                f"gh api --input body.json repositories/{rid}/issues",
                f"gh api -X POST 'repositories/{rid}/issues' -f title=x",
                f'gh api -X POST "/repositories/{rid}/issues?per_page=1" -f title=x',
                f"gh api -X POST https://api.github.com/repositories/{rid}/issues -f title=x",
                f"gh api -X POST 'https://api.github.com/repositories/{rid}/issues/3/comments' -f body=x",
                f"gh api -X POST Repositories/{rid}/issues -f title=x",
                f"bash -c 'gh api -X POST repositories/{rid}/issues -f title=x'",
                f"cd /tmp && env FOO=1 gh api -X POST /repositories/{rid}/issues -f title=x",
            ]
            for c in refused:
                with self.subTest(command=c):
                    out = bash(c)
                    self.assertEqual(decision(out), "deny")
                    self.assertIn("writes to the board repo acme/board directly", reason(out))   # named by its configured name
                    self.assertIn("(board-repo-guard.py)", reason(out))
            # the name forms are still refused, and reads stay open
            self.assertEqual(decision(bash("gh api -X POST repos/acme/board/issues -f title=x")), "deny")
            self.assertEqual(decision(bash("gh issue comment 1 -R acme/board -b hi")), "deny")
            self.assertEqual(decision(bash("gh issue close 4 --repo acme/old-board")), "deny")
            for c in (f"gh api repositories/{rid}/issues", f"gh api -X GET /repositories/{rid}/issues",
                      f"gh api https://api.github.com/repositories/{rid}/issues/3", "gh issue view 1 -R acme/board"):
                with self.subTest(command=c):
                    self.assertEqual(decision(bash(c)), "allow")

    def test_gh_api_by_other_repo_id_allowed(self):
        """A different id, or the id inside a longer number, is another repo: allowed."""
        with temp_home() as home:
            self.write_config(home, repo_id=self.REPO_ID)
            bash = self.id_bash(home)
            rid = str(self.REPO_ID)
            for other in ("123456789", rid[:-1], "1" + rid, rid + "0", rid + "1234"):
                for c in (f"gh api -X POST repositories/{other}/issues -f title=x",
                          f"gh api -X POST /repositories/{other}/issues -f title=x",
                          f"gh api -X POST https://api.github.com/repositories/{other}/issues -f title=x"):
                    with self.subTest(command=c):
                        self.assertEqual(decision(bash(c)), "allow")
            for c in (f"gh api -X POST repositories/{rid}x/issues -f title=x",
                      f"gh api -X POST repos/acme/elsewhere/issues -f title={rid}"):
                with self.subTest(command=c):
                    self.assertEqual(decision(bash(c)), "allow")

    def test_gh_api_odd_ids_do_not_crash_the_check(self):
        """An id that int() cannot parse must not crash the hook and let the rest of the command through."""
        with temp_home() as home:
            self.write_config(home, repo_id=self.REPO_ID)
            bash = self.id_bash(home)
            rid = str(self.REPO_ID)
            arabic = rid.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))   # Arabic-Indic digits
            for odd in ("²", "4" * 4400, arabic):
                with self.subTest(id=odd[:12]):
                    out = bash(f"gh api -X POST repositories/{odd}/issues && gh issue close 4 -R acme/board")
                    self.assertEqual(decision(out), "deny")
                    self.assertIn("gh issue close", reason(out))
            for odd in ("7" * 5000, arabic, "²"):
                with self.subTest(id=odd[:12]):
                    self.assertEqual(decision(bash(f"gh api -X POST repositories/{odd}/issues -f title=x")), "allow")
            # leading zeros still name the board repo, and the URL prefix matches in any case
            for c in (f"gh api -X POST repositories/00{rid}/issues -f title=x",
                      f"gh api -X POST HTTPS://API.GITHUB.COM/repositories/{rid}/issues -f title=x",
                      f"gh api -X POST HTTPS://API.GITHUB.COM/repos/acme/board/issues -f title=x"):
                with self.subTest(command=c):
                    self.assertEqual(decision(bash(c)), "deny")

    def test_gh_api_by_repo_id_without_key(self):
        """No `issues_repo_id`: the id form passes and the name forms are refused as before."""
        with temp_home() as home:
            self.write_config(home, aliases=["acme/old-board"])
            bash = self.id_bash(home)
            rid = self.REPO_ID
            for c in (f"gh api -X POST repositories/{rid}/issues -f title=x",
                      f"gh api -X POST /repositories/{rid}/issues -f title=x",
                      f"gh api -X POST https://api.github.com/repositories/{rid}/issues -f title=x"):
                with self.subTest(command=c):
                    self.assertEqual(decision(bash(c)), "allow")
            self.assertEqual(decision(bash("gh api -X POST repos/acme/board/issues -f title=x")), "deny")
            self.assertEqual(decision(bash("gh issue close 4 --repo acme/old-board")), "deny")
            self.assertEqual(decision(bash("gh issue view 1 -R acme/board")), "allow")
            # a key that is not an integer is the same as no key
            for bad in ('"abc"', "true", "0", "-5"):
                with self.subTest(value=bad):
                    self.write_config(home, repo_id=bad)
                    self.assertEqual(decision(bash(f"gh api -X POST repositories/{rid}/issues -f title=x")), "allow")
                    self.assertEqual(decision(bash("gh api -X POST repos/acme/board/issues -f title=x")), "deny")

    def test_connector_calls(self):
        with temp_home() as home:
            self.write_config(home)
            call = {"tool_name": "mcp__claude_ai_GitHub_MCP__issue_write",
                    "mcp_server": {"name": "claude.ai GitHub MCP"}, "tool_input": {"owner": "acme", "repo": "board", "method": "create"}}
            read = dict(call, tool_name="mcp__claude_ai_GitHub_MCP__get_issue")
            script = {"tool_name": "mcp__portal__portal_codemode_execute",
                      "tool_input": {"code": "await codemode.github_issue_write({owner: 'acme', repo: 'board', method: 'create'})"}}
            # without stackshot: allowed, with one line saying why
            env = env_for(home)
            out, err = self.hook("board-repo-guard.py", call, env)
            self.assertEqual(out, "")
            self.assertIn("connectors.py was not found", err)
            self.assertEqual(len(err.strip().splitlines()), 1)
            # with the stub checkout: the write is refused, the read passes
            stub = make_stub_stackshot(home)
            env = env_for(home, STACKSHOT_DIR=stub)
            self.assertEqual(decision(self.hook("board-repo-guard.py", call, env)[0]), "deny")
            self.assertEqual(decision(self.hook("board-repo-guard.py", read, env)[0]), "allow")
            self.assertEqual(decision(self.hook("board-repo-guard.py", script, env)[0]), "deny")
            other = dict(call, tool_input={"owner": "acme", "repo": "elsewhere", "method": "create"})
            self.assertEqual(decision(self.hook("board-repo-guard.py", other, env)[0]), "allow")
            # the stackshot folder also comes from the setting file
            with open(os.path.join(home, ".config", "stackshot", "plugin.json"), "w", encoding="utf-8") as f:
                json.dump({"stackshot_dir": stub}, f)
            env = env_for(home)
            self.assertEqual(decision(self.hook("board-repo-guard.py", call, env)[0]), "deny")

    def test_connector_calls_without_catalog(self):
        """No connector catalog on the box: the native connector is still judged from the tool name alone, in all three name forms."""
        with temp_home() as home:
            self.write_config(home)
            stub = make_stub_stackshot(home, catalog=False)
            env = env_for(home, STACKSHOT_DIR=stub)
            args = {"owner": "acme", "repo": "board", "method": "create"}
            forms = {
                "local": {"tool_name": "mcp__github-mcp__add_issue_comment", "mcp_server": {"name": "github-mcp"}},
                "claude.ai": {"tool_name": "mcp__claude_ai_GitHub_MCP__issue_write", "mcp_server": {"name": "claude.ai GitHub MCP"}},
                "older CLI, no mcp_server": {"tool_name": "mcp__github-mcp__add_issue_comment"},
            }
            for label, form in forms.items():
                with self.subTest(form=label):
                    call = dict(form, tool_input=args)
                    self.assertEqual(decision(self.hook("board-repo-guard.py", call, env)[0]), "deny")
                    read = dict(call, tool_name=form["tool_name"].rsplit("__", 1)[0] + "__get_issue")
                    self.assertEqual(decision(self.hook("board-repo-guard.py", read, env)[0]), "allow")
                    other = dict(call, tool_input=dict(args, repo="elsewhere"))
                    self.assertEqual(decision(self.hook("board-repo-guard.py", other, env)[0]), "allow")
            unrelated = {"tool_name": "mcp__other-server__add_issue_comment", "mcp_server": {"name": "other-server"}, "tool_input": args}
            self.assertEqual(decision(self.hook("board-repo-guard.py", unrelated, env)[0]), "allow")


class GbrainRedirectGuard(Neutral):
    def test_guard(self):
        with temp_home() as home:
            psql = os.path.join(home, "psql-stub")
            with open(psql, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\ncat >/dev/null\nprintf 'old/page\\tredirect\\tnew/page\\t\\n'\n")
            os.chmod(psql, 0o755)
            ev = {"tool_name": "mcp__acme_GBrain__put_page", "tool_input": {"slug": "old/page", "content": "x"}}
            fine = {"tool_name": "mcp__acme_GBrain__put_page", "tool_input": {"slug": "live/page", "content": "x"}}
            read = {"tool_name": "mcp__acme_GBrain__get_page", "tool_input": {"slug": "old/page"}}
            env = env_for(home, PSQL_BIN=psql)
            out = self.hook("gbrain-redirect-guard.py", ev, env)[0]
            self.assertEqual(decision(out), "deny")
            self.assertIn("new/page", reason(out))
            self.assertEqual(decision(self.hook("gbrain-redirect-guard.py", fine, env)[0]), "allow")   # not in the stub's answer: a live page
            self.assertEqual(decision(self.hook("gbrain-redirect-guard.py", read, env)[0]), "allow")
            # no psql and no gbrain CLI: the call goes on
            nothing = env_for(home, PSQL_BIN="/nonexistent/psql", GBRAIN_BIN="/nonexistent/gbrain")
            self.assertEqual(self.hook("gbrain-redirect-guard.py", ev, nothing)[0], "")
            # a portal script's call through the stub catalog is not resolved (stub): the plain tool still is
            self.assertEqual(self.hook("gbrain-redirect-guard.py", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, env)[0], "")


class PromptStamp(Neutral):
    def stamped(self, home, sid):
        return os.path.join(home, ".local", "state", "stackshot", "prompts", sid)

    def test_stamp(self):
        with temp_home() as home:
            env = env_for(home)
            self.hook("prompt-stamp.py", {"session_id": "abc", "prompt": "please fix the build"}, env)
            self.assertTrue(os.path.isfile(self.stamped(home, "abc")))
            self.assertTrue(S.read(self.stamped(home, "abc")).isdigit())
            for i, p in enumerate(["[anchor] x", "[stackshot] x", "<task-notification>x", "Another Claude session sent a message: x",
                                   "[for the owner] a question", "Stop hook feedback: x", ""]):
                self.hook("prompt-stamp.py", {"session_id": f"skip{i}", "prompt": p}, env)
                self.assertFalse(os.path.exists(self.stamped(home, f"skip{i}")), p)
            self.hook("prompt-stamp.py", {"session_id": "../evil", "prompt": "hi"}, env)
            self.assertFalse(os.path.exists(os.path.join(home, ".local", "state", "stackshot", "evil")))

    def test_owner_name_and_extra_prefixes_are_settings(self):
        with temp_home() as home:
            env = env_for(home, STACKSHOT_PLUGIN_OWNER_NAME="Ann", STACKSHOT_PLUGIN_PROMPT_SKIP_PREFIXES="[nudge],[other]")
            for i, p in enumerate(["[for Ann] x", "[nudge] x"]):
                self.hook("prompt-stamp.py", {"session_id": f"s{i}", "prompt": p}, env)
                self.assertFalse(os.path.exists(self.stamped(home, f"s{i}")), p)
            self.hook("prompt-stamp.py", {"session_id": "typed", "prompt": "hello"}, env)
            self.assertTrue(os.path.isfile(self.stamped(home, "typed")))


class PostToolNote(Neutral):
    def test_notes(self):
        with temp_home() as home:
            env = env_for(home)
            url = "https://github.com/acme/widgets/pull/7"
            ev = {"tool_name": "Bash", "tool_input": {"command": "gh pr create --title t --body b"}, "tool_response": {"stdout": url + "\n"}}
            out = self.hook("post-tool-note.py", ev, env)[0]
            self.assertIn(url, context(out))
            self.assertIn("anchor", context(out).lower())
            cap = {"tool_name": "Bash", "tool_input": {"command": "tmux capture-pane -p -t x"}, "tool_response": {"stdout": "hi"}}
            self.assertIn("unknown origin", context(self.hook("post-tool-note.py", cap, env)[0]))
            self.assertEqual(self.hook("post-tool-note.py", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, env)[0], "")
            failed = {"tool_name": "Bash", "tool_input": {"command": "gh pr create"}, "tool_response": {"stdout": "error"}}
            self.assertEqual(self.hook("post-tool-note.py", failed, env)[0], "")

    def test_portal_create_without_stackshot(self):
        with temp_home() as home:
            env = env_for(home)
            ev = {"tool_name": "mcp__portal__portal_codemode_execute",
                  "tool_input": {"code": "await codemode.github_create_pull_request({owner: 'acme', repo: 'widgets', title: 't'})"},
                  "tool_response": {"content": [{"type": "text", "text": "created https://github.com/acme/widgets/pull/9"}]}}
            out, err = self.hook("post-tool-note.py", ev, env)
            self.assertIn("https://github.com/acme/widgets/pull/9", context(out))


class RecurringJobNote(Neutral):
    def test_notes(self):
        with temp_home() as home:
            env = env_for(home)
            note = lambda tool, **kw: context(self.hook("recurring-job-note.py", {"tool_name": tool, "tool_input": kw}, env)[0])  # noqa: E731
            self.assertIn("recurring job", note("CronCreate", cron="*/5 * * * *", prompt="x"))
            self.assertEqual(note("CronCreate", cron="*/5 * * * *", prompt="x", recurring=False), "")
            self.assertIn("recurring job", note("Skill", skill="loop"))
            self.assertEqual(note("Skill", skill="spawn"), "")
            poll = "while true; do curl -s https://papers.example.org/latest; sleep 60; done"
            self.assertEqual(note("Bash", command=poll), "")            # no host setting, no second note
            env2 = env_for(home, STACKSHOT_PLUGIN_NO_POLL_HOSTS="example.org", STACKSHOT_PLUGIN_NO_POLL_NOTE="Do not poll {hosts}.")
            out = context(self.hook("recurring-job-note.py", {"tool_name": "Bash", "tool_input": {"command": poll}}, env2)[0])
            self.assertEqual(out, "Do not poll example.org.")
            once = {"tool_name": "Bash", "tool_input": {"command": "curl -s https://papers.example.org/latest"}}
            self.assertEqual(self.hook("recurring-job-note.py", once, env2)[0], "")


class ProjectPageKeeper(Neutral):
    def test_forwards_to_pages_py(self):
        with temp_home() as home:
            stub = make_stub_stackshot(home)
            env = env_for(home, STACKSHOT_DIR=stub)
            out, err = self.hook("claude-project-page.sh", {"session_id": "s"}, env, args=["nag"])
            self.assertEqual(out.strip(), "pages-stub nag")

    def test_off_without_stackshot(self):
        with temp_home() as home:
            out, err = self.hook("claude-project-page.sh", {"session_id": "s"}, env_for(home), args=["check"])
            self.assertEqual(out, "")
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertIn("page gate is off", err)

    def test_stackshot_dir_from_setting_file(self):
        with temp_home() as home:
            stub = make_stub_stackshot(home)
            os.makedirs(os.path.join(home, ".config", "stackshot"))
            with open(os.path.join(home, ".config", "stackshot", "plugin.json"), "w", encoding="utf-8") as f:
                json.dump({"stackshot_dir": stub}, f)
            out, _ = self.hook("claude-project-page.sh", {"session_id": "s"}, env_for(home), args=["keeper"])
            self.assertEqual(out.strip(), "pages-stub keeper")


class GbrainWriteback(Neutral):
    def stub_gbrain(self, home):
        out = os.path.join(home, "gbrain-ran.txt")
        path = os.path.join(home, "gbrain-stub")
        with open(path, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\ncat >/dev/null\n'
                    f'printf "%s|openai=%s|lane=%s\\n" "$*" "${{OPENAI_API_KEY:-unset}}" "${{GBRAIN_HOOK_LANE:-}}" > "{out}"\n')
        os.chmod(path, 0o755)
        return path, out

    def wait_for(self, path):
        deadline = time.time() + 10
        while not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.1)
        return os.path.exists(path)

    def test_runs_the_stub_detached(self):
        with temp_home() as home:
            stub, out = self.stub_gbrain(home)
            env = env_for(home, GBRAIN_WRITEBACK_BIN=stub, OPENAI_API_KEY="k-test")
            payload = {"session_id": "s", "cwd": home, "hook_event_name": "Stop"}
            o, e = self.hook("gbrain-writeback-hook.sh", payload, env)
            self.assertEqual((o, e), ("", ""))
            self.assertTrue(self.wait_for(out))
            line = S.read(out).strip()
            self.assertEqual(line, "hook stop|openai=k-test|lane=harness")       # no unset setting: the variable stays

    def test_unset_env_is_a_setting(self):
        with temp_home() as home:
            stub, out = self.stub_gbrain(home)
            env = env_for(home, GBRAIN_WRITEBACK_BIN=stub, OPENAI_API_KEY="k-test", STACKSHOT_PLUGIN_GBRAIN_UNSET_ENV="OPENAI_API_KEY")
            self.hook("gbrain-writeback-hook.sh", {"session_id": "s", "cwd": home}, env)
            self.assertTrue(self.wait_for(out))
            self.assertIn("openai=unset", S.read(out))

    def test_own_work_folders_are_skipped(self):
        with temp_home() as home:
            stub, out = self.stub_gbrain(home)
            env = env_for(home, GBRAIN_WRITEBACK_BIN=stub)
            self.hook("gbrain-writeback-hook.sh", {"session_id": "s", "cwd": os.path.join(home, ".gbrain", "x")}, env)
            time.sleep(1)
            self.assertFalse(os.path.exists(out))

    def test_no_gbrain_is_one_line(self):
        with temp_home() as home:
            o, e = self.hook("gbrain-writeback-hook.sh", {"session_id": "s", "cwd": home}, env_for(home))
            self.assertEqual(o, "")
            self.assertEqual(len(e.strip().splitlines()), 1)
            self.assertIn("gbrain is not on PATH", e)

    def bare_bin(self, home, drop):
        """A PATH folder of symlinks to the system tools the hook uses, without the names in `drop`."""
        d = os.path.join(home, "bare-bin")
        os.makedirs(d)
        for name in ("timeout", "cat", "sed", "head", "env", "dirname", "python3", "setsid", "nohup"):
            src = shutil.which(name)
            if src and name not in drop:
                os.symlink(src, os.path.join(d, name))
        return d

    def run_bare(self, home, bindir, stub):
        env = env_for(home, GBRAIN_WRITEBACK_BIN=stub, PATH=bindir)
        path = os.path.join(S.HOOKS, "gbrain-writeback-hook.sh")
        r = subprocess.run([shutil.which("bash"), path], input=json.dumps({"session_id": "s", "cwd": home}), capture_output=True,
                           text=True, env=env, timeout=30, cwd=home)
        return r.returncode, r.stdout, r.stderr

    def test_without_setsid_nohup_detaches(self):
        with temp_home() as home:
            stub, out = self.stub_gbrain(home)
            code, o, e = self.run_bare(home, self.bare_bin(home, drop=("setsid",)), stub)
            self.assertEqual((code, o, e), (0, "", ""))
            self.assertTrue(self.wait_for(out))
            self.assertEqual(S.read(out).strip(), "hook stop|openai=unset|lane=harness")

    def test_without_setsid_or_nohup_is_one_line(self):
        with temp_home() as home:
            stub, out = self.stub_gbrain(home)
            code, o, e = self.run_bare(home, self.bare_bin(home, drop=("setsid", "nohup")), stub)
            self.assertEqual((code, o), (0, ""))
            self.assertEqual(len(e.strip().splitlines()), 1)
            self.assertIn("neither setsid nor nohup", e)
            time.sleep(1)
            self.assertFalse(os.path.exists(out))


class Layout(unittest.TestCase):
    def test_manifests_and_hook_files(self):
        root = S.ROOT
        plugin = json.loads(S.read(os.path.join(root, ".claude-plugin", "plugin.json")))
        self.assertEqual(plugin["name"], "stackshot")
        market = json.loads(S.read(os.path.join(root, ".claude-plugin", "marketplace.json")))
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        hooks = json.loads(S.read(os.path.join(root, "hooks", "hooks.json")))["hooks"]
        seen = set()
        for event, groups in hooks.items():
            for g in groups:
                for h in g["hooks"]:
                    self.assertIn("${CLAUDE_PLUGIN_ROOT}", h["command"])
                    rel = h["command"].split("${CLAUDE_PLUGIN_ROOT}/")[1].split('"')[0]
                    self.assertTrue(os.path.isfile(os.path.join(root, rel)), rel)
                    seen.add(os.path.basename(rel))
        wanted = {"board-repo-guard.py", "build-stops.py", "front-watch-gate.py", "claude-project-page.sh", "prompt-stamp.py",
                  "post-tool-note.py", "wait-loop-guard.py", "recurring-job-note.py", "gbrain-writeback-hook.sh",
                  "gbrain-redirect-guard.py", "subagent-model-gate.py", "handoff-item-note.py"}
        self.assertEqual(seen, wanted)
        for a in ("executor", "deep-engineer", "sweeper"):
            text = S.read(os.path.join(root, "agents", a + ".md"))
            self.assertTrue(text.startswith("---\nname: " + a + "\n"), a)
        self.assertTrue(os.path.isfile(os.path.join(root, "skills", "spawn", "SKILL.md")))


if __name__ == "__main__":
    unittest.main()
