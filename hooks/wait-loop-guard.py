#!/usr/bin/env python3
"""wait-loop-guard.py — PreToolUse hook (matcher `Bash`): no shell wait loop that can never end.

The case behind it: a background agent starts, through Bash with run_in_background,
`until ! pgrep -f "bash tests/run.sh" >/dev/null; do sleep 5; done; tail ...`. `pgrep -f` matches the full command line of every
process, and the shell that runs the Bash call has the whole command, pattern text included, on its own command line
(`bash -c '<whole command>'`). The loop matches itself and spins for hours, can block a session restart, and the harness may not
even list it as a task. This hook refuses, before the command runs:

  1. SELF-MATCH. A `pgrep`/`pkill` (`pidof`) whose pattern matches the shell that runs this very command: with -f/--full
     (-af, -fa, -fx …) the pattern is tested as a regex against the whole command text; without -f the shell's name
     (`bash`) is the text. It is refused when its result is used for control: inside a `while`/`until` loop, in an
     `if`/`elif` test, after `!`, before `&&`/`||`, in `$(...)`/backticks (not as an echo/printf argument), piped into xargs,
     or with -q/-c/--count or stdout to /dev/null. A plain listing (`pgrep -af foo`, `pgrep -f foo | head`) passes. A `pkill`
     that matches its own shell kills that shell, so it is refused wherever it sits. The bracket trick (`[b]ash tests/run.sh`)
     does not match its own text and passes. Never bypassed.
  2. UNBOUNDED LOOP. A `while`/`until` loop (shell syntax, found by a tokenizer: quoted strings, heredoc bodies and echo/grep
     arguments are not code; `bash -c '…'`, `eval` and `$(…)` are looked into) with a `sleep` in it and nothing that ends it:
     no `timeout <n>` around it, no SECONDS / `date +%s` / EPOCHSECONDS deadline, no counter (arithmetic plus a comparison),
     no seq / {1..N} range. A loop on a finite input (`while read …`), or on a PID (`kill -0 "$pid"`, /proc/<pid>, `ps -p`),
     ends by itself and passes. `for` loops are not checked. `# wait-loop-guard: ok` anywhere in the command bypasses this rule.

When unsure it allows: a false refusal stalls an unattended agent. Bad or unreadable input exits 0 silently (fails open). It
never answers `ask`; a refusal is a `deny` whose reason names the fix. Stdlib only."""
import json
import os
import re
import sys

BYPASS = re.compile(r"#\s*wait-loop-guard:\s*ok\b")
FAST = re.compile(r"\b(pgrep|pkill|pidof|while|until)\b")

TRAILER = ("A background Bash is not always listed by TaskStop and then blocks a session recycle, "
           "so every wait loop must be bounded.")

SELF_FIX = ("Fix: bracket one character of the pattern so it no longer matches its own text "
            "(`pgrep -f '[b]ash tests/run.sh'`), or better wait on a PID and never search by name: "
            "`while kill -0 \"$pid\" 2>/dev/null; do sleep 2; done` inside `timeout 600 bash -c '…'`, "
            "`tail --pid=$pid -f /dev/null`, or start the work itself with run_in_background true and wait for its "
            "completion notice.")

LOOP_FIX = ("Fix: wrap the wait in a timeout (`timeout 600 bash -c 'until …; do sleep 5; done'`) or give the loop a deadline "
            "(`end=$((SECONDS+600)); until … || [ $SECONDS -ge $end ]; do sleep 5; done`). A wait loop on a PID needs no name "
            "search (`while kill -0 \"$pid\" 2>/dev/null; do sleep 2; done`, which ends with the process). If the loop truly "
            "ends by itself, add the comment `# wait-loop-guard: ok` to the command.")

# ------------------------------------------------------------------ tokenizer


class Tok:
    __slots__ = ("kind", "val", "start", "end", "quoted", "expands", "subs")

    def __init__(self, kind, val, start, end, quoted=False, expands=False, subs=None):
        self.kind = kind            # w (word), op, redir
        self.val = val
        self.start = start
        self.end = end
        self.quoted = quoted
        self.expands = expands
        self.subs = subs or []      # texts of $(...) / `...` / <(...) inside the word


REDIR = re.compile(r"\d*(?:&>>|&>|<<<|<<-|<<|>>|>\||>&|<&|<>|>|<)")
OPS2 = ("&&", "||", ";;", ";&", "|&")


