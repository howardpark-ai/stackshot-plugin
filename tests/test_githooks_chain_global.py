"""The repo's own git hooks (.githooks/) also run the machine's global git hook of the same name (.githooks/chain-global).

A clone that sets `core.hooksPath .githooks` replaces the global hooks directory, so a machine-wide hook would never run there. Each
test copies the hooks and tools/clean_text.py into a scratch git repository and runs the hook the way git does (relative path, the
repository as the working directory). HOME, XDG_CONFIG_HOME, GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM and TMPDIR all point into the
scratch folder, so the real machine's hooks and git settings are never read, run or changed. Stand-in global hooks record what they
were given in the scratch folder. Nothing here kills a process; a hook that loops is ended by the subprocess timeout."""
import os
import shutil
import subprocess
import unittest

import _support as S

ZERO = "0" * 40
# A message the repo's own check refuses (the generic board-item pattern), written in two pieces so this file stays clean itself.
BAD_MESSAGE = "Fix thing for board" + "#12\n"
GOOD_MESSAGE = "Fix thing\n"


class Scratch:
    """A scratch git repository holding the hooks and the check, a private home, and stand-in global hooks."""

    def __init__(self, home):
        self.home = home
        self.repo = os.path.join(home, "repo")
        self.out = os.path.join(home, "out")
        self.tmp = os.path.join(home, "tmp")
        self.xdg_hooks = os.path.join(home, ".config", "git", "hooks")
        self.gitconfig = os.path.join(home, "gitconfig")
        for d in (self.repo, self.out, self.tmp):
            os.makedirs(d)
        open(self.gitconfig, "w").close()
        self.env = S.env_for(home, GIT_CONFIG_GLOBAL=self.gitconfig, GIT_CONFIG_SYSTEM="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                             TMPDIR=self.tmp, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="noreply@example.invalid",
                             GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="noreply@example.invalid")
        shutil.copytree(os.path.join(S.ROOT, ".githooks"), os.path.join(self.repo, ".githooks"))
        os.makedirs(os.path.join(self.repo, "tools"))
        shutil.copy2(os.path.join(S.ROOT, "tools", "clean_text.py"), os.path.join(self.repo, "tools", "clean_text.py"))
        self.git("init", "-q")
        self.git("remote", "add", "origin", os.path.join(home, "no-such-remote.git"))

    def git(self, *args):
        r = subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()}")
        return r.stdout.strip()

    def commit(self, message):
        path = os.path.join(self.repo, "f")
        with open(path, "a") as f:
            f.write("x\n")
        self.git("add", "f")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def global_hook(self, name, code=0, directory=None, mode=0o755):
        """A stand-in global hook that records its arguments and standard input under out/<name>.{args,stdin} and exits `code`."""
        directory = directory or self.xdg_hooks
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.out}/{name}.args"\ncat > "{self.out}/{name}.stdin"\nexit {code}\n')
        os.chmod(path, mode)
        return path

    def recorded(self, name, what):
        path = os.path.join(self.out, f"{name}.{what}")
        return S.read(path) if os.path.exists(path) else None

    def run(self, name, args, stdin=""):
        r = subprocess.run([os.path.join(".githooks", name), *args], cwd=self.repo, env=self.env, input=stdin, capture_output=True,
                           text=True, timeout=30)
        return r.returncode, r.stderr

    def commit_msg(self, message):
        path = os.path.join(self.home, "MSG")
        with open(path, "w") as f:
            f.write(message)
        return self.run("commit-msg", [path]), path

    def pre_push(self, message):
        sha = self.commit(message)
        stdin = f"refs/heads/topic {sha} refs/heads/topic {ZERO}\n"
        return self.run("pre-push", ["origin", "https://example.invalid/r.git"], stdin), stdin


