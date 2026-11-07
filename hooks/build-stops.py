#!/usr/bin/env python3
"""build-stops.py - the front agents' build stops.

A front agent of a project hands its build work straight to the project's builder instead of doing it itself. Three stops and a log,
all code: a local lookup of the pane's name and role, no model call, nothing in context until a stop fires. They bind a front agent
and the subagents it starts (a subagent's hook call carries its parent's session_id, with an agent_id beside it), never a builder,
another project's session or the owner's own.

  (a) SessionStart (startup, resume, compact): one line telling the agent that its build work goes to its builder, with the item and
      what done means, and that it may still read code. A project configured with "stopped": false gets this stop alone.
  (b) PreToolUse Edit, Write, MultiEdit, NotebookEdit: a file inside a guarded repository is refused, before any work is spent.
      Bash too, for the obvious writes whose target is inside one: a redirection, tee, sed/perl -i on an existing file, cp, mv,
      install, rsync, ln, rm, rmdir, touch, mkdir, truncate, and git apply, am, cherry-pick, revert, mv, rm. Reading always passes,
      and so does a command the parser cannot place.
  (c) PreToolUse Bash: git commit, git push, gh pr create, gh pr merge and git merge while on main, in a guarded repository, go
      through with a warning as additionalContext, addressed to the builder. PostToolUse Bash: after a merge whose output shows no
      failure, the project's maintainer is told through stackshot's push.py, detached and never waited for, that the PR was merged
      outside the release script and needs a review and a deploy.
  (d) Every firing is one JSON line in ~/.local/state/build-stops/firings.jsonl (under $XDG_STATE_HOME): t (UTC), stop (start | edit |
      warn | notice), session, sala_id ("" when the registry cannot tell), sid, agent_id, project, repo, tool, kind, target.

Settings. The projects, their builders and the repositories they guard are the owner's instance data, so they are a setting, never
code: `build_stops` (an object in ${XDG_CONFIG_HOME:-~/.config}/stackshot/plugin.json, or a JSON text in the environment variable
STACKSHOT_PLUGIN_BUILD_STOPS):

    {"projects": {"<project id>": {"short": "name used in the texts", "builder": "builder session name",
                                   "builder_id": "builder's sala id (optional; names it by the name it has now)",
                                   "maintainer": "who is told of an outside merge (default: the builder)",
                                   "release": "name of the release script, for the notice",
                                   "repos": ["owner/repo", ...],          # exact owner/repo of a guarded repository, any clone or worktree
                                   "repo_names": ["repo", ...],           # a repository by its name under any owner
                                   "keys": ["projects/page-key", ...],    # page keys that roles.json may carry for this project
                                   "stopped": true}},                     # false: only stop (a)
     "fork_owners": ["owner/", ...]}                                      # remote owners preferred when a repo has several remotes

With no `build_stops` setting there is no project, so the hook does nothing (silent exit 0). Without stackshot's roles file
(~/.local/state/stackshot/roles.json, written by the stackshot server from the project pages' agents: maps) it does nothing either;
at SessionStart it then says so in one line on stderr. A project is held by its permanent stackshot id (prj_<8 hex>); a roles.json
`project` that holds a page key is turned into the id through stackshot's published projects.json (`by_key`), else the project's
`keys`.

Who: the session's name comes from the fleet store first (stackshot's <fleet>/<letter>/sessions.json, transcript UUID -> name), then the
CLI's own registry (~/.claude/sessions/*.json): stackshot's pages.py session_name. Its role and project come from roles.json; a session
is a front agent when its role there is "front". No file, no name or no front role: silent exit 0.

Where: a guarded repository is found by git remote, never by path, in any clone or worktree (a .git file's gitdir and commondir are
followed to the config).

Sala ids (optional): a rename must need no edit here. The session is first resolved to its permanent sala id through sala's
session-registry.json (_sala_identity.py: the hook input's session_id, else the pane's $SALA_SESSION_ID); its role then comes from
roles.json's `ids` map (name -> sala id) by that id, so a renamed front agent keeps its stops while roles.json still lists the old
name. The builder named in the texts is shown by the name the registry gives its `builder_id` now. Wherever the registry cannot tell
(no sala, missing, unreadable, ambiguous, or silent on the session) the name logic runs.

Env (tests and the probe only): BUILD_STOPS_NAME (the session's name), BUILD_STOPS_ROLES (the roles file), BUILD_STOPS_PROJECTS
(stackshot's projects.json), STACKSHOT_FLEET_DIR, BUILD_STOPS_PUSH (a command run in place of push.py), XDG_STATE_HOME (the log's home).
Any error or unreadable input: silent exit 0 (fails open).
"""
import glob, json, os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402
try:                                     # the shared id lookup; without it the hook is the name-based hook
    import _sala_identity as ident