def scan_paren(s, i):
    """s[i] is '('; the index of its matching ')', or -1."""
    depth, j, n = 0, i, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = s.find("'", j + 1)
            if k < 0:
                return -1
            j = k + 1
            continue
        if c == '"':
            j = skip_dq(s, j)
            if j < 0:
                return -1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def skip_dq(s, j):
    """s[j] is '"'; the index after its closing quote, or -1."""
    j += 1
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        if c == "$" and s.startswith("$(", j):
            e = scan_paren(s, j + 1)
            if e < 0:
                return -1
            j = e + 1
            continue
        j += 1
    return -1


def find_backtick(s, j):
    n = len(s)
    while j < n:
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == "`":
            return j
        j += 1
    return -1


def match_brace(s, j):
    """s[j] is '{' of ${…}; index of the closing '}' or -1."""
    depth, n = 0, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def read_word(s, i):
    n = len(s)
    j = i
    buf = []
    quoted = expands = False
    subs = []
    while j < n:
        c = s[j]
        if c in " \t\r\n;&|()<>":
            break
        if c == "\\":
            quoted = True
            if j + 1 < n:
                if s[j + 1] != "\n":
                    buf.append(s[j + 1])
                j += 2
            else:
                j += 1
            continue
        if c == "'":
            quoted = True
            k = s.find("'", j + 1)
            if k < 0:
                buf.append(s[j + 1:])
                j = n
                break
            buf.append(s[j + 1:k])
            j = k + 1
            continue
        if c == '"':
            quoted = True
            j += 1
            while j < n and s[j] != '"':
                d = s[j]
                if d == "\\" and j + 1 < n:
                    if s[j + 1] in '"\\$`':
                        buf.append(s[j + 1])
                    elif s[j + 1] != "\n":
                        buf.append("\\" + s[j + 1])
                    j += 2
                    continue
                if d == "$" and s.startswith("$(", j):
                    e = scan_paren(s, j + 1)
                    if e < 0:
                        buf.append(s[j:])
                        j = n
                        break
                    if not s.startswith("$((", j):
                        subs.append(s[j + 2:e])
                    buf.append(s[j:e + 1])
                    expands = True
                    j = e + 1
                    continue
                if d == "`":
                    e = find_backtick(s, j + 1)
                    if e < 0:
                        buf.append(s[j:])
                        j = n
                        break
                    subs.append(s[j + 1:e])
                    buf.append(s[j:e + 1])
                    expands = True
                    j = e + 1
                    continue
                if d == "$" and j + 1 < n and (s[j + 1].isalnum() or s[j + 1] in "_{@*#?!$"):
                    expands = True
                buf.append(d)
                j += 1
            j += 1
            continue
        if c == "$":
            nx = s[j + 1] if j + 1 < n else ""
            if nx == "(":
                e = scan_paren(s, j + 1)
                if e < 0:
                    buf.append(s[j:])
                    j = n
                    break
                if not s.startswith("$((", j):
                    subs.append(s[j + 2:e])
                buf.append(s[j:e + 1])
                expands = True
                j = e + 1
                continue
            if nx == "{":
                e = match_brace(s, j + 1)
                if e < 0:
                    buf.append(s[j:])
                    j = n
                    break
                buf.append(s[j:e + 1])
                expands = True
                j = e + 1
                continue
            if nx == "'":
                quoted = True
                k = j + 2
                while k < n and s[k] != "'":
                    k += 2 if s[k] == "\\" else 1
                buf.append(s[j + 2:k])
                j = k + 1
                continue
            if nx and (nx.isalnum() or nx in "_@*#?!$-"):
                expands = True
            buf.append(c)
            j += 1
            continue
        if c == "`":
            e = find_backtick(s, j + 1)
            if e < 0:
                buf.append(s[j:])
                j = n
                break
            subs.append(s[j + 1:e])
            buf.append(s[j:e + 1])
            expands = True
            j = e + 1
            continue
        buf.append(c)
        j += 1
    return "".join(buf), j, quoted, expands, subs