class ChainGlobal(unittest.TestCase):
    def setUp(self):
        cm = S.temp_home()
        self.sc = Scratch(cm.__enter__())
        self.addCleanup(cm.__exit__, None, None, None)

    def assert_tmp_empty(self):
        self.assertEqual(os.listdir(self.sc.tmp), [], "the pre-push hook left its temporary file behind")

    # no global hook: the hooks behave as before
    def test_no_global_hook_clean_passes(self):
        (code, _), _ = self.sc.commit_msg(GOOD_MESSAGE)
        self.assertEqual(code, 0)
        (code, _), _ = self.sc.pre_push(GOOD_MESSAGE)
        self.assertEqual(code, 0)
        self.assert_tmp_empty()

    def test_no_global_hook_own_check_refuses(self):
        (code, err), _ = self.sc.commit_msg(BAD_MESSAGE)
        self.assertEqual(code, 1)
        self.assertIn("commit-msg: the commit was refused", err)
        (code, err), _ = self.sc.pre_push(BAD_MESSAGE)
        self.assertEqual(code, 1)
        self.assertIn("pre-push: the push was refused", err)
        self.assert_tmp_empty()

    # a global hook that passes receives the same arguments and standard input
    def test_global_pass_gets_same_arguments_and_stdin(self):
        self.sc.global_hook("commit-msg")
        self.sc.global_hook("pre-push")
        (code, _), path = self.sc.commit_msg(GOOD_MESSAGE)
        self.assertEqual(code, 0)
        self.assertEqual(self.sc.recorded("commit-msg", "args"), path + "\n")
        (code, _), stdin = self.sc.pre_push(GOOD_MESSAGE)
        self.assertEqual(code, 0)
        self.assertEqual(self.sc.recorded("pre-push", "args"), "origin\nhttps://example.invalid/r.git\n")
        self.assertEqual(self.sc.recorded("pre-push", "stdin"), stdin)
        self.assert_tmp_empty()

    # a global hook that refuses fails the hook
    def test_global_refuses(self):
        self.sc.global_hook("commit-msg", 1)
        self.sc.global_hook("pre-push", 1)
        (code, _), _ = self.sc.commit_msg(GOOD_MESSAGE)
        self.assertEqual(code, 1)
        self.assertIsNotNone(self.sc.recorded("commit-msg", "args"))
        (code, _), _ = self.sc.pre_push(GOOD_MESSAGE)
        self.assertEqual(code, 1)
        self.assertIsNotNone(self.sc.recorded("pre-push", "args"))
        self.assert_tmp_empty()

    # the repo's own check refuses while the global hook passes: the hook fails, and the global hook still ran
    def test_own_refuses_global_passes(self):
        self.sc.global_hook("commit-msg")
        self.sc.global_hook("pre-push")
        (code, err), _ = self.sc.commit_msg(BAD_MESSAGE)
        self.assertEqual(code, 1)
        self.assertIn("the commit was refused", err)
        self.assertIsNotNone(self.sc.recorded("commit-msg", "args"))
        (code, err), stdin = self.sc.pre_push(BAD_MESSAGE)
        self.assertEqual(code, 1)
        self.assertEqual(self.sc.recorded("pre-push", "stdin"), stdin)
        self.assert_tmp_empty()

    # both refuse: both ran, the hook fails
    def test_both_refuse_both_ran(self):
        self.sc.global_hook("commit-msg", 1)
        self.sc.global_hook("pre-push", 1)
        (code, err), _ = self.sc.commit_msg(BAD_MESSAGE)
        self.assertEqual(code, 1)
        self.assertIn("the commit was refused", err)
        self.assertIsNotNone(self.sc.recorded("commit-msg", "args"))
        (code, err), stdin = self.sc.pre_push(BAD_MESSAGE)
        self.assertEqual(code, 1)
        self.assertIn("the push was refused", err)
        self.assertEqual(self.sc.recorded("pre-push", "stdin"), stdin)
        self.assert_tmp_empty()

    # a file that is not executable is ignored
    def test_non_executable_is_ignored(self):
        self.sc.global_hook("commit-msg", 1, mode=0o644)
        self.sc.global_hook("pre-push", 1, mode=0o644)
        (code, _), _ = self.sc.commit_msg(GOOD_MESSAGE)
        self.assertEqual(code, 0)
        (code, _), _ = self.sc.pre_push(GOOD_MESSAGE)
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(self.sc.out), [])

    # the global core.hooksPath is read from `git config --global` (a leading ~ expanded) and wins over the XDG folder
    def test_global_hooks_path_setting(self):
        custom = os.path.join(self.sc.home, "my-hooks")
        self.sc.global_hook("commit-msg", 1, directory=custom)
        self.sc.global_hook("commit-msg", 0)                      # the XDG one would pass; it must not be the one that runs
        subprocess.run(["git", "config", "--global", "core.hooksPath", custom], env=self.sc.env, check=True, timeout=30)
        (code, _), _ = self.sc.commit_msg(GOOD_MESSAGE)
        self.assertEqual(code, 1)

    def test_global_hooks_path_setting_with_tilde(self):
        self.sc.global_hook("commit-msg", 1, directory=os.path.join(self.sc.home, "tilde-hooks"))
        subprocess.run(["git", "config", "--global", "core.hooksPath", "~/tilde-hooks"], env=self.sc.env, check=True, timeout=30)
        (code, _), _ = self.sc.commit_msg(GOOD_MESSAGE)
        self.assertEqual(code, 1)

    # the global setting may point back at this repo's own hooks, or a symlink may lead to the own hook: no recursion
    def test_global_setting_pointing_at_own_hooks_does_not_loop(self):
        own = os.path.join(self.sc.repo, ".githooks")
        subprocess.run(["git", "config", "--global", "core.hooksPath", own], env=self.sc.env, check=True, timeout=30)
        (code, _), _ = self.sc.commit_msg(GOOD_MESSAGE)
        self.assertEqual(code, 0)
        (code, _), _ = self.sc.pre_push(GOOD_MESSAGE)
        self.assertEqual(code, 0)

    def test_symlink_to_own_hook_does_not_loop(self):
        os.makedirs(self.sc.xdg_hooks)
        for name in ("commit-msg", "pre-push"):
            os.symlink(os.path.join(self.sc.repo, ".githooks", name), os.path.join(self.sc.xdg_hooks, name))
        (code, _), _ = self.sc.commit_msg(GOOD_MESSAGE)           # a loop would end at the 30 s timeout with an exception
        self.assertEqual(code, 0)
        (code, _), _ = self.sc.pre_push(GOOD_MESSAGE)
        self.assertEqual(code, 0)

    def test_helper_modes(self):
        for name in ("chain-global", "commit-msg", "pre-push"):
            self.assertTrue(os.access(os.path.join(S.ROOT, ".githooks", name), os.X_OK), name)


if __name__ == "__main__":
    unittest.main()
