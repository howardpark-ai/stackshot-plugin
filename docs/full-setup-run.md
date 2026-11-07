# Full-setup guide: the neutral run

The record of one run of [full-setup-guide.md](full-setup-guide.md), step by step, in a throwaway home directory with no access to any
existing setup. What passed, what could not run and why, and what the run changed in the guide.

Versions: this plugin (version 0.1.2) plus the guide's own change; the stackshot and salamander of the time;
gbrain `0.60.538.0` (upstream `latest-stable`, installed from GitHub with bun 1.4.4); Claude Code 2.1.316; system `python3` 3.14.4.

Note: the run used stand-ins in place of clones of the stack's repositories ("the snapshot" is the copy of stackshot's files the run used), so every "stand-in" below describes that; the guides install over https with no credentials.

Note added later: with Claude Code 2.1.318 and gbrain `0.60.583.0`, a first-time `claude plugin install` run after gbrain's setup drops the marker as well (installing a plugin that is already installed does not); the guides say so.

Note added later: stackshot's `doctor` now counts the page-gate hooks the plugin carries, so its hooks line no longer reads `1 of 4` in this setup; the record below keeps what the run saw.

## The neutral environment

- A temp folder with a lock held for the whole run (`flock` on the folder, held by a background process that was ended by its process id at the end). Inside it: `HOME`, `XDG_*`, `TMPDIR` and `TMUX_TMPDIR`.
- The environment was built from nothing (`env -i`). `PATH` was one folder of symlinks to system tools (`bash`, `python3`, `git`, `curl`, `uv`, `bun`, `claude`, `tmux`, coreutils) plus the throwaway home's own `.bun/bin` and `.local/bin`. Not on it at the start: `sala`, `gbrain`, `psql`, `systemctl`, `ssh`, `gh`.
- `GIT_SSH_COMMAND=/bin/false` and a private git config (a made-up identity), so no ssh key was used. The two repositories could therefore not be cloned; stand-ins are named per step below.
- Nothing under the real `~/.claude`, `~/.config/stackshot` or `~/.gbrain` was read or written (checked at the end: nothing there names the temp folder). No tmux session was started. No Claude process was killed; each `claude -p` call ended by itself (not signed in).
- Not neutral: the private skills repo was copied from a clone made beforehand; sala's tree came from the committed tree of an existing checkout, read-only; only Python 3.14 was used; the `claude` binary is the one installed on the box, run with the throwaway home.

## Steps

