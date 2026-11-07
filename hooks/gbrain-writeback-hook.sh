#!/bin/bash
# gbrain-writeback-hook.sh - the Stop hook that feeds gbrain's ambient memory write-back.
#
# What it does: reads the Stop hook JSON from stdin and hands it, detached, to `gbrain hook stop`, then exits 0 at once. gbrain's stop
# event makes no model call itself: it gates the turn (the gbrain setting memory.auto_writeback), banks the turn as a corpus file and
# asks the running `gbrain serve` over its unix socket to extract it. A refused call only leaves the banked file for the sweep. The
# hook itself never talks to a model.
#
# Why the wrapper: (1) the hook must cost a turn nothing, so gbrain's start-up runs in a detached child (setsid -f, or nohup in the background where setsid is missing; all output to
# /dev/null, a hard `timeout`); (2) variables named in the setting gbrain_unset_env (default none) are removed from the child's
# environment, for a setup that keeps metered API keys out of gbrain's reach; (3) GBRAIN_STOP_PUSH=0 keeps gbrain's other stop step,
# the per-turn workspace git push, off - only the write-back is wanted; (4) GBRAIN_HOOK_LANE=harness makes gbrain yield silently in a
# workspace that has its own bootstrap-v1 install, so a machine-wide user-scope hook never fires the same event twice.
#
# Fail-open: no gbrain on PATH, no stdin, a spawn error - all exit 0 with empty output (a Stop hook that prints or exits 2 can hold a
# turn). One line on stderr says when gbrain is missing, or when there is neither setsid nor nohup to detach it with. Undo, live at once, no settings edit: `gbrain config set memory.auto_writeback
# off` turns the hook into a no-op.
# Settings: STACKSHOT_PLUGIN_GBRAIN_UNSET_ENV or the key gbrain_unset_env (a comma-separated list or a JSON list of variable names);
# GBRAIN_WRITEBACK_BIN names the gbrain binary (the tests use a stub).

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
payload=$(timeout 2 cat 2>/dev/null) || payload=""
[ -n "$payload" ] || exit 0

# gbrain's own automation never feeds the brain back into itself: anything under ~/.gbrain (its hermetic claude-cli config dir
# included), a ~/.gbrain-* working folder of a gbrain job, and gbrain's own claude-cli subprocess scratch cwd
# (/tmp/gbrain-claude-cli-cwd-<pid>). Their Stop turns are maintenance, and extracting them only spends tokens.
cwd=$(printf '%s' "$payload" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
case "$cwd" in
    "$HOME/.gbrain"|"$HOME/.gbrain/"*|"$HOME/.gbrain-"*|/tmp/gbrain-claude-cli-cwd*) exit 0 ;;
esac

export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"
bin="${GBRAIN_WRITEBACK_BIN:-$(command -v gbrain 2>/dev/null)}"
if [ -z "$bin" ] || [ ! -x "$bin" ]; then
    echo "stackshot-plugin: gbrain-writeback-hook.sh: gbrain is not on PATH; the write-back is off" >&2
    exit 0
fi

unset_args=()
if command -v python3 >/dev/null 2>&1; then
    while IFS= read -r v; do
        case "$v" in ""|*[!A-Za-z0-9_]*) ;; *) unset_args+=(-u "$v") ;; esac
    done < <(python3 "$here/_config.py" list gbrain_unset_env 2>/dev/null)
fi

# One plain detached child. Its stdin is the pipe from printf, stdout/stderr go nowhere, so Claude Code sees the hook finish now.
# setsid where util-linux has it (a new session, so nothing of the turn's process group reaches it); else nohup in a background
# subshell; with neither, one line on stderr says so, as for a missing gbrain (gbrain is never run in the foreground, which would
# hold the turn).
if command -v setsid >/dev/null 2>&1; then
    printf '%s' "$payload" | setsid -f env "${unset_args[@]}" GBRAIN_HOOK_LANE=harness GBRAIN_STOP_PUSH=0 \
        timeout 15 "$bin" hook stop >/dev/null 2>&1
elif command -v nohup >/dev/null 2>&1; then
    (printf '%s' "$payload" | nohup env "${unset_args[@]}" GBRAIN_HOOK_LANE=harness GBRAIN_STOP_PUSH=0 \
        timeout 15 "$bin" hook stop) >/dev/null 2>&1 &
else
    echo "stackshot-plugin: gbrain-writeback-hook.sh: neither setsid nor nohup is on PATH; the write-back is off" >&2
fi
exit 0
