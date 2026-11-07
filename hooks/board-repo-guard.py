#!/usr/bin/env python3
"""board-repo-guard.py - PreToolUse hook (matcher `Bash|mcp__.*`): no session writes the board repo directly, by gh or the GitHub connector.

When the board is mirrored to GitHub Issues, sessions write the board repo only through stackshot's tools: the server adds the
`[author: ...]` header that tells a session's words from the owner's, and keeps its store (the working copy the board renders from) in
step with the issues. A `gh issue comment` typed in a pane does neither. This hook refuses such a write before it runs:
  - `gh issue create|new|edit|comment|close|reopen|delete|transfer|lock|unlock|pin|unpin|develop`
  - `gh label create|edit|delete|clone`
  - `gh pr create|new|edit|comment|close|reopen|merge|ready|review|lock|unlock|update-branch|revert`
  - `gh api` with a writing method (`-X`/`--method` POST, PATCH, PUT or DELETE; or, with no method given, `-f`/`-F`/
    `--field`/`--raw-field`/`--input`, which make gh send a POST) on `repos/<board repo>/...` or on the repo's numeric id,
    `repositories/<id>/...` (GitHub serves every repo at that path too, so it is refused like the name form), and `gh api graphql`
    with a mutation that names the repo (or its `{owner}`/`{repo}` placeholders, from a board clone)
when the command aims at the board repo: by `-R`/`--repo`, by a github.com URL in its arguments, by `GH_REPO` (in the command or the
environment), or, with none of these, by a working directory inside a clone whose remote is the board repo (a `cd` earlier in the same
command counts). `bash -c`/`sh -c`, `eval`, `ssh host '...'` (explicit targets only: the remote directory is unknown), and the
wrappers sudo/env/xargs/timeout/nohup/time/command/exec/nice/stdbuf are looked into.
Reads stay open: `gh issue view|list|status`, `gh api` GET (an explicit `-X GET` too), `gh api graphql` queries, `gh search`.

The repo is never typed here: it is `[github] issues_repo` of stackshot's config, the file found as stackshot's config.py finds it
($STACKSHOT_CONFIG or $BOARD_MCP_CONFIG, then ${XDG_CONFIG_HOME:-~/.config}/stackshot/stackshot.toml, then
$STACKSHOT_DIR/stackshot.toml, then each file of the setting stackshot_toml_fallbacks, a list of paths, default none). If the repo
moves, the hook follows the config. A renamed or moved repo's old name still reaches it, since GitHub redirects it, so the old names
are listed in the same section as `issues_repo_aliases = ["owner/name", ...]` and each is refused exactly like `issues_repo`, in
every form below (gh -R/--repo, URLs, `gh api` paths, a clone whose remote is the old name, the connector's owner/repo, the portal's
Code Mode). The repo's numeric id is read the same way, from `[github] issues_repo_id = 123456789` (an integer), and `gh api`
writes on `repositories/<id>/...` (with or without a leading slash, or as a full API URL) are refused like the name form, the refusal
naming the repo by its configured name; a config without the key refuses only the name forms. The GitHub connector takes
owner/repo, never an id, so the id is checked on the gh route only. With no config file, `[github] sync` not true, or no
`issues_repo`, the hook does nothing.

Exempt by construction: stackshot's own mirror writes the issues with urllib from the server process, not through a Claude session's
Bash tool, so no Claude hook ever sees it.

Cheap for unrelated Bash: the command is checked for a `gh` word before anything is imported or read; the config is read and a
`git remote -v` run only for a gh write. A refusal is a JSON permissionDecision "deny" on stdout with exit 0. Any error or unreadable
input: silent exit 0 (fails open). Env: STACKSHOT_CONFIG / BOARD_MCP_CONFIG, STACKSHOT_DIR, GH_REPO.

The GitHub connector (matcher `mcp__.*`, the second route). GitHub's MCP server writes the repo as directly as gh does, and the Bash
path never sees it. A write of it whose owner/repo is the board repo (case-insensitive; every `<x>owner`/`<x>repo` pair of the
arguments, so `issue_write`'s parent_owner/parent_repo counts too) is refused, made
  - on the direct connector (`mcp__claude_ai_GitHub_MCP__issue_write`), or
  - through the MCP portal's Code Mode (`codemode.github_<tool>({owner, repo, ...})` in a `portal_codemode_execute` script, on any
    portal name: the catalog's, the prefixes of the setting portal_prefixes, or any server's tool of that name). The entry id `github`
    (the setting github_entry) is known here whether or not the catalog carries it yet, so the refusal works before the first portal
    sync that adds the entry; a catalog entry that reads as GitHub's under another id is checked too. A write there whose owner/repo
    the script computes (a variable, or arguments that are not an object literal, or carry a spread or a computed key) is refused
    unless a literal part already rules the board out (it cannot be told apart), with the hint to name owner and repo literally. A
    script that hides its calls (an alias of `codemode`, `codemode[...]`, eval) and mentions both GitHub and the board repo's name is
    refused too. Reads (get_*, list_*, search_*, download_*, *_read) are never refused.
  - All names of the GitHub server are one server to the guard: the direct claude.ai connector, a local `mcp__github-mcp__` (the word
    "github" in the server name, below) and the portal route above.
Nothing names the connector. Which server a call reaches comes from stackshot's connectors.py (STACKSHOT_DIR) and its catalog:
  - a portal execute script first (`names_of` of the catalog's portal, with the typed fallbacks above, so a box without a catalog is
    covered too), read with connectors.read_script; then resolve(): a direct call to a catalog server (a GitHub portal entry, once
    joined with its connector by `connectors.py build`). A catalog server is GitHub when its upstream host is GitHub's
    (api.githubcopilot.com, *.github.com) or its name says GitHub.
  - else the native connector, which is not in the catalog: the hook input's `mcp_server.name` (claude 2.1.274+, the name resolve()
    also trusts first), confirmed by connectors.prefix_of(name) being the tool's prefix (the CLI's own sanitizer), which also gives
    the upstream tool name; without `mcp_server` (an older CLI), the server part of `mcp__<server>__<tool>`. Either is GitHub when it
    carries the word "github" (any case, between non-alphanumerics).
Write or read: the catalog's `read_only` (the tool's readOnlyHint, which GitHub's server sets on every tool) when the call resolved to
a catalog server whose tool list carries any read_only tool at all (a list with none means the hints are absent); otherwise the name
rule: a read is get_*, list_*, search_*, download_* or *_read, and every other tool is a write (issue_write, add_issue_comment,
create_or_update_file, merge_pull_request, sub_issue_write, request_copilot_review, fork_repository ...). A new GitHub tool is thus
refused on the board repo until it reads as a read, never let through unseen.
No decision without a config, with sync off, without connectors.py, or for any other server (a missing catalog only takes away the
catalog route: the native connector is still judged from the tool name alone);
without a stackshot checkout one line on stderr says so. Fast path: an MCP call with no `owner`/`repo`-like argument and no
`codemode` script passes before anything is read. If another hook allows the same call, the CLI keeps the strictest hook answer
(deny > ask > allow), so this refusal stands.
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402

TAG = "(board-repo-guard.py)"
GH_WORD = re.compile(r"(?:^|[\s;&|(`'\"/\\])gh(?:\.exe)?(?=[\s'\"]|$)")

ISSUE_WRITES = {"create", "new", "edit", "comment", "close", "reopen", "delete", "transfer", "lock", "unlock", "pin", "unpin",
                "develop"}
LABEL_WRITES = {"create", "edit", "delete", "clone"}
PR_WRITES = {"create", "new", "edit", "comment", "close", "reopen", "merge", "ready", "review", "lock", "unlock",
             "update-branch", "revert"}
API_VALUE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header", "-q", "--jq", "-t", "--template",
                   "--input", "--hostname", "--cache", "-p", "--preview"}
API_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}
WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
WRAPPERS = {"sudo", "env", "command", "exec", "time", "nohup", "timeout", "xargs", "nice", "stdbuf", "unbuffer"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
ITEM_VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--title", "-l", "--label", "-a", "--assignee",
                    "-m", "--milestone", "-p", "--project", "-B", "--base", "-H", "--head", "-c", "--color",
                    "--description", "-r", "--reviewer", "-T", "--template", "-n", "--name", "-s", "--state", "--reason",
                    "--add-label", "--remove-label", "--add-assignee", "--remove-assignee", "--add-project",
                    "--remove-project", "--add-reviewer", "--remove-reviewer", "--duplicate-of", "--subject",
                    "--match-head-commit", "--author-email", "--issue-repo"}
SSH_VALUE_FLAGS = {"-i", "-p", "-o", "-l", "-J", "-F", "-b", "-c", "-D", "-E", "-e", "-L", "-m", "-O", "-Q", "-R", "-S", "-W",
                   "-w", "-B"}
GH_URL = re.compile(r"github\.com[:/]+(?:repos/)?([\w.-]+)/([\w.-]+)", re.I)
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)

REFUSAL = ("Refused: `{what}` writes to the board repo {repo} directly. Sessions write the board only through stackshot's "
           "tools, which add the author header and keep stackshot's store in step with the issues: create, update, complete "
           "or message an item through the stackshot connector (its create / update / complete / message tools) or the MCP "
           "portal's codemode.stackshot_create / stackshot_update / stackshot_complete / "
           "stackshot_message. Reads stay open: gh issue view / list, gh api GET, gh search. " + TAG)
REFUSAL_MCP = ("Refused: `{what}` writes to the board repo {repo} directly through the GitHub connector{why}. Sessions write the "
               "board only through stackshot's tools, which add the author header and keep stackshot's store in step with the "
               "issues: create, update, complete or message an item through the stackshot connector (its create / update / "
               "complete / message tools) or the MCP portal's codemode.stackshot_create / stackshot_update / "
               "stackshot_complete / stackshot_message. Reads stay open: the GitHub connector's get_*, list_*, search_* and "
               "*_read tools. " + TAG)
GITHUB_WORD = re.compile(r"(?:^|[^a-z0-9])github(?:[^a-z0-9]|$)", re.I)
GITHUB_HOSTS = ("githubcopilot.com", "github.com")
READ_PREFIXES = ("get_", "list_", "search_", "download_")
READ_SUFFIXES = ("_read",)
MISSING = object()
EXECUTE = "portal_codemode_execute"


def github_entry():
    """The id of the GitHub entry in the MCP portal (setting github_entry, default `github`)."""
    return _config.get_str("github_entry", "github") or "github"


def portal_prefixes():
    """Typed fallbacks for the portal's tool-name prefixes (setting portal_prefixes), added to the catalog's."""
    return tuple(_config.get_list("portal_prefixes"))


# ---------------------------------------------------------------- the board repo, from stackshot's config

def config_file():
    env = os.environ.get("STACKSHOT_CONFIG") or os.environ.get("BOARD_MCP_CONFIG")
    if env:
        p = os.path.expanduser(env)
        return p if os.path.isfile(p) else ""
    cands = [os.path.join(os.path.dirname(_config.config_file()), "stackshot.toml"),
             os.path.join(_config.stackshot_dir(), "stackshot.toml")]
    cands += [os.path.expanduser(p) for p in _config.get_list("stackshot_toml_fallbacks")]
    return next((p for p in cands if os.path.isfile(p)), "")


def board_config():
    """→ ([`[github] issues_repo`, then each of `[github] issues_repo_aliases`], `[github] issues_repo_id`), the names normalized
    to owner/name and the id as a digit string ("" when absent or not a positive integer), when `[github] sync` is true and
    issues_repo is set; else ([], ""). The aliases are the repo's pre-move names (GitHub redirects them)."""
    p = config_file()
    if not p:
        return [], ""
    import tomllib
    with open(p, "rb") as f:
        gh = tomllib.load(f).get("github") or {}
    if not isinstance(gh, dict) or gh.get("sync") is not True:
        return [], ""
    main = norm_repo(str(gh.get("issues_repo") or ""))
    if not main:
        return [], ""
    rid = gh.get("issues_repo_id")
    rid = str(rid) if isinstance(rid, int) and not isinstance(rid, bool) and rid > 0 else ""
    aliases = gh.get("issues_repo_aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    out = [main]
    for a in aliases if isinstance(aliases, list) else []:
        n = norm_repo(str(a))
        if n and n not in out:
            out.append(n)
    return out, rid


def board_repos():
    return board_config()[0]


def named(repos, hit_repo):
    """How a refusal names the board repo: its current name, and the alias the call used when that was the way in."""
    return repos[0] if hit_repo == repos[0] else f"{repos[0]} (the call names {hit_repo}, its pre-move name)"


def norm_repo(s):
    """owner/name, lower case, from `owner/name`, `HOST/owner/name`, a github.com URL or a git remote; else ""."""
    s = (s or "").strip().strip("'\"").lower()
    if not s:
        return ""
    m = GH_URL.search(s)
    if m:
        owner, name = m.group(1), m.group(2)
    else:
        parts = [x for x in re.sub(r"^[a-z]+://", "", s).split("/") if x]
        if len(parts) == 3 and "." in parts[0]:
            parts = parts[1:]
        if len(parts) != 2:
            return ""
        owner, name = parts
    name = name[:-4] if name.endswith(".git") else name
    return f"{owner}/{name}" if owner and name else ""


def urls_in(tokens):
    return [norm_repo(m.group(0)) for t in tokens for m in GH_URL.finditer(t)]


_remote_cache = {}


def is_board_clone(path, repo):
    """Some remote of the git repository at `path` is the board repo (gh takes its base repo from the remotes)."""
    if not path:
        return False
    if path not in _remote_cache:
        import subprocess
        try:
            r = subprocess.run(["git", "-C", path, "remote", "-v"], capture_output=True, text=True, timeout=5)
            _remote_cache[path] = {norm_repo(ln.split()[1]) for ln in r.stdout.splitlines() if len(ln.split()) > 1} \
                if r.returncode == 0 else set()
        except Exception:
            _remote_cache[path] = set()
    return repo in _remote_cache[path]


# ---------------------------------------------------------------- the command, as shell words

def strip_heredocs(cmd):
    """Drop heredoc bodies (their text is data, not commands)."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        tags = re.findall(r"<<-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1", ln)
        i += 1
        for _, tag in tags:
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1
    return "\n".join(out)


def segments(cmd):
    """Simple commands, as word lists, split at ; & | ( ) and newlines."""
    cmd = strip_heredocs(cmd).replace("\\\n", " ").replace("\n", " ; ")
    import shlex
    try:
        lx = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        toks = list(lx)
    except ValueError:
        toks = []
        for part in re.split(r"[;&|()]+", cmd):
            toks += part.split() + [";"]
    segs, cur = [], []
    for t in toks:
        if t and all(c in "();<>|&" for c in t):
            if t in ("<", ">", ">>", "<<", "&>", ">&", "2>"):
                continue
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def base(word):
    w = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return w[:-4] if w.endswith(".exe") else w


def cd_target(cwd, arg):
    home = os.path.expanduser("~")
    if not arg or arg == "~":
        return home
    if arg == "-":
        return cwd
    arg = os.path.expanduser(arg)
    if cwd is None and not os.path.isabs(arg):
        return None
    return os.path.normpath(os.path.join(cwd or home, arg))


# ---------------------------------------------------------------- one gh invocation

def gh_flag(args, short, long_):
    """The value of -R/--repo style flags (also -RX, --repo=X, -R=X), last one wins; None when absent."""
    val = None
    for i, a in enumerate(args):
        if a in (short, long_):
            val = args[i + 1] if i + 1 < len(args) else ""
        elif a.startswith(long_ + "="):
            val = a[len(long_) + 1:]
        elif short and a.startswith(short) and len(a) > len(short) and not a.startswith("--"):
            val = a[len(short):].lstrip("=")
    return val


def positionals(args, value_flags):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in value_flags:
            skip = True
        elif a.startswith("-") and a != "-":
            continue
        else:
            out.append(a)
    return out


def check_gh(args, cwd, gh_repo, repo, rid=""):
    """args: the words after `gh`. Returns the refused command's short form, or ""."""
    pos = positionals(args, ITEM_VALUE_FLAGS)
    if not pos:
        return ""
    group = pos[0]
    verb = pos[1] if len(pos) > 1 else ""

    def base_is_board():
        if gh_repo:
            return norm_repo(gh_repo) == repo
        return is_board_clone(cwd, repo)

    if group == "api":
        rest = args[args.index("api") + 1:] if "api" in args else args[1:]
        p = positionals(rest, API_VALUE_FLAGS)
        endpoint = p[0] if p else ""
        method = (gh_flag(rest, "-X", "--method") or "").upper()
        fields = any(a in API_FIELD_FLAGS or a.startswith(("--field=", "--raw-field=", "--input="))
                     or re.match(r"^-[fF].", a) for a in rest)
        ep = re.sub(r"^https?://[^/]+/(api/v3/)?", "", endpoint, flags=re.I).lstrip("/")
        if ep.lower().startswith("graphql"):
            text = " ".join(rest)
            if not re.search(r"\bmutation\b", text):
                return ""
            owner, name = repo.split("/")
            names = (repo in text.lower()
                     or (re.search(r"(?<![\w.-])%s(?![\w.-])" % re.escape(name), text, re.I)
                         and re.search(r"(?<![\w.-])%s(?![\w.-])" % re.escape(owner), text, re.I))
                     or (("{repo}" in text or "{owner}" in text) and base_is_board()))
            return "gh api graphql (mutation)" if names else ""
        if method in WRITE_METHODS or (not method and fields):
            parts = ep.split("?")[0].split("/")
            # compared as ASCII digit strings, never through int(): isdigit() also takes digits such as
            # "²" that int() rejects, and int() raises on a very long digit string
            if rid and len(parts) >= 2 and parts[0].lower() == "repositories" \
                    and parts[1].isascii() and parts[1].isdigit() \
                    and parts[1].lstrip("0") == rid.lstrip("0"):
                return f"gh api {method or 'POST'} {endpoint}"      # the repo by its numeric id
            if len(parts) >= 3 and parts[0].lower() == "repos":
                o, n = parts[1].lower(), parts[2].lower()
                hit = (f"{o}/{n}" == repo) or (o in ("{owner}", ":owner") and n in ("{repo}", ":repo") and base_is_board())
                if hit:
                    return f"gh api {method or 'POST'} {endpoint}"
        return ""

    writes = {"issue": ISSUE_WRITES, "label": LABEL_WRITES, "pr": PR_WRITES}.get(group)
    if not writes or verb not in writes:
        return ""
    flag = gh_flag(args, "-R", "--repo")
    # the selector (an issue or PR number, or its URL) is the first word after the verb; a URL there names the repo.
    # `label clone` takes the source repo there, which it only reads
    sel = pos[2] if len(pos) > 2 and not (group == "label" and verb == "clone") else ""
    urls = urls_in([sel]) if sel else []
    if urls:
        hit = repo in urls
    elif flag is not None:
        hit = norm_repo(flag) == repo
    else:
        hit = base_is_board()
    if group == "issue" and verb == "transfer" and len(pos) > 3 and norm_repo(pos[3]) == repo:
        hit = True                                  # an issue moved into the board repo
    return f"gh {group} {verb}" if hit else ""


# ---------------------------------------------------------------- the whole command

def scan(cmd, cwd, repo, env_gh_repo, depth=0, rid=""):
    if depth > 3:
        return ""
    gh_repo = env_gh_repo
    for seg in segments(cmd):
        words = list(seg)
        local_repo = gh_repo
        while words and ASSIGN.match(words[0]):
            k, v = ASSIGN.match(words[0]).groups()
            if k == "GH_REPO":
                local_repo = v
            words.pop(0)
        if not words:
            gh_repo = local_repo                    # a bare assignment holds for the rest of the command
            continue
        head = base(words[0])
        if head in ("cd", "pushd"):
            cwd = cd_target(cwd, words[1] if len(words) > 1 else "")
            continue
        if head == "export":
            for w in words[1:]:
                m = ASSIGN.match(w)
                if m and m.group(1) == "GH_REPO":
                    gh_repo = m.group(2)
            continue
        if head in SHELLS:
            for i, w in enumerate(words[1:], 1):
                if re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", w) and i + 1 < len(words):
                    hit = scan(words[i + 1], cwd, repo, local_repo, depth + 1, rid)
                    if hit:
                        return hit
                    break
            continue
        if head == "eval":
            hit = scan(" ".join(words[1:]), cwd, repo, local_repo, depth + 1, rid)
            if hit:
                return hit
            continue
        if head == "ssh":
            i = 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in SSH_VALUE_FLAGS else 1
            if i + 1 < len(words):
                hit = scan(" ".join(words[i + 1:]), None, repo, None, depth + 1, rid)
                if hit:
                    return hit
            continue
        if head in WRAPPERS:
            rest = words[1:]
            idx = next((j for j, w in enumerate(rest) if base(w) == "gh"), None)
            if idx is None:
                continue
            for w in rest[:idx]:
                m = ASSIGN.match(w)
                if m and m.group(1) == "GH_REPO":
                    local_repo = m.group(2)
            words = rest[idx:]
            head = "gh"
        if head == "gh":
            hit = check_gh(words[1:], cwd, local_repo, repo, rid)
            if hit:
                return hit
    return ""


# ---------------------------------------------------------------- the GitHub connector, direct or through the portal

def mcp_worth_reading(inp):
    """The fast path: a call that names no repo and carries no Code Mode script cannot write the board."""
    if not isinstance(inp, dict):
        return False
    if "codemode" in str(inp.get("code") or ""):
        return True
    return any(isinstance(k, str) and k.lower().endswith(("owner", "repo")) for k in inp)


def is_github_name(name):
    return bool(name) and bool(GITHUB_WORD.search(str(name)))


def is_github_entry(entry):
    """A catalog server is GitHub's: its upstream host is GitHub's, or its name or connector name says GitHub."""
    if not isinstance(entry, dict):
        return False
    url = str(entry.get("url") or "").strip()
    if url:
        from urllib.parse import urlsplit
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in GITHUB_HOSTS):
            return True
    return is_github_name(entry.get("name")) or is_github_name(entry.get("connector"))


