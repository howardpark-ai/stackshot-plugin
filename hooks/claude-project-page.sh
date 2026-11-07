#!/usr/bin/env bash
# claude-project-page.sh <verb> - the pane-side project-page gate's hook entry (touch, nag, check, keeper). The code is stackshot's
# pages.py; this file only forwards the hook's JSON on stdin to it.
# The stackshot checkout is $STACKSHOT_DIR, else the setting stackshot_dir of ${XDG_CONFIG_HOME:-$HOME/.config}/stackshot/plugin.json,
# else ~/stackshot. Without the checkout (or without python3) the hook does nothing: one line on stderr, exit 0, the session goes on.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null 2>&1 || { echo "stackshot-plugin: claude-project-page.sh: python3 is not on PATH; the page gate is off" >&2; exit 0; }
S=$(python3 "$here/_config.py" stackshot-dir 2>/dev/null) || S=""
S="${S:-$HOME/stackshot}"
if [ ! -f "$S/pages.py" ]; then
    echo "stackshot-plugin: claude-project-page.sh: no pages.py in $S (set STACKSHOT_DIR to the stackshot checkout); the page gate is off" >&2
    exit 0
fi
exec python3 "$S/pages.py" "$@"