| # | Guide step | Result |
|---|---|---|
| 0 | Preconditions (section 2) | Ran, passed: `python3`, `bash`, `git`, `curl`, `uv`, `bun` 1.4.4, `tmux`, `claude` found. `~/.claude` did not exist yet in the throwaway home (see step 5). |
| 1 | Board: the board guide's steps 1 to 5 | Ran, passed, with a stand-in clone (a copy of the snapshot). `./stackshot setup --yes --owner Ann --tz UTC --base-url http://127.0.0.1:18020 --port 18020 --emails ann@example.test --prefixes "[work] " --no-systemd` exited 0; the `ui_open = false` edit and `./stackshot doctor` behaved as the board guide says (exit 1, five `missing`). **Could not run:** the board guide's steps 6 to 10 and its step 5 remote (a Google OAuth client, `oauth.env` values, starting the server, a public HTTPS name, the claude.ai connector, lingering and the systemd user units: no root, no Google or claude.ai sign-in, no systemd user manager for the throwaway user). Not repeated here. |
| 2 | sala: the sala guide's steps 1 to 5 and 9 | Ran, passed, with a stand-in tree. `install.sh` printed the `✅ salamander` line; after the guide's step 3 settings merge it printed no `settings.json` warning; `sala config` showed the step 4 values; `sala lane --list` and `sala spawn first --cwd "$HOME" --dry-run` ended with `nothing made, recorded or typed`; the step 9 board lines were set and `python3 ~/stackshot/client.py recycle-wanted` exited 0 (empty: the board server was down). **Could not run:** step 6 (a real spawn: sala uses the default tmux socket, and there was no Claude sign-in), 7, 8 and 10 (systemd, tmux-resurrect, web UI). |
| 3 | Plugin: install, list, settings | Ran, passed, with the local folder of the branch standing in for the repository (`claude plugin marketplace add <folder>`, then `claude plugin install stackshot@stackshot-plugin`). `claude plugin list`: version 0.1.2, scope user, enabled. `claude plugin validate`: passed. `claude plugin update`: "nothing to update" for a folder source. The real CLI loaded the plugin (`Read hooks.json for plugin stackshot`, 22 hooks registered from 4 plugins, 3 agents). `~/.config/stackshot/plugin.json` (`stackshot_dir`, `owner_name`) was read by the plugin's config reader. **Could not run:** a clone of the repository by `owner/repo` (no credentials, by design); `/hooks` and `/plugin` (interactive). |
| 4 | The brief hook (skip `client install`) | Ran, passed. The `SessionStart` entry was added to `settings.json`; run by hand with sample JSON, `session-start.sh` printed `Board: empty.` with the board server down (it falls back to the data repository); started by the real CLI it finished with status 0 beside the plugin's, sala's and gbrain's `SessionStart` hooks. `./stackshot doctor` then said `Claude Code hooks ...: 1 of 4` (documented in the guide as expected). |
| 5 | gbrain, upstream's steps | Ran, passed after one fix to the guide. `bun install -g github:garrytan/gbrain#latest-stable` (bun printed "Blocked 1 postinstall"; nothing broke); `gbrain init --pglite --no-embedding`; `gbrain setup claude-code --dry-run`; `gbrain setup claude-code` (`Verify: ok`, `Turn hook: ok`); `gbrain doctor` 100/100 "All checks passed"; `gbrain remember ... --provenance ...` then `gbrain recall` returned the fact. The plugin's write-back hook, run with a sample Stop payload, exited 0 and wrote nothing (`memory.auto_writeback` is unset), and `gbrain hook stop` exited 0. The plugin's redirect guard, with no `psql` and the real gbrain CLI on `PATH`, allowed a write to an ordinary slug in 1.6 seconds and denied a write to a redirect stub it was given, naming the target page. **Could not run:** the write-back end to end (needs a running `gbrain serve` and a signed-in session), a Postgres brain, embeddings or a model key, `gbrain mcp expose`. |
| 6 | The rulebook | Ran, passed. The template was copied from the plugin's cache folder (`~/.claude/plugins/cache/stackshot-plugin/stackshot/*/templates/RULES.md`), put under git in a folder of its own, and the import line `@~/rulebook/RULES.md` was appended to `~/.claude/CLAUDE.md` outside gbrain's marker block; the file the line names exists, and both gbrain setup and its removal left the line alone. **Could not run:** that a session loads it (`/memory`), and that sessions follow it: no signed-in session. |
| 7 | Connectors | **Could not run:** the claude.ai custom connectors (no claude.ai login), `gbrain mcp expose --funnel` (Tailscale, root, a public name), an MCP portal. Only the optional `[pages] flavor = "gbrain"` switch was tried: stackshot's config reader accepted it. |
| 8 | Check the whole | Ran, passed as far as it goes. `claude plugin list`, `./stackshot doctor` (only the lines the missing board pieces explain, and the `1 of 4` hooks line), `sala config`, `gbrain doctor`. `claude -p --debug hooks` with no sign-in loaded everything and ran all four `SessionStart` hooks (sala's, the plugin's `build-stops` and page-gate keeper, stackshot's brief, gbrain's) with status 0; the brief (`Board: empty.`) and gbrain's "Hot memory" block were delivered; the only plugin output was the stderr line `build-stops: no build_stops setting ...` (shown only in debug output). The call itself then ended with "Not logged in", as expected. **Could not run:** a real session's first context, the prompt and Stop hooks in a live turn. |

## What the run changed in the guide

1. **Order of the steps.** A first `gbrain setup claude-code` failed with `workspace directory does not exist` because `~/.claude` did not exist yet. The prerequisites now say Claude Code must have run once, and the plugin comes before gbrain.
2. **`gbrain remember` needs `--provenance`.** The first call without it failed with `invalid_params`; the guide's example has it.
3. **A Claude Code trap.** `claude plugin enable`, `disable` and `uninstall` rewrite `settings.json` and drop the marker gbrain puts on its two hooks; `gbrain doctor` then warns the per-turn hook is not loaded, and a plain second `gbrain setup claude-code` does not notice. `gbrain setup claude-code --remove` followed by `gbrain setup claude-code` repairs it (checked: doctor back to 100/100, the brain, its fact and the rulebook line untouched). `claude plugin install` and `update` do not drop it. Both guides say so, with the repair, and the troubleshooting tables list it.
4. **The page gate and `client install`.** The board guide's step 11 writes four page-gate hooks, three of which this plugin carries; the guide skips it and adds the one hook the plugin lacks, the board's brief. The doctor line `1 of 4` follows from that and is documented.
5. **Where the template is.** A plugin does not live under `marketplaces/`; the guide copies from the plugin's cache folder.
6. **What a hook's stderr line is.** The `stackshot-plugin: ...` lines are notes about what is off, shown only in debug output; the guides' "first context" check says so.

## What the run did not cover (the `TODO(verify)` list)

- The clone by `owner/repo` over https.
- A Google sign-in, the board server running, the HTTPS front, the claude.ai connectors (Stackshot, and gbrain's), lingering and the user units.
- A signed-in session: the first context, the rulebook being loaded and followed, the prompt and Stop hooks in a live turn, the `spawn` skill, the three helper agents in use.
- The gbrain write-back end to end, a Postgres brain for many sessions, `gbrain mcp expose --funnel`, model keys and embeddings.
- A stackshot release whose doctor counts the plugin's hooks; the project pages inside gbrain.
- `claude plugin update` against a real remote, other Python versions, other distributions.
- The `/hooks` and `/plugin` screens and the `/memory` listing (interactive).
