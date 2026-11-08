"""The hooks find stackshot's modules in both checkout layouts: the package (<checkout>/stackshot/<area>/<module>.py) and the older
one (<checkout>/<module>.py). The package wins when a checkout holds both, which is what a checkout does while the old root paths
are one-line forwarders. Every checkout here is a fake one in a temp folder; nothing reads a real stackshot checkout."""
import json
import os
import subprocess
import sys
import time
import unittest

import _support as S
import test_neutral_install as N
from _support import env_for, make_git_stub, make_stub_stackshot, place_stub_module, temp_home

CONFIG_PY = os.path.join(S.HOOKS, "_config.py")


def run_config(home, *args, **extra):
    r = subprocess.run([sys.executable, CONFIG_PY, *args], capture_output=True, text=True, timeout=30, cwd=home,
                       env=env_for(home, **extra))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def import_in_child(home, module, **extra):
    """Run _config.import_stackshot(module) in a fresh interpreter; returns the imported module's WHERE marker (or None) and stderr."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import _config; m = _config.import_stackshot(sys.argv[2], 'test'); "
            "print(getattr(m, 'WHERE', None) if m else None); print(getattr(m, '__name__', None) if m else None)")
    r = subprocess.run([sys.executable, "-c", code, S.HOOKS, module], capture_output=True, text=True, timeout=30, cwd=home,
                       env=env_for(home, **extra))
    where, name = (r.stdout.split("\n") + ["", ""])[:2]
    return where, name, r.stderr.strip()


def marker(where):
    return f'WHERE = "{where}"\n'


class Layouts(unittest.TestCase):
    def test_path_in_each_layout(self):
        for layout, rel in (("root", "pages.py"), ("package", os.path.join("stackshot", "projects", "pages.py"))):
            with self.subTest(layout=layout), temp_home() as home:
                d = make_stub_stackshot(home, layout=layout)
                rc, out, _ = run_config(home, "stackshot-path", "pages", STACKSHOT_DIR=d)
                self.assertEqual((rc, out), (0, os.path.join(d, rel)))
                rc, out, _ = run_config(home, "stackshot-path", "push", STACKSHOT_DIR=d)
                self.assertEqual(rc, 0)
                self.assertTrue(out.endswith("push.py"))

    def test_package_wins_when_both_exist(self):
        with temp_home() as home:
            d = make_stub_stackshot(home, layout="root")
            place_stub_module(d, "pages", "# package copy\n", "package")
            rc, out, _ = run_config(home, "stackshot-path", "pages", STACKSHOT_DIR=d)
            self.assertEqual(out, os.path.join(d, "stackshot", "projects", "pages.py"))
            rc, out, _ = run_config(home, "stackshot-path", "push", STACKSHOT_DIR=d)      # only the root copy of push exists
            self.assertEqual(out, os.path.join(d, "push.py"))

    def test_a_module_is_found_in_whatever_area_holds_it(self):
        """The plugin keeps no list of areas: a module moved to another folder is still found."""
        with temp_home() as home:
            d = os.path.join(home, "co")
            for area in ("zeta", "alpha"):
                os.makedirs(os.path.join(d, "stackshot", area))
            for init in ("stackshot/__init__.py", "stackshot/zeta/__init__.py", "stackshot/alpha/__init__.py", "stackshot/alpha/thing.py"):
                open(os.path.join(d, init), "w").close()
            rc, out, _ = run_config(home, "stackshot-path", "thing", STACKSHOT_DIR=d)
            self.assertEqual(out, os.path.join(d, "stackshot", "alpha", "thing.py"))

    def test_nothing_found(self):
        with temp_home() as home:
            rc, out, _ = run_config(home, "stackshot-path", "pages", STACKSHOT_DIR=os.path.join(home, "absent"))
            self.assertEqual((rc, out), (1, ""))
            d = os.path.join(home, "empty")                    # a folder named stackshot without __init__.py is not the package
            os.makedirs(os.path.join(d, "stackshot", "projects"))
            open(os.path.join(d, "stackshot", "projects", "pages.py"), "w").close()
            rc, out, _ = run_config(home, "stackshot-path", "pages", STACKSHOT_DIR=d)
            self.assertEqual((rc, out), (1, ""))

    def test_import_in_each_layout(self):
        for layout in ("root", "package"):
            with self.subTest(layout=layout), temp_home() as home:
                d = os.path.join(home, "co")
                os.makedirs(d)
                place_stub_module(d, "config", marker(layout), layout)
                where, name, err = import_in_child(home, "config", STACKSHOT_DIR=d)
                self.assertEqual((where, err), (layout, ""))
                self.assertEqual(name, "config" if layout == "root" else "stackshot.core.config")

    def test_import_prefers_the_package(self):
        with temp_home() as home:
            d = os.path.join(home, "co")
            os.makedirs(d)
            place_stub_module(d, "config", marker("root"), "root")
            place_stub_module(d, "config", marker("package"), "package")
            self.assertEqual(import_in_child(home, "config", STACKSHOT_DIR=d)[0], "package")

    def test_import_missing_and_broken(self):
        with temp_home() as home:
            d = os.path.join(home, "co")
            os.makedirs(d)
            where, _, err = import_in_child(home, "config", STACKSHOT_DIR=d)
            self.assertEqual(where, "None")
            self.assertIn("config.py was not found", err)
            place_stub_module(d, "config", "raise RuntimeError('x')\n", "package")
            where, _, err = import_in_child(home, "config", STACKSHOT_DIR=d)
            self.assertEqual(where, "None")
            self.assertIn("could not be imported (RuntimeError)", err)
            self.assertEqual(len(err.splitlines()), 1)

    def test_stackshot_has(self):
        code = "import sys; sys.path.insert(0, sys.argv[1]); import _config; print(_config.stackshot_has(sys.argv[2]))"
        for layout in ("root", "package"):
            with self.subTest(layout=layout), temp_home() as home:
                d = make_stub_stackshot(home, layout=layout)
                open(os.path.join(d, "stackshot.toml"), "w").close()
                for name, want in (("push.py", "True"), ("stackshot.toml", "True"), ("nothing.py", "False")):
                    r = subprocess.run([sys.executable, "-c", code, S.HOOKS, name], capture_output=True, text=True, cwd=home,
                                       env=env_for(home, STACKSHOT_DIR=d))
                    self.assertEqual(r.stdout.strip(), want, name)


class PageGate(N.ProjectPageKeeper):
    """The page gate's whole set of tests again, on a package-layout checkout."""

    def setUp(self):
        S.LAYOUT = "package"
        self.addCleanup(setattr, S, "LAYOUT", "root")