def is_write(tool, entry=None):
    """The catalog's readOnlyHint when this server's tool list carries the hints; else the name rule (module docstring)."""
    tools = (entry or {}).get("tools") if isinstance(entry, dict) else None
    if isinstance(tools, dict) and tool in tools and any(isinstance(t, dict) and t.get("read_only") for t in tools.values()):
        return not (tools.get(tool) or {}).get("read_only")
    t = str(tool or "").lower()
    return not (t.startswith(READ_PREFIXES) or t.endswith(READ_SUFFIXES))


def aims_at_board(args, clean, repo):
    """"yes" when the call's owner/repo is the board repo, "unsure" when the script computes it (or its arguments carry a
    spread or a computed key) and no literal part rules the board out, else ""."""
    owner_b, name_b = repo.split("/", 1)
    args = args if isinstance(args, dict) else {}
    keys = {str(k).lower(): k for k in args}
    stems = {k[:-5] for k in keys if k.endswith("owner")} | {k[:-4] for k in keys if k.endswith("repo")}

    def part(v, want):                          # True matches, False rules it out, None cannot be told (computed)
        if v is MISSING:
            return False
        if isinstance(v, str):
            return v.strip().lower() == want
        return None if v is not None and not isinstance(v, (int, float, bool, list, dict)) else False

    unsure = False
    for s in stems:
        o = args.get(keys[s + "owner"], MISSING) if s + "owner" in keys else MISSING
        r = args.get(keys[s + "repo"], MISSING) if s + "repo" in keys else MISSING
        if o is MISSING and isinstance(r, str) and "/" in r:
            if norm_repo(r) == repo:
                return "yes"
            continue
        po, pr = part(o, owner_b), part(r, name_b)
        if po is False or pr is False:
            continue
        if po and pr:
            return "yes"
        unsure = True
    if unsure or not clean:
        return "unsure"
    return ""