def tokenize(s):
    toks = []
    i, n = 0, len(s)
    pending = []                # heredocs waiting for the next newline: (delimiter, strip_tabs)
    expect_heredoc = None
    while i < n:
        c = s[i]
        if c in " \t\r":
            i += 1
            continue
        if c == "\\" and s.startswith("\\\n", i):
            i += 2
            continue
        if c == "\n":
            toks.append(Tok("op", ";", i, i + 1))
            i += 1
            for delim, strip in pending:
                while i < n:
                    e = s.find("\n", i)
                    e = n if e < 0 else e
                    line = s[i:e]
                    i = e + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            pending = []
            continue
        if c == "#":
            e = s.find("\n", i)
            i = n if e < 0 else e
            continue
        m = REDIR.match(s, i)
        if m:
            op = m.group(0)
            if op in ("<", ">") and s.startswith("(", m.end()):          # process substitution <(…) / >(…)
                e = scan_paren(s, m.end())
                if e > 0:
                    toks.append(Tok("w", s[i:e + 1], i, e + 1, expands=True, subs=[s[m.end() + 1:e]]))
                    expect_heredoc = None
                    i = e + 1
                    continue
            toks.append(Tok("redir", op, i, m.end()))
            if op.endswith("<<") or op.endswith("<<-"):
                expect_heredoc = op.endswith("-")
            i = m.end()
            continue
        if c == "(" and s.startswith("((", i):                           # arithmetic command (( … ))
            depth, j = 0, i
            while j < n:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < n and s[j - 1] == ")" and depth == 0:
                toks.append(Tok("w", s[i:j + 1], i, j + 1, quoted=True))
                i = j + 1
                continue
        if c in ";&|()":
            two = s[i:i + 2]
            op = two if two in OPS2 else c
            toks.append(Tok("op", op, i, i + len(op)))
            i += len(op)
            continue
        val, j, quoted, expands, subs = read_word(s, i)
        if j == i:                      # nothing consumed: never loop forever
            j = i + 1
        toks.append(Tok("w", val, i, j, quoted, expands, subs))
        if expect_heredoc is not None:
            pending.append((val, expect_heredoc))
            expect_heredoc = None
        i = j
    return toks


# ------------------------------------------------------------------ analysis

ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
WRAP_VAL = {                                   # wrapper -> options that take a value
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}, "env": {"-u", "-C", "-S"}, "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"}, "timeout": {"-s", "-k"}, "setsid": set(), "nohup": set(),
    "command": set(), "exec": {"-a"}, "builtin": set(), "time": {"-f", "-o"},
}

DEADLINE = re.compile(r"\bSECONDS\b|\bEPOCHSECONDS\b|\bEPOCHREALTIME\b|\bdate\b[^;|&\n]*%s|%\(%s\)T")
RANGE = re.compile(r"\bseq\b|\{\d+\.\.\d+\}")
ARITH = re.compile(r"\$\(\(|\(\(|\blet\b|\bexpr\b|\+\+|\+=")
COMPARE = re.compile(r"(?<![\w-])-(?:lt|le|gt|ge|eq|ne)(?![\w-])|==|<=|>=|\(\([^)]*[<>]")
FINITE_COND = re.compile(r"^\s*(?:while|until)\s+!?\s*(?:IFS=\S*\s+)?(?:read|mapfile|readarray|getopts)\b"
                         r"|\bkill\s+-0\b|/proc/|\bps\s+(?:-\w*p\b|--pid\b)|\bwait\b")


class Frame:
    __slots__ = ("kind", "phase", "start", "do_start", "sleep")

    def __init__(self, kind, phase, start):
        self.kind, self.phase, self.start, self.do_start, self.sleep = kind, phase, start, None, False


class Seg:
    __slots__ = ("words", "redirs", "prev_op", "next_op")

    def __init__(self):
        self.words, self.redirs, self.prev_op, self.next_op = [], [], None, None


def unwrap(words):
    """Strip sudo/env/timeout/nohup… from the front of a simple command: (rest, wrapped_in_timeout)."""
    bounded, i = False, 0
    while i < len(words):
        v = words[i].val
        if ASSIGN.match(v):
            i += 1
            continue
        b = os.path.basename(v)
        if b in WRAP_VAL and not words[i].quoted:
            i += 1
            vals = WRAP_VAL[b]
            while i < len(words) and words[i].val.startswith("-") and len(words[i].val) > 1:
                o = words[i].val
                i += 1
                if o in vals and i < len(words):
                    i += 1
            if b == "timeout":
                bounded = True
                if i < len(words):
                    i += 1                  # the duration
            continue
        break
    return words[i:], bounded


