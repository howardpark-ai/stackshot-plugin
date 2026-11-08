"""Shared helpers of the plugin's tests: a locked, self-cleaning temp home, a stub stackshot checkout, a hook runner.

Nothing here kills a process, touches the real home directory or uses tmux. Each test makes its own temp folder with `temp_home()`:
the folder is locked (flock on its own descriptor, which systemd-tmpfiles ageing honors) while the test runs and removed when the
block ends, pass or fail.
"""
import contextlib
import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "hooks")

# What must never show in a neutral install's output or in the tree: the setup of the person who wrote the plugin. The repository holds
# only structural patterns that name no one (an item reference, a home path). The instance patterns (names, addresses, machines,
# companies, accounts, sessions) are read the way tools/clean_text.py reads them: the environment variable CLEAN_TEXT_DENY, else the
# per-user file ${XDG_CONFIG_HOME:-$HOME/.config}/stackshot/clean-text-deny, one case-insensitive regex per line. With neither, only the
# generic patterns run and INSTANCE_NOTE says so. A pattern is never printed, and neither is the text it hit: a failure names a
# location and "deny pattern N" (the line number in the list) or "generic pattern N".
# (The first mirrors tools/clean_text.py's board-item pattern; the hook tests use other trackers' item references, which are fine.)
GENERIC_PATTERNS = [r"board[\w-]*#\d+"]
# A home path is checked in the tree only: a hook's output names where the plugin is installed, which is under a home folder on a runner.
TREE_ONLY_PATTERNS = [r"/home/[a-z]"]