def load_connectors():
    return _config.import_stackshot("connectors", "board-repo-guard")


def is_portal_execute(tool_name, cat, conn):
    """The tool is the portal's Code Mode execute, on any of the portal's names: the catalog's (the claude.ai connector and its
    aliases, e.g. the local `mcp-portal`), the typed fallbacks for a box without the catalog, or any other server's tool of
    that name."""
    pres = list(portal_prefixes())
    if cat:
        pres += [p for _, p in conn.names_of(cat.get("portal")) if p]
    return any(tool_name == p + EXECUTE for p in pres) or tool_name.rsplit("__", 1)[-1] == EXECUTE


def check_portal_script(inp, cat, conn, repo):
    """→ (the refused call's short form, why) or ("", ""). Reads the script's `codemode.<entry>_<tool>(…)` calls with stackshot's
    reader, which splits an entry off by the catalog's ids: the GitHub entry's id is added to them when the catalog does not carry it
    yet, so this works before the first portal sync that adds it. A call is GitHub's when its entry is that id or a catalog entry
    that reads as GitHub's."""
    code = str((inp or {}).get("code") or "")
    servers = dict((cat or {}).get("servers") or {})
    gh = github_entry()
    scat = {**(cat or {}), "servers": {**servers, gh: servers.get(gh) or {}}}
    calls, problems = conn.read_script(code, scat)
    unsure_why = (" (its owner/repo cannot be read from the call, so it cannot be told apart from the board repo; name the "
                  "owner and repo literally in the call, owner: 'x', repo: 'y', not through a variable or a spread)")
    for c in calls:
        entry = servers.get(c.server) if c.server else None
        entry = entry if isinstance(entry, dict) and entry else None
        if not c.server or not (c.server == gh or is_github_entry(entry)) or not is_write(c.tool, entry):
            continue
        hit = aims_at_board(c.args, c.clean, repo)
        if hit:
            return f"codemode.{c.server}_{c.tool}", ("" if hit == "yes" else unsure_why)
    # a script that reaches `codemode` in a way the reader cannot follow (an alias, codemode[...], eval) hides its calls: with
    # GitHub and the board repo's name both in its text, it is refused with the way to retry
    low = code.lower()
    if (any(p.startswith(("codemode used other than", "codemode inside", "eval/Function")) for p in problems)
            and "github" in low and repo.split("/", 1)[1] in low):
        return "a portal script that reaches GitHub", (
            " (the script uses codemode in a way that hides its calls: write plain codemode.github_<tool>({owner: 'x', "
            "repo: 'y'}) calls, one per line, with the owner and repo literal)")
    return "", ""