except Exception:
    ident = None

HOME = os.path.expanduser("~")
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local/state"), "build-stops")
ROLES = os.environ.get("BUILD_STOPS_ROLES") or os.path.join(HOME, ".local/state/stackshot/roles.json")
PROJECTS_FILE = os.environ.get("BUILD_STOPS_PROJECTS") or os.path.join(HOME, ".local/state/stackshot/projects.json")
STACKSHOT = _config.stackshot_dir()
TAG = "(build-stops.py)"


def _settings():
    """(PROJECTS, KEY_IDS, GUARDED, GUARDED_BY_NAME, STOPPED, FORK_OWNERS) from the `build_stops` setting; all empty without it."""
    cfg = _config.get_dict("build_stops")
    projects, key_ids, guarded, by_name, stopped = {}, {}, {}, {}, []
    raw = cfg.get("projects")
    for pid, p in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(p, dict):
            continue
        pid = str(pid)
        builder = str(p.get("builder") or "")
        projects[pid] = {"short": str(p.get("short") or pid), "builder": builder, "builder_id": str(p.get("builder_id") or ""),
                         "maintainer": str(p["maintainer"]) if "maintainer" in p else builder,
                         "release": str(p.get("release") or "the release script")}
        for k in p.get("keys") or []:
            key_ids[str(k).strip("/")] = pid
        for r in p.get("repos") or []:
            guarded[str(r).lower()] = pid
        for r in p.get("repo_names") or []:
            by_name[str(r).lower()] = pid
        if p.get("stopped", True):
            stopped.append(pid)
    owners = tuple(str(o).lower() for o in cfg.get("fork_owners") or [])
    return projects, key_ids, guarded, by_name, tuple(stopped), owners


PROJECTS, KEY_IDS, GUARDED, GUARDED_BY_NAME, STOPPED, FORK_OWNERS = _settings()


def guarded(s):
    """The project of a guarded owner/repo slug (lowercased), or None."""
    return GUARDED.get(s) or GUARDED_BY_NAME.get(s.rpartition("/")[2])


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
MAIN_BRANCHES = ("main", "master")

WRITE_ALL = {"rm", "rmdir", "touch", "mkdir", "truncate", "unlink", "shred"}      # every path argument is written
WRITE_LAST = {"cp", "mv", "install", "rsync", "ln"}                                 # the last argument is written
GIT_EDITS = {"apply", "am", "cherry-pick", "revert", "mv", "rm"}                     # (b): new code lands in the tree
GIT_WORK = {"commit": "commit", "push": "push", "merge": "merge"}                   # (c)
GH_PR = {"create": "pull request", "merge": "merge"}                                 # (c)
GH_VALUE_FLAGS = {"-b", "--body", "-F", "--body-file", "-t", "--subject", "--title", "-B", "--base", "-H", "--head",
                  "-a", "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer",
                  "-T", "--template", "--match-head-commit", "--author-email", "-R", "--repo"}
WRAPPERS = {"sudo", "nohup", "setsid", "env", "command", "builtin", "exec", "time", "stdbuf", "unbuffer"}
FAILED = re.compile(r"(fatal:|error:|conflict|not mergeable|failed|could not|already up to date|aborting|graphql:)", re.I)


# --- who ---------------------------------------------------------------------------------------------------------------
def fleet_dir():
    """stackshot's fleet view, as its config.fleet_dir() says (STACKSHOT_FLEET_DIR first), else its default place."""
    if os.environ.get("STACKSHOT_FLEET_DIR"):
        return os.environ["STACKSHOT_FLEET_DIR"]
    try:
        config = _config.import_stackshot("config", "build-stops", quiet=True)   # stackshot's config.py (stdlib tomllib)
        return str(config.fleet_dir())
    except BaseException:
        return os.path.join(HOME, ".local/state/stackshot/clients/fleet")