POSIX = {"[:alnum:]": "a-zA-Z0-9", "[:alpha:]": "a-zA-Z", "[:digit:]": "0-9", "[:space:]": r"\s", "[:upper:]": "A-Z",
         "[:lower:]": "a-z", "[:blank:]": r" \t", "[:punct:]": r"!-/:-@\[-`{-~"}
PG_VAL_SHORT = set("dgGOPstuUFr")
PG_VAL_LONG = {"--delimiter", "--pgroup", "--group", "--older", "--parent", "--session", "--terminal", "--euid", "--uid",
               "--pidfile", "--runstates", "--ns", "--nslist", "--signal"}


def parse_pgrep(name, args):
    """args: Tok list after the command name. -> dict(full, exact, icase, quiet, pattern, unknown) or None."""
    o = {"full": False, "exact": False, "icase": False, "quiet": False, "pattern": None, "unknown": False, "scoped": False}
    i, only_pos = 0, False
    while i < len(args):                    # GNU getopt permutes: options may follow the pattern
        a = args[i].val
        if not only_pos and a == "--":
            only_pos = True
            i += 1
            continue
        if not only_pos and a.startswith("--"):
            opt = a.split("=", 1)[0]
            if opt == "--full":
                o["full"] = True
            elif opt == "--exact":
                o["exact"] = True
            elif opt == "--ignore-case":
                o["icase"] = True
            elif opt == "--count":
                o["quiet"] = True
            elif opt in ("--parent", "--session", "--pgroup", "--terminal", "--pidfile"):
                o["scoped"] = True          # narrowed to a parent, session, group, tty or pidfile: the shell is not in it
            if opt in PG_VAL_LONG and "=" not in a:
                i += 1
            i += 1
            continue
        if not only_pos and a.startswith("-") and len(a) > 1:
            if name == "pkill" and re.fullmatch(r"-(?:\d+|[A-Z][A-Z0-9]+)", a):      # pkill -9 / -TERM / -SIGKILL
                i += 1
                continue
            k = 1
            while k < len(a):
                ch = a[k]
                if ch in PG_VAL_SHORT:
                    if ch in "PstgF":
                        o["scoped"] = True
                    if k + 1 >= len(a):
                        i += 1                  # the value is the next argument
                    break
                if ch == "f":
                    o["full"] = True
                elif ch == "x":
                    o["exact"] = True
                elif ch == "i":
                    o["icase"] = True
                elif ch in "cq":
                    o["quiet"] = True
                k += 1
            i += 1
            continue
        if o["pattern"] is None:
            o["pattern"] = a
            o["unknown"] = args[i].expands
        i += 1
    return o


def to_regex(pattern, icase):
    p = pattern
    for k, v in POSIX.items():
        p = p.replace(k, v)
    flags = re.I if icase else 0
    try:
        return re.compile(p, flags)
    except re.error:
        return re.compile(re.escape(pattern), flags)


ANCHOR_START = re.compile(r"(?:^|[|(])\^")
ANCHOR_END = re.compile(r"(?<!\\)\$(?:$|[|)])")


def matches_shell(o, full_text):
    """Does this pgrep/pkill pattern match the shell that runs full_text?"""
    pat = o["pattern"]
    if pat is None or o["unknown"] or o["scoped"] or pat == "":
        return False
    if o["full"]:
        if o["exact"]:
            return False                # -fx needs the whole command line to equal the pattern
        if ANCHOR_START.search(pat) or ANCHOR_END.search(pat):
            return False                # anchored on the shell's own prefix/suffix, which this text does not show
        return bool(to_regex(pat, o["icase"]).search(full_text))
    rx = to_regex(pat, o["icase"])      # the process name: the shell is `bash`
    return bool(rx.fullmatch("bash") if o["exact"] else rx.search("bash"))