def check_mcp(tool_name, inp, mcp_server, repo):
    """→ (the refused call's short form, why) or ("", "")."""
    conn = load_connectors()
    if conn is None:
        return "", ""                               # the resolver has not landed on this box
    cat = conn.load_catalog()
    if is_portal_execute(tool_name, cat, conn):
        return check_portal_script(inp, cat, conn, repo)
    servers = (cat or {}).get("servers") or {}
    if cat:                                         # no catalog: nothing to resolve, the native route below reads the tool name
        res = conn.resolve(tool_name, inp, mcp_server, catalog=cat)
        if res is None:
            return "", ""
        if res.route == "portal":                   # the portal's other tools (search, list, toggle): no repo write
            return "", ""
        if res.route == "direct":                   # GitHub as a catalog server: a portal entry joined with its connector
            c = res.calls[0] if res.calls else None
            entry = servers.get(c.server) if c else None
            if c and is_github_entry(entry) and is_write(c.tool, entry) and aims_at_board(c.args, True, repo) == "yes":
                return tool_name, ""
            return "", ""
    # not a server of the catalog: the native connector, known by the name the CLI gives it
    srv = str((mcp_server or {}).get("name") or "") if isinstance(mcp_server, dict) else ""
    if srv:
        pre = conn.prefix_of(srv)
        if not tool_name.startswith(pre) or not is_github_name(srv):
            return "", ""
        tool = tool_name[len(pre):]
    else:
        rest = tool_name[len("mcp__"):]
        if "__" not in rest:
            return "", ""
        seg, tool = rest.rsplit("__", 1)
        if not is_github_name(seg):
            return "", ""
    if is_write(tool) and aims_at_board(inp, True, repo) == "yes":
        return tool_name, ""
    return "", ""