def _load_clean_text():
    spec = importlib.util.spec_from_file_location("_clean_text", os.path.join(ROOT, "tools", "clean_text.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_instance_patterns():
    """([(line number, compiled regex)], note or None). A pattern of the list that is invalid is an error that names its number only."""
    ct = _load_clean_text()
    raw, _source = ct.deny_source()
    deny, bad = ct.load_deny(raw)
    if bad:
        raise RuntimeError("; ".join(f"deny pattern {n} {why}" for n, why in bad))
    note = None if deny else ("the deny list is not available (CLEAN_TEXT_DENY is empty or unset and there is no per-user deny file): "
                              "only the generic patterns ran")
    return deny, note


class _Patterns:
    """The generic patterns plus the loaded instance patterns. `hits(text)` names which matched, never the matched text."""

    def __init__(self, generic, instance):
        self.generic = [(i, re.compile(p, re.I)) for i, p in enumerate(generic, 1)]
        self.instance = instance

    def hits(self, text):
        return ([f"generic pattern {i}" for i, rx in self.generic if rx.search(text)]
                + [f"deny pattern {i}" for i, rx in self.instance if rx.search(text)])

    def search(self, text):
        """True when any pattern matches `text`, else None (a match object would carry the matched text)."""
        return True if self.hits(text) else None


_INSTANCE, INSTANCE_NOTE = _load_instance_patterns()
OWNER_RX = _Patterns(GENERIC_PATTERNS, _INSTANCE)                       # what a hook prints
TREE_RX = _Patterns(GENERIC_PATTERNS + TREE_ONLY_PATTERNS, _INSTANCE)   # what a file or file name holds


def assert_no_owner(test, text, what):
    """Fail `test` when `text` holds an instance or generic-pattern string, naming `what` and the patterns, never the text."""
    test.assertEqual(OWNER_RX.hits(text), [], f"{what} holds an owner string")


@contextlib.contextmanager
def temp_home():
    """A fresh folder under the system temp folder, locked while the block runs and removed when it ends."""
    d = tempfile.mkdtemp(prefix="stackshot-plugin-test-")
    fd = os.open(d, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        finally:
            os.close(fd)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def env_for(home, **extra):
    """A minimal environment for a hook: the temp home, no sala, no stackshot, no inherited plugin settings."""
    env = {"HOME": home, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "XDG_CONFIG_HOME": os.path.join(home, ".config"), "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def run_hook(name, payload, env, args=(), timeout=30):
    """Run hooks/<name> with `payload` (a dict, or str) on stdin. Returns (exit code, stdout, stderr)."""
    path = os.path.join(HOOKS, name)
    cmd = (["bash", path] if name.endswith(".sh") else [sys.executable, path]) + list(args)
    data = payload if isinstance(payload, str) else json.dumps(payload)
    r = subprocess.run(cmd, input=data, capture_output=True, text=True, env=env, timeout=timeout, cwd=env["HOME"])
    return r.returncode, r.stdout, r.stderr


def decision(stdout):
    """'deny' | 'note' | 'allow' for a PreToolUse hook's stdout (empty means allow)."""
    if not stdout.strip():
        return "allow"
    out = json.loads(stdout)["hookSpecificOutput"]
    return "deny" if out.get("permissionDecision") == "deny" else "note"


def context(stdout):
    return json.loads(stdout)["hookSpecificOutput"].get("additionalContext", "") if stdout.strip() else ""


def reason(stdout):
    return json.loads(stdout)["hookSpecificOutput"].get("permissionDecisionReason", "") if stdout.strip() else ""


# The layout make_stub_stackshot uses when a test does not name one; test_package_layout.py runs the hook tests again with "package".
LAYOUT = "root"

# Where stackshot's package keeps the modules the hooks use (the stand-in's own copy of the map; the plugin keeps none).
STUB_AREAS = {"connectors": "fleet", "push": "fleet", "config": "core", "pages": "projects"}


def place_stub_module(d, module, text, layout="root"):
    """Write the stand-in module `module` into the checkout `d`: at its root (an older checkout, layout "root") or as
    stackshot/<area>/<module>.py (layout "package", with the __init__.py files that make it a package)."""
    if layout == "package":
        folder = os.path.join(d, "stackshot", STUB_AREAS[module])
        os.makedirs(folder, exist_ok=True)
        for init in (os.path.join(d, "stackshot", "__init__.py"), os.path.join(folder, "__init__.py")):
            if not os.path.exists(init):
                open(init, "w", encoding="utf-8").close()
    else:
        folder = d
    with open(os.path.join(folder, module + ".py"), "w", encoding="utf-8") as f:
        f.write(text)


def make_stub_stackshot(parent, catalog=True, layout=None):
    """A stand-in for a stackshot checkout in `parent`/stackshot: just enough of connectors.py, config.py, pages.py and push.py for the
    hooks, laid out as an older checkout (layout "root") or as the package (layout "package"). With catalog=False its connectors.py
    has no connector catalog (load_catalog() is None). Returns its path."""
    layout = layout or LAYOUT
    d = os.path.join(parent, "stackshot")
    os.makedirs(d)
    connectors = ('''
import collections, re
Res = collections.namedtuple("Res", "route calls")
Call = collections.namedtuple("Call", "server tool args clean")
PORTAL = "portal"
CATALOG = {"servers": {}, "portal": "portal"}

def load_catalog():
    return CATALOG

def names_of(portal):
    return [("portal", "mcp__portal__")]

def prefix_of(name):
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", name) + "__"

def resolve(tool, inp, mcp_server=None, catalog=None):
    return Res(None, [])

def matches(calls, patterns):
    return []

def read_script(code, catalog):
    calls, problems = [], []
    entries = sorted(catalog.get("servers") or {}, key=len, reverse=True)
    for m in re.finditer(r"codemode\\.([A-Za-z_]\\w*)\\(\\{([^}]*)\\}\\)", code):
        ident = m.group(1)
        server = next((e for e in entries if ident.startswith(e + "_")), None)
        if not server:
            continue
        args = dict(re.findall(r"(\\w+)\\s*:\\s*['\\"]([^'\\"]*)['\\"]", m.group(2)))
        calls.append(Call(server, ident[len(server) + 1:], args, True))
    return calls, problems
''')
    if not catalog:
        connectors += "\nCATALOG = None\n"
    place_stub_module(d, "connectors", connectors, layout)
    place_stub_module(d, "config", 'import os\n\ndef fleet_dir():\n    return os.path.join(os.path.expanduser("~"), "fleet-stub")\n', layout)
    place_stub_module(d, "pages", 'import sys\nsys.stdin.read()\nprint("pages-stub " + " ".join(sys.argv[1:]))\n', layout)
    place_stub_module(d, "push", 'import os, sys\nopen(os.environ["PUSH_OUT"], "w").write(" ".join(sys.argv[1:]))\n', layout)
    return d


def make_git_stub(root, remote_url, branch="main"):
    """A folder that looks like a clone to the hooks (a .git folder with a config and a HEAD), without running git."""
    os.makedirs(os.path.join(root, ".git"))
    with open(os.path.join(root, ".git", "config"), "w", encoding="utf-8") as f:
        f.write(f'[remote "origin"]\n\turl = {remote_url}\n')
    with open(os.path.join(root, ".git", "HEAD"), "w", encoding="utf-8") as f:
        f.write(f"ref: refs/heads/{branch}\n")
    return root