class Analyzer:
    def __init__(self, full, any_loop=False):
        self.any_loop = any_loop       # front-watch-gate.py: report every while/until/for loop with a sleep, bounded or not
        self.full = full
        self.bypass = bool(BYPASS.search(full))
        self.global_deadline = bool(DEADLINE.search(full))
        self.findings = []

    def add(self, kind, detail):
        if (kind, detail) not in self.findings:
            self.findings.append((kind, detail))

    def run(self):
        self.analyze(self.full, bounded=False, capture=False, depth=0)
        return self.findings

    # ---- one script text
    def analyze(self, text, bounded, capture, depth):
        if depth > 6:
            return
        toks = tokenize(text)
        items, cur = [], Seg()
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.kind == "op":
                if cur.words or cur.redirs:
                    items.append(cur)
                items.append(t.val)
                cur = Seg()
            elif t.kind == "redir":
                target = None
                if i + 1 < len(toks) and toks[i + 1].kind == "w":
                    target = toks[i + 1]
                    i += 1
                cur.redirs.append((t.val, target))
            else:
                cur.words.append(t)
            i += 1
        if cur.words or cur.redirs:
            items.append(cur)
        segs, last_op = [], None
        for idx, it in enumerate(items):
            if isinstance(it, str):
                if it not in ("(", ")"):
                    last_op = it
                continue
            it.prev_op = last_op
            nxt = None
            for later in items[idx + 1:]:
                if isinstance(later, str) and later not in ("(", ")"):
                    nxt = later
                    break
            it.next_op = nxt
            segs.append(it)
        frames = []
        for k, seg in enumerate(segs):
            nxt_seg = segs[k + 1] if k + 1 < len(segs) else None
            self.process_seg(text, seg, nxt_seg, frames, bounded, capture, depth)

    def process_seg(self, text, seg, nxt_seg, frames, bounded, capture, depth):
        words = seg.words
        pos, neg = 0, False
        while pos < len(words):
            w = words[pos]
            if w.quoted or w.expands:
                break
            v = w.val
            if v in ("while", "until"):
                frames.append(Frame(v, "cond", w.start))
            elif v in ("for", "select"):
                frames.append(Frame("for", "cond", w.start))
                pos = len(words)
                break
            elif v == "case":
                frames.append(Frame("case", "cond", w.start))
                pos = len(words)
                break
            elif v == "if":
                frames.append(Frame("if", "cond", w.start))
            elif v == "elif":
                for f in reversed(frames):
                    if f.kind == "if":
                        f.phase = "cond"
                        break
            elif v == "do":
                for f in reversed(frames):
                    if f.kind in ("while", "until", "for") and f.phase == "cond":
                        f.phase, f.do_start = "body", w.start
                        break
            elif v in ("then", "else"):
                for f in reversed(frames):
                    if f.kind == "if":
                        f.phase = "body"
                        break
            elif v == "done":
                self.close_loop(text, frames, w.end, bounded)
            elif v == "fi":
                self.pop_kind(frames, ("if",))
            elif v == "esac":
                self.pop_kind(frames, ("case",))
            elif v == "!":
                neg = True
            elif v in ("{", "}", "time", "coproc", "function"):
                pass
            else:
                break
            pos += 1
        cmd = words[pos:]
        rest, wrapped = unwrap(cmd) if cmd else ([], False)
        # a $(...) shown by echo/printf is a listing; anywhere else its result is used
        show = bool(rest) and os.path.basename(rest[0].val) in ("echo", "printf")
        for w in words:                                 # $(...) / `...` anywhere in the segment
            for sub in w.subs:
                self.analyze(sub, bounded, capture=not show, depth=depth + 1)
        for _op, target in seg.redirs:
            if target is not None:
                for sub in target.subs:
                    self.analyze(sub, bounded, capture=True, depth=depth + 1)
        if not rest:
            return
        if not rest:
            return
        bnd = bounded or wrapped
        name = os.path.basename(rest[0].val)
        if rest[0].expands or ASSIGN.match(rest[0].val):
            return
        if name in ("sleep", "usleep"):
            for f in frames:
                if f.kind in ("while", "until") or (self.any_loop and f.kind == "for"):
                    f.sleep = True
        elif name in SHELLS:
            script = self.shell_script(rest[1:])
            if script is not None:
                self.analyze(script, bnd, False, depth + 1)
        elif name == "eval":
            self.analyze(" ".join(w.val for w in rest[1:]), bnd, False, depth + 1)
        elif name in ("pgrep", "pkill"):
            self.check_pgrep(name, rest, seg, nxt_seg, frames, neg, capture)
        elif name == "pidof":
            self.check_pidof(rest, seg, frames, neg, capture)

    @staticmethod
    def shell_script(args):
        for k, a in enumerate(args):
            v = a.val
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", v):
                for b in args[k + 1:]:
                    if not b.val.startswith("-"):
                        return b.val
                return None
            if not v.startswith("-"):
                return None
        return None

    @staticmethod
    def pop_kind(frames, kinds):
        for k in range(len(frames) - 1, -1, -1):
            if frames[k].kind in kinds:
                del frames[k:]
                return

    def close_loop(self, text, frames, end, bounded):
        for k in range(len(frames) - 1, -1, -1):
            f = frames[k]
            if f.kind in ("while", "until", "for"):
                del frames[k:]
                if f.sleep and (self.any_loop or (f.kind != "for" and not bounded and not self.bypass)):
                    body = text[f.start:end]
                    cond = text[f.start:f.do_start] if f.do_start else body
                    if self.any_loop or self.unbounded(body, cond):
                        self.add("loop", re.sub(r"\s+", " ", body)[:140])
                return

    def unbounded(self, body, cond):
        if self.global_deadline or DEADLINE.search(body) or RANGE.search(body):
            return False
        if ARITH.search(body) and (COMPARE.search(body) or re.search(r"\b(?:break|exit|return)\b", body)):
            return False
        if FINITE_COND.search(cond):
            return False
        if re.search(r"\bdate\b", cond) and COMPARE.search(cond):        # a wait for a time of day ends by itself
            return False
        return True

    # ---- pgrep / pkill / pidof
    def control(self, seg, frames, neg, capture, quiet):
        if neg or quiet or capture:
            return True
        if any(f.kind in ("while", "until") for f in frames) or any(f.kind == "if" and f.phase == "cond" for f in frames):
            return True
        if seg.next_op in ("&&", "||"):
            return True
        for op, target in seg.redirs:
            if target is not None and target.val == "/dev/null" and re.fullmatch(r"1?>>?|&>>?|>\|", op):
                return True
        return False

    def check_pgrep(self, name, rest, seg, nxt_seg, frames, neg, capture):
        o = parse_pgrep(name, rest[1:])
        if not matches_shell(o, self.full):
            return
        what = "pkill" if name == "pkill" else "pgrep"
        shown = " ".join(["-f" if o["full"] else "(name)", repr(o["pattern"])])
        if what == "pkill":
            self.add("self", (what, shown, o["full"]))
            return
        piped_xargs = (seg.next_op == "|" and nxt_seg is not None and nxt_seg.words
                       and os.path.basename(nxt_seg.words[0].val) in ("xargs", "kill"))
        if self.control(seg, frames, neg, capture, o["quiet"]) or piped_xargs:
            self.add("self", (what, shown, o["full"]))

    def check_pidof(self, rest, seg, frames, neg, capture):
        names, i = [], 1
        while i < len(rest):
            a = rest[i].val
            if a in ("-o", "-S"):
                i += 2
                continue
            if not a.startswith("-"):
                names.append(os.path.basename(a))
            i += 1
        if "bash" in names and self.control(seg, frames, neg, capture, False):
            self.add("self", ("pidof", "bash", False))