class BoardRepoGuardPackage(N.BoardRepoGuard):
    """The guard imports stackshot's connectors module: its tests again, on a package-layout checkout."""

    def setUp(self):
        S.LAYOUT = "package"
        self.addCleanup(setattr, S, "LAYOUT", "root")


class MergeNotice(N.Neutral):
    """build-stops runs stackshot's push module itself when BUILD_STOPS_PUSH is not set."""

    def merge_event(self, repo):
        return {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Bash", "cwd": repo,
                "tool_input": {"command": "git merge feature"}, "tool_response": {"stdout": "Merge made by the 'ort' strategy.", "stderr": ""}}

    def test_push_found_in_each_layout(self):
        for layout in ("root", "package"):
            with self.subTest(layout=layout), temp_home() as home:
                roles = os.path.join(home, "roles.json")
                with open(roles, "w", encoding="utf-8") as f:
                    json.dump({"sessions": {"acme-main": {"role": "front", "project": "prj_testproj"},
                                            "acme-builder": {"role": "back", "project": "prj_testproj"}}}, f)
                repo = make_git_stub(os.path.join(home, "work", "widgets"), "https://example.com/acme/widgets.git")
                cfg = {"projects": {"prj_testproj": {"short": "widgets", "builder": "acme-builder", "release": "ship.sh",
                                                      "repos": ["acme/widgets"]}}}
                stub = make_stub_stackshot(home, layout=layout)
                pushed = os.path.join(home, "pushed.txt")
                env = env_for(home, BUILD_STOPS_ROLES=roles, BUILD_STOPS_NAME="acme-main", STACKSHOT_PLUGIN_BUILD_STOPS=json.dumps(cfg),
                              STACKSHOT_DIR=stub, PUSH_OUT=pushed)
                self.hook("build-stops.py", self.merge_event(repo), env)
                deadline = time.time() + 10
                while not os.path.exists(pushed) and time.time() < deadline:
                    time.sleep(0.1)                             # the push child is detached and ends by itself
                self.assertTrue(os.path.exists(pushed), layout)
                self.assertIn("acme-builder", S.read(pushed))

    def test_no_push_module_says_so(self):
        with temp_home() as home:
            roles = os.path.join(home, "roles.json")
            with open(roles, "w", encoding="utf-8") as f:
                json.dump({"sessions": {"acme-main": {"role": "front", "project": "prj_testproj"}}}, f)
            repo = make_git_stub(os.path.join(home, "work", "widgets"), "https://example.com/acme/widgets.git")
            cfg = {"projects": {"prj_testproj": {"short": "widgets", "builder": "acme-builder", "release": "ship.sh",
                                                  "repos": ["acme/widgets"]}}}
            env = env_for(home, BUILD_STOPS_ROLES=roles, BUILD_STOPS_NAME="acme-main", STACKSHOT_PLUGIN_BUILD_STOPS=json.dumps(cfg),
                          STACKSHOT_DIR=os.path.join(home, "absent"))
            _, err = self.hook("build-stops.py", self.merge_event(repo), env)
            self.assertIn("push.py was not found", err)


if __name__ == "__main__":
    unittest.main()
