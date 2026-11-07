"""_config.py - the plugin's settings, shared by every hook.

A value that differs from one owner's setup to the next is a setting with a neutral default, never a literal in a hook. A setting
is read, first match wins, from
  1. the environment variable STACKSHOT_PLUGIN_<NAME> (upper case), a plain string, a comma-separated list or a JSON value;
  2. the key <name> of ${XDG_CONFIG_HOME:-$HOME/.config}/stackshot/plugin.json (a JSON object);
  3. the default the caller gives.
Two settings keep the names stackshot itself uses: STACKSHOT_DIR (the stackshot checkout) and, for the board repo, STACKSHOT_CONFIG.

Nothing here raises: a missing or unreadable file is the same as no file. Stdlib only.
"""
import json
import os
import sys

_FILE = None
_NOTED = set()


def config_file():
    """${XDG_CONFIG_HOME:-$HOME/.config}/stackshot/plugin.json; a relative XDG_CONFIG_HOME is ignored (the XDG specification)."""
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "stackshot", "plugin.json")


def _load():
    global _FILE
    if _FILE is None:
        try:
            with open(config_file(), encoding="utf-8") as f:
                d = json.load(f)
            _FILE = d if isinstance(d, dict) else {}
        except Exception:
            _FILE = {}
    return _FILE


def reset():
    """Forget the cached file (tests)."""
    global _FILE
    _FILE = None


def _env(name):
    v = os.environ.get("STACKSHOT_PLUGIN_" + name.upper())
    return None if v is None or not v.strip() else v.strip()


def get(name, default=None):
    """The setting as the environment or the file holds it: a JSON value when the environment text parses as one that is a list,
    an object, a number or a boolean, else the plain string. `default` when neither has it."""
    v = _env(name)
    if v is not None:
        if v[0] in "[{" or v in ("true", "false", "null"):
            try:
                return json.loads(v)
            except ValueError:
                pass
        return v
    f = _load()
    return f[name] if name in f else default


def get_str(name, default=""):
    v = get(name, default)
    return v if isinstance(v, str) else (default if v is None else str(v))


def get_list(name, default=()):
    """A list of strings: a JSON list, or a comma-separated string, or the default."""
    v = get(name, None)
    if v is None:
        return list(default)
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x) for x in v] if isinstance(v, list) else list(default)


def get_dict(name, default=None):
    v = get(name, None)
    return v if isinstance(v, dict) else dict(default or {})


def stackshot_dir():
    """The stackshot checkout: $STACKSHOT_DIR, else the setting stackshot_dir, else ~/stackshot (stackshot's documented place)."""
    return os.path.expanduser(os.environ.get("STACKSHOT_DIR") or get_str("stackshot_dir") or "~/stackshot")


def stackshot_has(filename):
    """True when the stackshot checkout holds `filename`."""
    return os.path.isfile(os.path.join(stackshot_dir(), filename))


def note(hook, text):
    """One line on stderr, once per process: the hook went on without something it can use. Never raises, never a block."""
    key = (hook, text)
    if key in _NOTED:
        return
    _NOTED.add(key)
    try:
        sys.stderr.write(f"stackshot-plugin: {hook}: {text}\n")
    except Exception:
        pass


def import_stackshot(module, hook, quiet=False):
    """stackshot's module `module` (connectors, config ...) imported from the checkout, or None with one line of note (none when
    `quiet`) when the checkout or the module is absent or does not import. The checkout's folder is on sys.path only while it imports."""
    d = stackshot_dir()
    if not os.path.isfile(os.path.join(d, module + ".py")):
        if not quiet:
            note(hook, f"stackshot's {module}.py was not found (set STACKSHOT_DIR to the stackshot checkout); going on without it")
        return None
    sys.path.insert(0, d)
    try:
        return __import__(module)
    except BaseException as e:
        if not quiet:
            note(hook, f"stackshot's {module}.py could not be imported ({e.__class__.__name__}); going on without it")
        return None
    finally:
        try:
            sys.path.remove(d)
        except ValueError:
            pass


def _cli(argv):
    """For the shell hooks:  _config.py get NAME | list NAME | stackshot-dir. Prints the value (one list item per line)."""
    if argv[:1] == ["stackshot-dir"]:
        print(stackshot_dir())
    elif argv[:1] == ["get"] and len(argv) == 2:
        print(get_str(argv[1]))
    elif argv[:1] == ["list"] and len(argv) == 2:
        for x in get_list(argv[1]):
            print(x)
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