# ------------------------------------------------------------------ hook


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 0


def reason_for(findings):
    parts = ["wait-loop-guard: this command can start a wait that never ends."]
    for kind, detail in findings:
        if kind == "self":
            what, shown, full = detail
            if what == "pkill":
                parts.append(f"`pkill {shown}` matches the shell that runs this very command (the pattern text is on that shell's "
                             "own command line), so it would kill its own shell.")
            elif full:
                parts.append(f"`{what} {shown}` matches the shell that runs this very command (the pattern text is on that "
                             "shell's own command line), so it is always true and a loop on it never ends.")
            else:
                parts.append(f"`{what} {shown}` matches the shell that runs this very command (its name is bash), "
                             "so it is always true.")
        else:
            parts.append(f"Unbounded wait loop with no timeout, deadline or counter: `{detail}`")
    if any(k == "self" for k, _ in findings):
        parts.append(SELF_FIX)
    if any(k == "loop" for k, _ in findings):
        parts.append(LOOP_FIX)
    parts.append(TRAILER)
    return " ".join(parts)


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(ev, dict):
        return 0
    tool = ev.get("tool_name")
    if tool is not None and tool != "Bash":
        return 0
    inp = ev.get("tool_input")
    cmd = inp.get("command") if isinstance(inp, dict) else None
    if not isinstance(cmd, str) or not FAST.search(cmd):
        return 0                                    # the fast path: nothing to read
    findings = Analyzer(cmd).run()
    if not findings:
        return 0
    # rule 1 is never bypassed; rule 2's bypass is applied inside the analyzer
    return deny(reason_for(findings))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