def deny(reason):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                        "permissionDecisionReason": reason}}, ensure_ascii=False))
    return 0


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(ev, dict):
        return 0
    tool = str(ev.get("tool_name") or "")
    if tool.startswith("mcp__"):
        inp = ev.get("tool_input") or {}
        if not mcp_worth_reading(inp):
            return 0                                # the fast path: no repo named, no script, nothing read
        repos = board_repos()
        for repo in repos:                          # the repo, then each pre-move name, each checked like the repo
            hit, why = check_mcp(tool, inp, ev.get("mcp_server"), repo)
            if hit:
                return deny(REFUSAL_MCP.format(what=hit, repo=named(repos, repo), why=why))
        return 0
    if tool != "Bash":
        return 0
    cmd = str((ev.get("tool_input") or {}).get("command") or "")
    if "gh" not in cmd or not GH_WORD.search(cmd):
        return 0                                    # the fast path: no gh in the command, nothing read
    repos, rid = board_config()
    cwd = str(ev.get("cwd") or "") or os.getcwd()
    for i, repo in enumerate(repos):                # the id is the repo's own: checked once, with its configured name
        hit = scan(cmd, cwd, repo, os.environ.get("GH_REPO") or None, rid=rid if i == 0 else "")
        if hit:
            return deny(REFUSAL.format(what=hit, repo=named(repos, repo)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
