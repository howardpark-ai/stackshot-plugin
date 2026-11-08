# What is optional

Every hook that needs stackshot, sala or gbrain works without it: it **allows the call, writes one line to stderr** saying what is
missing, and goes on. None of them can fail a session: any error or unreadable input is a silent exit 0, and a refusal is always a
`deny` that names what to change, never an `ask`.

| If this is missing | These hooks | What happens |
|---|---|---|
| a stackshot checkout (`STACKSHOT_DIR`, default `~/stackshot`) | `claude-project-page.sh`; `board-repo-guard` (its connector half); `gbrain-redirect-guard`, `post-tool-note` (they fall back to the tool's name) | the page gate is off; connector writes to the board repo are not checked; connector calls are read from their names |
| stackshot's roles file (`~/.local/state/stackshot/roles.json`, written by the stackshot server) | `build-stops`, `front-watch-gate` rule 1, `handoff-item-note` | no session is a front agent, so no stop fires; the GitHub polling floor of `front-watch-gate` still applies |
| the `build_stops` setting | `build-stops` | no project is guarded; the hook does nothing |
| sala | `build-stops`, `front-watch-gate` (session ids), the `spawn` skill | names come from stackshot's fleet store or Claude's own session files; `spawn` says sala is not installed and stops |
| gbrain on `PATH` | `gbrain-writeback-hook` | no write-back |
| `setsid` (else `nohup`) | `gbrain-writeback-hook` | without `setsid` it detaches with `nohup`; with neither, one stderr line and no write-back |
| `psql`, or the gbrain CLI at `~/.bun/bin/gbrain` (`GBRAIN_BIN`) | `gbrain-redirect-guard` | a write to a redirect stub or a moved page, or a new page of the other brain, is not caught |

The scopes of the stack build on one another: the board alone (stackshot), then with sala, then the full setup with gbrain. This
plugin serves the full setup, and degrades to whatever is installed.