def session_name(sid):
    """pages.py session_name: the live fleet store first (transcript UUID -> name), then the CLI's own registry."""
    if os.environ.get("BUILD_STOPS_NAME"):
        return os.environ["BUILD_STOPS_NAME"]
    if not sid:
        return ""
    for f in glob.glob(os.path.join(fleet_dir(), "*", "sessions.json")):
        try:
            v = json.load(open(f, encoding="utf-8")).get(sid)
            if isinstance(v, dict) and v.get("name"):
                return v["name"]
        except Exception:
            continue
    for f in glob.glob(os.path.join(HOME, ".claude/sessions/*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
            if d.get("sessionId") == sid and d.get("name"):
                return d["name"]
        except Exception:
            continue
    return ""


def project_id(x):
    """The permanent project id for a roles.json `project` value (a page key or an id): '' when it names none of PROJECTS. By
    stackshot's projects.json first (a page renamed since is followed), else KEY_IDS."""
    x = str(x or "").strip().strip("/")
    if x in PROJECTS:
        return x
    try:
        i = (json.load(open(PROJECTS_FILE, encoding="utf-8")).get("by_key") or {}).get(x)
        if i in PROJECTS:
            return i
    except Exception:
        pass
    return KEY_IDS.get(x, "")


def project_key(pid):
    """The page key a project id has now (for the firings log); '' when projects.json cannot say."""
    try:
        return str((json.load(open(PROJECTS_FILE, encoding="utf-8")).get("projects") or {}).get(pid, {}).get("key") or "")
    except Exception:
        return ""


def role_entry(name, sala_id=""):
    """The roles.json entry (dict with role, project, ...) of the session, or None when it cannot be told. By its sala id first:
    the names roles.json's `ids` map gives that id, retired ones left out, must agree on one role and project (the file may still
    list a renamed session under its old name). Cannot tell (no id, no `ids` map, no entry, or entries that disagree): by `name`.
    front-watch-gate.py reads the role through this too."""
    try:
        d = json.load(open(ROLES, encoding="utf-8"))
        sessions = d.get("sessions") or {}
    except Exception:
        return None
    e = None
    ids = d.get("ids") if isinstance(d.get("ids"), dict) else {}
    if sala_id and ids:
        hits = [sessions[n] for n, i in ids.items() if i == sala_id and isinstance(sessions.get(n), dict) and not sessions[n].get("retired")]
        if hits and all((h.get("role"), h.get("project")) == (hits[0].get("role"), hits[0].get("project")) for h in hits):
            e = hits[0]
    if e is None:
        e = sessions.get(name) if name else None
    return e if isinstance(e, dict) else None


def front_project(name, sala_id=""):
    """The project id when the session is a front agent of one of PROJECTS (stackshot's roles file), else ''."""
    e = role_entry(name, sala_id)
    if e is None or e.get("role") != "front":
        return ""
    return project_id(e.get("project"))


def handoff(project):
    """Who a front agent hands build work to: the project's builder, directly. The builder reports on the board item, not by
    message, so REPORT_ON_ITEM rides with the name where the agent is told to hand work over."""
    return builder_name(project)


def builder_name(project):
    """The project's builder by the name its sala id goes by now (a rename needs no edit here), else the name written in PROJECTS."""
    p = PROJECTS[project]
    return (ident.current_name(p.get("builder_id")) if ident is not None else "") or p["builder"]


def maintainer_name(project):
    """Who is told of a merge made outside release.sh: the project's maintainer, which is its builder wherever PROJECTS says so.
    A name, for a person to read; the push goes to maintainer_target."""
    p = PROJECTS[project]
    return builder_name(project) if p["maintainer"] and p["maintainer"] == p["builder"] else p["maintainer"]


def maintainer_target(project):
    """The push target of the merge notice: the builder's permanent sala id (push.py delivers to the name it has now), else the maintainer's name."""
    p = PROJECTS[project]
    return p["builder_id"] if p["maintainer"] and p["maintainer"] == p["builder"] and p.get("builder_id") else p["maintainer"]


REPORT_ON_ITEM = "the result comes back on the board item, so never ask anyone to report to you"


# --- where -------------------------------------------------------------------------------------------------------------
def git_dir(path):
    """The nearest enclosing git tree of `path` (which need not exist): (root, this tree's gitdir), or None."""
    p = os.path.abspath(path)
    while True:
        g = os.path.join(p, ".git")
        if os.path.isdir(g):
            return p, g
        if os.path.isfile(g):
            try:
                line = open(g, encoding="utf-8").read().strip()
            except Exception:
                return None
            if line.startswith("gitdir:"):
                return p, os.path.normpath(os.path.join(p, line[7:].strip()))
            return None
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def slug(url):
    """owner/repo, lowercased, from a git remote url of any shape; '' when it has no such tail."""
    u = re.sub(r"\.git/?$", "", url.strip().rstrip("/"))
    m = re.search(r"(?:^|[:/])([^/:]+)/([^/:]+)$", u)
    return (m.group(1) + "/" + m.group(2)).lower() if m else ""


def repo_of(path):
    """The guarded repository holding `path`: {project, slug, root, gitdir}, or None."""
    gd = git_dir(path)
    if not gd:
        return None
    root, gitdir = gd
    common = gitdir
    try:
        c = open(os.path.join(gitdir, "commondir"), encoding="utf-8").read().strip()
        common = os.path.normpath(os.path.join(gitdir, c))
    except Exception:
        pass
    try:
        cfg = open(os.path.join(common, "config"), encoding="utf-8").read()
    except Exception:
        return None
    hits = [s for s in (slug(m.group(1)) for m in re.finditer(r"^\s*url\s*=\s*(\S+)", cfg, re.M)) if guarded(s)]
    if not hits:
        return None
    s = next((h for h in hits if FORK_OWNERS and h.startswith(FORK_OWNERS)), hits[0])     # the fork's name over its upstream's
    return {"project": guarded(s), "slug": s, "root": root, "gitdir": gitdir}


def head_branch(repo):
    try:
        h = open(os.path.join(repo["gitdir"], "HEAD"), encoding="utf-8").read().strip()
    except Exception:
        return ""
    return h[len("ref: refs/heads/"):] if h.startswith("ref: refs/heads/") else ""


def resolve(arg, cwd):
    """A path argument as the shell would take it, or '' when it holds something the hook cannot expand (or is relative
    and `cwd` is None: the folder is unknown)."""
    a = arg.strip()
    if not a or a.startswith("-"):
        return ""
    a = a.replace("${HOME}", HOME).replace("$HOME", HOME)
    if "$" in a or "`" in a:
        return ""
    a = os.path.expanduser(a)
    return a if os.path.isabs(a) else (os.path.join(cwd, a) if cwd is not None else "")


# --- the shell command, read cheaply -----------------------------------------------------------------------------------
def strip_heredocs(cmd):
    """The command without its here-document bodies (and their << markers), so a body line is never read as a command."""
    for _ in range(10):
        m = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", cmd)
        if not m:
            break
        eol = cmd.find("\n", m.end())
        if eol < 0:
            break
        t = re.search(r"^\t*" + re.escape(m.group(2)) + r"[ \t]*$", cmd[eol + 1:], re.M)
        if not t:
            break
        cmd = cmd[:m.start()] + cmd[m.end():eol] + cmd[eol + 1 + t.end():]
    return cmd


def tokenize(cmd):
    """[(kind, text)]: 'w' words with quotes resolved, 'op' unquoted runs of <>|&;() — a quoted '>' is a word."""
    toks, cur, have, i, n = [], "", False, 0, len(cmd)

    def flush():
        nonlocal cur, have
        if have:
            toks.append(("w", cur))
        cur, have = "", False
    while i < n:
        c = cmd[i]
        if c == "\\" and i + 1 < n:
            cur += cmd[i + 1]; have = True; i += 2
        elif c == "'":
            j = cmd.find("'", i + 1); j = n if j < 0 else j
            cur += cmd[i + 1:j]; have = True; i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    cur += cmd[j + 1]; j += 2
                else:
                    cur += cmd[j]; j += 1
            have = True; i = j + 1
        elif c in " \t\r\n":
            flush()
            if c == "\n":
                toks.append(("op", ";"))
            i += 1
        elif c in "<>|&;()":
            flush(); j = i
            while j < n and cmd[j] in "<>|&;()":
                j += 1
            toks.append(("op", cmd[i:j])); i = j
        else:
            cur += c; have = True; i += 1
    flush()
    return toks


def segments(cmd):
    """The command's simple commands: each a list of tokens, separators dropped, redirections kept as ('op', ...)."""
    out, cur = [], []
    for k, t in tokenize(strip_heredocs(cmd)):
        if k == "op" and not (">" in t or "<" in t):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append((k, t))
    if cur:
        out.append(cur)
    return out


def words_of(seg):
    """(command name, its arguments) of a simple command, redirections and their targets, wrappers and VAR=x left out."""
    ws, skip = [], False
    for k, t in seg:
        if skip:
            skip = False
            continue
        if k == "op":
            skip = True                      # the redirection's target (or a here-string / input file)
            continue
        ws.append(t)
    while ws and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", ws[0]):
        ws.pop(0)
    while ws and os.path.basename(ws[0]) in WRAPPERS:
        ws.pop(0)
        while ws and (ws[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", ws[0])):
            ws.pop(0)
    if ws and os.path.basename(ws[0]) == "timeout":
        ws = ws[1:]
        while ws and ws[0].startswith("-"):
            ws = ws[2:] if ws[0] in ("-k", "--kill-after", "-s", "--signal") else ws[1:]
        ws = ws[1:]                          # the duration
    if not ws:
        return "", []
    return os.path.basename(ws[0]), ws[1:]


def redirect_targets(seg):
    out = []
    for i, (k, t) in enumerate(seg):
        if k == "op" and ">" in t and i + 1 < len(seg) and seg[i + 1][0] == "w":
            nxt = seg[i + 1][1]
            if not (t.endswith("&") or nxt.isdigit()):
                out.append(nxt)
    return out


def git_call(args):
    """(dir from -C, subcommand, its arguments) of a git command's arguments."""
    d, i = "", 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            d = os.path.join(d, args[i + 1]) if d and not os.path.isabs(args[i + 1]) else args[i + 1]; i += 2
        elif a in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
        else:
            i += 1
    if i >= len(args):
        return d, "", []
    return d, args[i], args[i + 1:]


def gh_call(args):
    """(repo from -R/--repo, the command path such as ['pr', 'merge'], the positional arguments after it)."""
    repo, path, pos, i = "", [], [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--repo=") or a.startswith("-R="):
            repo = a.split("=", 1)[1]; i += 1
        elif a in ("-R", "--repo") and i + 1 < len(args):
            repo = args[i + 1]; i += 2
        elif a.startswith("-"):
            i += 2 if a in GH_VALUE_FLAGS and "=" not in a and i + 1 < len(args) else 1
        elif len(path) < 2:
            path.append(a); i += 1
        else:
            pos.append(a); i += 1
    return repo, path, pos


def pr_ref(pos):
    for p in pos:
        if p.isdigit():
            return "PR #" + p
        m = re.search(r"/pull/(\d+)", p)
        if m:
            return "PR #" + m.group(1)
        return "the PR of branch " + p
    return "the current branch's PR"


def read_command(cmd, cwd):
    """What the command does to guarded repositories: {'edits': [(repo, target)], 'work': [(repo, kind, detail)]}."""
    edits, work, here = [], [], cwd
    rhere = cwd                              # where a redirect lands: follows cd/pushd; None once a cd target is unknown
    for seg in segments(cmd):
        name, args = words_of(seg)
        for t in redirect_targets(seg):
            # After a cd into a folder the hook cannot expand ($D, `...`, $(...), cd -), a relative target's folder is unknown:
            # let it pass. A false refusal costs a front agent its turn, and the stop is a nudge, not a security boundary.
            p = resolve(t, rhere)
            r = repo_of(p) if p else None
            if r:
                edits.append((r, p))
        if name in ("cd", "pushd"):
            dest = args[0] if args else (HOME if name == "cd" else "")      # a bare pushd swaps folders: unknown
            here = resolve(dest, here) or here
            rhere = resolve(dest, rhere) or None
            continue
        paths = [a for a in args if not a.startswith("-")]
        if name == "tee":
            cand = paths
        elif name in ("sed", "perl") and any(re.match(r"^(-[a-zA-Z]*i|--in-place)", a) for a in args):
            cand = [p for p in paths if (resolve(p, here) and os.path.isfile(resolve(p, here)))]
        elif name in WRITE_ALL:
            cand = paths
        elif name in WRITE_LAST:
            cand = [p for p in paths[-1:] if not re.match(r"^[^/]+:", p)]
        else:
            cand = []
        for c in cand:
            p = resolve(c, here)
            r = repo_of(p) if p else None
            if r:
                edits.append((r, p))
        if name == "git":
            d, sub, rest = git_call(args)
            at = resolve(d, here) if d else here
            r = repo_of(at) if at else None
            if not r:
                continue
            if sub in GIT_EDITS:
                edits.append((r, "git " + sub + " in " + r["root"]))
            elif sub == "merge":
                b = head_branch(r)
                if b in MAIN_BRANCHES:
                    src = next((x for x in rest if not x.startswith("-")), "a branch")
                    work.append((r, "merge", f"branch {src} was merged to {b}"))
            elif sub in GIT_WORK:
                work.append((r, GIT_WORK[sub], "git " + sub))
        elif name == "gh":
            repo, path, pos = gh_call(args)
            if path[:1] != ["pr"] or len(path) < 2 or path[1] not in GH_PR:
                continue
            r = None
            if not repo:                     # a PR given by its URL names the repository too
                repo = next((re.sub(r"/pull/\d+.*$", "", p) for p in pos if "/pull/" in p), "")
            if repo:
                s = slug(repo)
                if guarded(s):
                    r = {"project": guarded(s), "slug": s, "root": "", "gitdir": ""}
            else:
                r = repo_of(here)
            if not r:
                continue
            kind = GH_PR[path[1]]
            work.append((r, kind, (pr_ref(pos) + " was merged") if kind == "merge" else "gh pr create"))
    return {"edits": edits, "work": work}


# --- the stops' texts --------------------------------------------------------------------------------------------------
def start_text(name, project):
    p = PROJECTS[project]
    s = (f"[build-stops] You ({name}) are a front agent of {p['short']}: your build work (code, commits, pull requests, "
         f"merges, deploys) goes to {handoff(project)} ({REPORT_ON_ITEM}); hand it over with the item and what done means. You may still read code.")
    if project in STOPPED:
        s += (" In the guarded repositories a code edit is refused, and a commit, push, PR or merge goes "
              "through with a warning.")
    return s + " " + TAG


def edit_text(name, repo, target, tool):
    b = h = handoff(repo["project"])
    what = f"a code edit in {repo['slug']} ({target})" if tool != "Bash" else f"this shell command writes into {repo['slug']} ({target}), which"
    return (f"{name} is a front agent: {what} is {b}'s work. Hand the job to {h} with the item and what done means ({REPORT_ON_ITEM}), before "
            f"any work is spent; reading the code is fine. {TAG}")


def warn_text(name, repo, kind):
    p = PROJECTS[repo["project"]]
    s = (f"[build-stops] {name}: this {kind} in {repo['slug']} is accepted because the work is already done, but from this "
         f"point on hand it to {handoff(repo['project'])}, not only for this task but for all future build tasks: a front agent's code, "
         f"commits, pull requests, merges and deploys in {repo['slug']} are {handoff(repo['project'])}'s.")
    if kind == "merge":
        s += f" After the merge {maintainer_name(repo['project'])} is told to review and deploy, so production does not lag."
    return s + f" {TAG[:-1]}; the call is not blocked.)"


def notice_text(name, repo, detail):
    p = PROJECTS[repo["project"]]
    return f"[build-stops] {repo['slug']}: {detail} outside {p['release']} by {name} (a front agent) and needs a review and a deploy."


# --- outputs -----------------------------------------------------------------------------------------------------------
def log(rec):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "firings.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def out(event, **fields):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}, ensure_ascii=False))
    return 0


def push(to, sender, text):
    """stackshot's push.py, detached: the hook never waits for the delivery (push.py --from <sender> <to> <text>)."""
    cmd = os.environ.get("BUILD_STOPS_PUSH")
    if not cmd and not os.path.isfile(os.path.join(STACKSHOT, "push.py")):
        _config.note("build-stops", "stackshot's push.py was not found (set STACKSHOT_DIR); the merge notice was not sent")
        return
    argv = (cmd.split() if cmd else [sys.executable, os.path.join(STACKSHOT, "push.py")]) + ["--from", sender, to, text]
    os.makedirs(STATE, exist_ok=True)
    lf = open(os.path.join(STATE, "push.log"), "ab")
    env = dict(os.environ, ANCHOR_PUSH_SOURCE="msg")
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, env=env)


# --- main --------------------------------------------------------------------------------------------------------------
def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(ev, dict):
        return 0
    event = str(ev.get("hook_event_name") or "")
    if not PROJECTS or not os.path.isfile(ROLES):
        if event == "SessionStart":              # once a session: the stops are off, and why
            _config.note("build-stops", "no `build_stops` setting or no stackshot roles file; the front-agent stops are off")
        return 0
    sid = str(ev.get("session_id") or "").strip() or os.path.basename(str(ev.get("transcript_path") or "")).replace(".jsonl", "")
    row = ident.resolve(sid) if ident is not None and not os.environ.get("BUILD_STOPS_NAME") else None
    sala_id = str(row.get("id") or "").lower() if row else ""
    name = (str(row.get("name") or "") if row else "") or session_name(sid)
    project = front_project(name, sala_id)
    if not project:
        return 0
    who = {"session": name, "sala_id": sala_id, "sid": sid, "agent_id": str(ev.get("agent_id") or ""), "project": project,
           "project_key": project_key(project)}

    if event == "SessionStart":
        log({"stop": "start", **who, "source": str(ev.get("source") or "")})
        return out("SessionStart", additionalContext=start_text(name, project))

    if project not in STOPPED:
        return 0
    tool = str(ev.get("tool_name") or "")
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    cwd = str(ev.get("cwd") or "") or os.getcwd()

    if event == "PreToolUse" and tool in EDIT_TOOLS:
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if not path:
            return 0
        path = path if os.path.isabs(path) else os.path.join(cwd, path)
        r = repo_of(path)
        if not r:
            return 0
        log({"stop": "edit", **who, "repo": r["slug"], "tool": tool, "target": path})
        return out("PreToolUse", permissionDecision="deny", permissionDecisionReason=edit_text(name, r, path, tool))

    if tool != "Bash":
        return 0
    cmd = str(inp.get("command") or "")
    if not cmd.strip():
        return 0
    read = read_command(cmd, cwd)

    if event == "PreToolUse":
        if read["edits"]:
            r, target = read["edits"][0]
            log({"stop": "edit", **who, "repo": r["slug"], "tool": tool, "target": target, "command": cmd[:200]})
            return out("PreToolUse", permissionDecision="deny", permissionDecisionReason=edit_text(name, r, target, tool))
        if read["work"]:
            r, kind, detail = read["work"][0]
            log({"stop": "warn", **who, "repo": r["slug"], "tool": tool, "kind": kind, "command": cmd[:200]})
            return out("PreToolUse", additionalContext=warn_text(name, r, kind))
        return 0

    if event == "PostToolUse":
        merges = [(r, d) for r, k, d in read["work"] if k == "merge"]
        if not merges:
            return 0
        resp = ev.get("tool_response") if isinstance(ev.get("tool_response"), dict) else {}
        if resp.get("interrupted"):
            return 0
        text = str(resp.get("stdout") or "") + "\n" + str(resp.get("stderr") or "")
        if FAILED.search(text):
            return 0
        r, detail = merges[0]
        to = maintainer_target(r["project"])
        if not to:                                   # a project with no maintainer and no builder: nobody to tell
            return 0
        msg = notice_text(name, r, detail)
        log({"stop": "notice", **who, "repo": r["slug"], "tool": tool, "kind": "merge", "to": to, "to_name": maintainer_name(r["project"]), "command": cmd[:200]})
        push(to, "build-stops", msg)
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
