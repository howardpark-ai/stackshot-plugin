# The full stackshot setup: procedure for an AI agent (draft v0.2, Fri Nov 6th 2026)

Audience: a Claude Code assistant that sets up the full stack (board, sala, gbrain, this plugin, a starter rulebook, connectors) on the
user's own Linux box.
Checked on Fri Nov 6th 2026 by running the steps in a throwaway home directory with no access to any existing setup, against this plugin
(version 0.1.2), the stackshot and salamander of that day, gbrain `0.60.583.0` and Claude Code 2.1.318. Later versions of the plugin and of gbrain were not re-run. The current plugin version is 0.1.5. The first full run
is recorded in [full-setup-run.md](full-setup-run.md).
`TODO(verify)` marks what those runs could not reach (a Google sign-in, a claude.ai login, root, a public host name, a Postgres server, a
signed-in session): check it live and tell the user what you found.
The same procedure for the person is [full-setup-guide.md](full-setup-guide.md). The board and sala each have their own pair of guides:
[board-alone-guide.md](https://github.com/howardpark-ai/stackshot/blob/main/docs/board-alone-guide.md),
[board-alone-guide-for-ai.md](https://github.com/howardpark-ai/stackshot/blob/main/docs/board-alone-guide-for-ai.md),
[sala-guide.md](https://github.com/howardpark-ai/salamander/blob/main/docs/sala-guide.md) and
[sala-guide-for-ai.md](https://github.com/howardpark-ai/salamander/blob/main/docs/sala-guide-for-ai.md). Link to them and follow them. Do not repeat them.

## 0. Goal and done-condition

Goal: the board, sala, gbrain and this plugin working together on this box, with the user's own rulebook imported and the connectors
they asked for. Nothing in it names another person's machines, accounts or rules.

Done when all of these hold. Check each yourself, then report.

1. The board's done-condition (board-alone-guide-for-ai.md, section 0) holds, with the plugin counted: `./stackshot doctor` prints `ok      Claude Code hooks in ...: 4 of 4 (3 of them from the stackshot plugin)` (step 4) and exits 0. Every other line is `ok` or an explained `warn`.
2. The sala done-condition (sala-guide-for-ai.md, section 0) items the user chose hold; at least `sala spawn <name> --cwd "$HOME" --dry-run` ends with `nothing made, recorded or typed`.
3. `claude plugin list` shows `stackshot@stackshot-plugin`, enabled, scope `user`.
4. `~/.claude/settings.json` has the `SessionStart` entry for `session-start.sh` (step 4), no entry for `project-page.sh`, and gbrain's own two hooks.
5. `gbrain doctor` ends with `All checks passed.` (before the test fact; after it, on a keyless brain, `All checks OK (some warnings).`); `gbrain remember` then `gbrain recall` round-trips a test fact; `~/.claude.json` has the `gbrain` MCP entry.
6. `~/.claude/CLAUDE.md` has the import line for the user's rulebook, outside gbrain's marker block, and the file it names exists.
7. `~/.config/stackshot/plugin.json` has `owner_name` equal to the board's `[instance] owner`.
8. A new Claude Code session's first context has the board's brief and no hook error line (the user checks this: hand-off H4).

Out of scope (mention as later options only): memory-file sync between machines, hand-off and pick-up, a page-tidying job some setups run (Claude Code's own
memory and gbrain's own steps cover them), anchor-hub, other client machines, the project pages inside gbrain.

## 1. Rules for you

- **Never print a secret.** `oauth.env`, `client.token`, `client-tokens/*`, `ui-session.key`, `~/.gbrain/serve/admin-token`, any `~/.gbrain/env`, API keys and bearer tokens. Count matches or test file modes instead. Never `cat` them. Never put a secret in a command line.
- **Do not edit files inside `~/stackshot`, `~/salamander` or the plugin's folder.** Updates replace or refuse on them. Your files are `~/.config/stackshot/*`, `~/.config/salamander/config`, `~/.claude/settings.json`, `~/.claude/CLAUDE.md` and the user's rulebook.
- **Never kill a Claude process by name.** No `pkill claude`, `killall claude`, `pgrep -x claude`. Retire a session with `sala retire NAME`. Use a tmux socket of your own (`tmux -L <name> -f /dev/null`) for any test pane.
- **`uv` for Python, never `pip install`.** gbrain is installed with `bun`, from GitHub, never from npm (the npm package called `gbrain` is a different project).
- **Run each `sala` step as one plain command.** Do not chain it with others.
- **Do not guess a value that names the user** (name, zone, scopes, owner name). Ask (H0).
- **Do not edit the user's rules for them.** The rulebook is the user's own setting. You copy the blank template and leave it as it is unless they dictate rules; you write only what they dictate. Never write a rule on your own judgment, and never copy rules from another setup.
- **gbrain's setup is upstream's, not yours.** Read the current steps at `https://github.com/garrytan/gbrain` (README, "Install") before you run them; this file has the shape, not the last word. Do not read `~/.gbrain/env` or any file that may hold keys; take variable names from upstream's docs only. Do not add model API keys to gbrain unless the user decides to, after you have told them what that costs.
- **Do not turn on what the user did not ask for:** the write-back (`memory.auto_writeback`), `gbrain mcp expose`, any `autopilot` service, the sala watchdog, Tailscale. Say what each does and wait.
- **Do not copy the plugin's hooks into `settings.json`.** They would run twice.
- **At a hand-off, stop, tell the user exactly what to do, and end your turn.** Continue after they reply.
- **Check after every step.** If the result differs from "Expected", stop and read section 6.

## 2. Facts

| Item | Value |
|---|---|
| Plugin repository | `https://github.com/howardpark-ai/stackshot-plugin` (public; MIT; clones and installs over https with no credentials) |
| Install | `claude plugin marketplace add howardpark-ai/stackshot-plugin`, then `claude plugin install stackshot@stackshot-plugin` (scope `user`); inside a session `/plugin marketplace add ...` and `/plugin install ...` |
| Update | `claude plugin marketplace update stackshot-plugin`, then `claude plugin update --scope user stackshot@stackshot-plugin`; a new session or `/reload-plugins` loads it |
| Contents | the hooks of `hooks/hooks.json` (the person's guide, section 4, lists them), the `spawn` skill, agents `executor`, `deep-engineer`, `sweeper` |
| Needs | `python3` 3.12+ (the board guide's requirement) and `bash` on `PATH`; standard library only |
| Plugin settings | env `STACKSHOT_PLUGIN_<NAME>`, else key in `${XDG_CONFIG_HOME:-~/.config}/stackshot/plugin.json`, else default. Claude Code passes the `env` block of `settings.json` to hooks |
| Hook logs | `${XDG_STATE_HOME:-~/.local/state}/build-stops/firings.jsonl`, `.../front-watch-gate/firings.jsonl`, prompt stamps in `.../stackshot/prompts/` |
| Failure mode | A hook whose dependency is missing allows the call and writes one stderr line; any error is a silent exit 0; a refusal is `deny`, never `ask` |
| Board checkout | `~/stackshot` (`stackshot_dir` / `STACKSHOT_DIR`); board config `~/.config/stackshot/stackshot.toml`. Update it with `./deploy.sh`, never a `git pull` first (board guide, section 6) |
| Brief hook | `bash ~/stackshot/client/hooks/session-start.sh` (stackshot's own; prints the board's brief, then the page keeper's note; falls back to the data repository when the server is down). The plugin's `SessionStart` keeper hook prints the same note, so a session that keeps a page sees it twice: harmless, and not a reason to drop either hook |
| Page gate | the plugin's `hooks/claude-project-page.sh <keeper\|nag\|touch\|check>` forwards to `~/stackshot/pages.py`. `./stackshot client install` writes the same verbs into `settings.json`: never both |
| gbrain | upstream `github.com/garrytan/gbrain`, MIT; needs `bun` 1.4.4+; install `bun install -g github:garrytan/gbrain#latest-stable`; home `~/.gbrain` |
| gbrain keyless start | `gbrain init --pglite --no-embedding`: local file database, keyword search, no model call. PGLite admits one process at a time |
| gbrain for Claude Code | `gbrain setup claude-code` (`--dry-run` first, `--remove` undoes): stdio MCP entry in `~/.claude.json` (the memory verbs, `serve --surface starter`), hooks `SessionStart` and `UserPromptSubmit` in `~/.claude/settings.json` (command `gbrain hook session-start` / `user-prompt`), a brain-first block between markers in `~/.claude/CLAUDE.md`, a receipt `~/.gbrain-connection-claude-code-gbrain.json`. Its plan has two decisions, `capture` (gbrain's own Stop and SessionEnd hooks and transcript capture) and `providers` (a paid model), both declined by default. Needs `~/.claude/` to exist |
| gbrain write-back | the plugin's Stop hook calls `gbrain hook stop` detached with `GBRAIN_HOOK_LANE=harness GBRAIN_STOP_PUSH=0`; extraction runs only when `memory.auto_writeback` is `salient` or `all` (unset: no Stop-turn capture) |
| Rulebook template | `templates/RULES.md` in the plugin (also in the plugin cache under `~/.claude/plugins/cache/stackshot-plugin/stackshot/<version>/templates/`) |
| Rulebook import | one line `@~/<folder>/RULES.md` in `~/.claude/CLAUDE.md` |
| Connector names | the board's: **Stackshot** (tools `mcp__claude_ai_Stackshot__<tool>`); gbrain's, if a claude.ai connector is made: a name the user picks |

## 3. Settings

### Set with the user (ask, then write)

| Where | Key | Value |
|---|---|---|
| `plugin.json` | `owner_name` | the user's first name, equal to the board's `[instance] owner` |
| `plugin.json` | `stackshot_dir` | only if the board is not at `~/stackshot` |
| `plugin.json` | `build_stops` | only if they run front and builder sessions: ask for each project's short name, builder session and guarded repositories; the project's permanent id (`prj_` and eight hex digits) is `python3 ~/stackshot/tools/project.py show projects/<slug>` (`hooks/build-stops.py` header has the shape). Otherwise leave out |
| rulebook | nothing, unless they dictate rules | leave the template as it is; write only the rules they dictate |

### Leave at the defaults

`item_ref_patterns`, `agent_namespaces`, `stackshot_toml_fallbacks`, `prompt_skip_prefixes`, `portal_prefixes`, `github_entry`,
`connector_entries`, `workspace_ids`, `gbrain_entries`, `gbrain_moved_banner`, `gbrain_moved_keep`, `gbrain_moved_write_hint`, `gbrain_move_permit_cli`, `gbrain_newpage_patterns`, `gbrain_newpage_org`, `gbrain_newpage_hint`, `gbrain_direct_exclude`, `gbrain_unset_env`, `no_poll_hosts`, `no_poll_note`, `ci_runs_hint`: see the
plugin's settings page (`docs/settings.md`). Set `portal_prefixes` and the entry ids only if the user runs an MCP portal.

## 4. Procedure

### Step 0. Preconditions and hand-off H0

```bash
claude --version                              # a version
test -d ~/.claude && echo ok                  # ok (Claude Code has run once)
python3 --version                             # 3.12 or newer
bash --version | head -1; command -v git curl uv tmux
bun --version                                 # 1.4.4 or newer
systemctl --user status >/dev/null 2>&1; echo $?   # 0 (a user manager runs)
```

If `claude` is not signed in, STOP: tell the user to run `claude` and finish the sign-in. If `~/.claude` is missing, the same. If `bun`
is missing, tell the user to install it from `bun.sh` (hand-off H3) and wait.

**H0. Questions for the user.** Ask all of these in one message, then wait:

1. The questions of the board guide's hand-off H0 and of the sala guide's H0 (name, time zone, prefixes, base URL, emails, scopes). Collect them now so each guide's step does not ask again.
2. Do you want gbrain, and the key-free start (recommended) or a model key from the start?
3. Do you want the write-back (gbrain saving facts you state, at the end of each turn) turned on now? It is a paid model call if a key is set.
4. Where should your rulebook live (default `~/rulebook`), and do you have a private git repository for it?
5. Which connectors: Stackshot in claude.ai (yes, it is the board's), gbrain in claude.ai (needs Tailscale and a brain that serves more than one process), an MCP portal (optional)?
6. Do you run front and builder sessions (then the `build_stops` setting), or one kind only?

### Step 1. The board

Follow board-alone-guide-for-ai.md steps for the board, up to and **not including** `./stackshot client install`. Its hand-offs (the
secrets file, the Google client, the HTTPS front, the claude.ai connector) are the user's. Name the connector **Stackshot**.

Expected: the board guide's done-conditions 1, 2, 3, 5 hold (the doctor `hooks` line is `ok` once the plugin is installed in step 3; condition 4's tools exist once the user signs in).

### Step 2. sala

Follow sala-guide-for-ai.md steps 0 to 6 and 9, and the optional steps the user chose. The watchdog (step 7) is the sala guide's recommended step: offer it, say that it updates Claude Code and relaunches panes by itself, and start it only on the user's yes.

Expected: `sala spawn first --cwd "$HOME" --dry-run` ends with `nothing made, recorded or typed`; `~/salamander/install.sh` prints no `settings.json` warning.

### Step 3. The plugin

```bash
claude plugin marketplace add howardpark-ai/stackshot-plugin
claude plugin install stackshot@stackshot-plugin
claude plugin list
```

The repositories are public: no credentials are needed, and you never use the user's personal key. If the clone fails, check
`git ls-remote https://github.com/howardpark-ai/stackshot-plugin`; a failure there is the box's network or proxy: STOP and tell the user.
Checked on plugin 0.1.4 from a clean home (no git account, SSH key or login): both commands succeed. The short name tries SSH first and retries over https by itself; `https://github.com/howardpark-ai/stackshot-plugin.git` skips the SSH try.

Expected: the list shows `stackshot@stackshot-plugin`, version `0.1.x`, scope `user`, `✔ enabled`.

Write the settings file (create the folder first). Use an editor-free way that keeps nothing else:

```bash
mkdir -p ~/.config/stackshot
test -e ~/.config/stackshot/plugin.json && echo "exists: merge, do not overwrite"
cat > ~/.config/stackshot/plugin.json <<'EOF'
{
  "stackshot_dir": "~/stackshot",
  "owner_name": "<the user's first name>"
}
EOF
python3 -I "$(ls -d ~/.claude/plugins/cache/stackshot-plugin/stackshot/*/ | sort -V | tail -1)hooks/_config.py" stackshot-dir
```

Expected: the last command prints the full path of the board's checkout. If `plugin.json` already exists, merge the keys.

### Step 4. The brief hook

Add one entry to `~/.claude/settings.json`; do it with a small script that keeps every other key, never with `sed`. Take a backup first:

```bash
test -e ~/.claude/settings.json && cp ~/.claude/settings.json ~/.claude/settings.json.bak-full-setup
python3 -I - <<'EOF'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
d = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
cmd = "bash ~/stackshot/client/hooks/session-start.sh"
lst = d.setdefault("hooks", {}).setdefault("SessionStart", [])
if not any(h.get("command") == cmd for e in lst for h in e.get("hooks", [])):
    lst.append({"hooks": [{"type": "command", "command": cmd, "timeout": 30}]})
    json.dump(d, open(p, "w", encoding="utf-8"), indent=2)
    print("added")
else:
    print("already there")
EOF
```

Then check that no entry of `settings.json` mentions the board's page-gate script:

```bash
grep -c "project-page" ~/.claude/settings.json      # 0
```

Expected: `added` or `already there`, and `0`. Then `cd ~/stackshot && ./stackshot doctor`: the hooks line reads `ok      Claude Code hooks in <path>: 4 of 4 (3 of them from the stackshot plugin)`.
doctor counts the page-gate hooks of a plugin named `stackshot@...` that is enabled in the user's `settings.json` (`enabledPlugins`) and listed in
`~/.claude/plugins/installed_plugins.json`; a project-scope install does not count. The plugin's keeper hook counts as the session-start page hook, so before this
step the line already reads `4 of 4 (4 of them from the stackshot plugin)`: doctor does not check for the brief hook. If it says `1 of 4` and `missing`, check `claude plugin list`
and whether the board's checkout is older than the release that counts the plugin (update it with `./deploy.sh`, never a `git pull` first). Do not run `client install` to clear it: it would add the page-gate hooks to `settings.json` a second time.
`session-start.sh` also prints the page keeper's note, as the plugin's `SessionStart` hook does, so a session that keeps a page sees that note twice. Leave both.

### Step 5. gbrain

Read upstream's current install steps first (README "Install"; `docs/mcp/CLAUDE_CODE.md`). At the time of writing:

```bash
bun install -g github:garrytan/gbrain#latest-stable
gbrain --version
gbrain init --pglite --no-embedding
gbrain setup claude-code --dry-run
```

Show the user the `--dry-run` output (brain, transport, files it will write, decisions) and wait for a yes (hand-off H2). Then:

```bash
gbrain setup claude-code
gbrain doctor | tail -3
gbrain remember "guide test fact" --entity "Setup test" --provenance "told by the user during setup"
gbrain recall "guide test fact"
```

Expected: `Verify: ok (smoke_passed)` and `Turn hook: ok`; `Overall health score: 100/100. All checks passed.`; the recall prints the fact. Run `gbrain doctor` before the test
fact: a keyless brain that holds a fact scores low on embeddings, so afterwards doctor ends `All checks OK (some warnings).`, which is expected. Delete the test fact
if the user asks (`gbrain forget`, see `gbrain forget --help`). The setup plan's two decisions, `capture` and `providers`, are declined by default: leave them
(`--capture` adds gbrain's own Stop and SessionEnd hooks and transcript capture; `--providers` sets up a paid model; only the user decides either).

`bun install` prints `Blocked 1 postinstall`; nothing is wrong yet. If a gbrain command later says the schema is not migrated, run
`gbrain apply-migrations --yes --no-autopilot-install` (upstream issue 218). If `setup` says the workspace directory
does not exist, `~/.claude` is missing: go back to step 0.

Order matters once: `claude plugin install` (of a plugin that is not installed yet), `enable`, `disable` and `uninstall` rewrite `settings.json` and drop the marker gbrain puts on its two hooks,
so `gbrain doctor` then warns `harness_wiring ... per-turn hook` and a plain re-run of `gbrain setup claude-code` does not notice. Do the plugin
first (step 3). If you must run one of those commands later, repair with `gbrain setup claude-code --remove`, then `gbrain setup claude-code`
(it keeps the brain, its notes and the rulebook line). Installing a plugin that is already installed changes nothing.

State the three consequences to the user (they are in the person's guide, step 5): one writer (PGLite), the write-back is off until
`gbrain config set memory.auto_writeback salient`, search is keyword-only without embeddings. Turn the write-back on only if the user said
yes in H0.

Optional, only if asked: move to Postgres for many sessions (upstream `docs/guides/move-to-postgres.md`). `TODO(verify)`: not run.

### Step 6. The rulebook

```bash
d=~/rulebook      # the folder the user chose in H0
mkdir -p "$d"
test -e "$d/RULES.md" && echo "exists: stop and ask" || cp "$(ls -d ~/.claude/plugins/cache/stackshot-plugin/stackshot/*/ | sort -V | tail -1)templates/RULES.md" "$d/RULES.md"
```

Leave the copy as it is: it holds no rules, only a one-line description and one placeholder per section. Write a rule into it only when the user dictates
one, in their words, replacing that section's placeholder; never invent a rule. Tell them the file is theirs to fill in. Remove the comment block at the top only if they ask. If they gave
a private repository, `git -C "$d" init`, commit, and add the remote they name (push is theirs to approve).

Then add the import line to `~/.claude/CLAUDE.md`, outside gbrain's marker block, once:

```bash
grep -qxF '@~/rulebook/RULES.md' ~/.claude/CLAUDE.md 2>/dev/null || printf '\n@~/rulebook/RULES.md\n' >> ~/.claude/CLAUDE.md
```

Use the user's folder in both places. Expected: `grep -c '^@~/.*RULES.md$' ~/.claude/CLAUDE.md` prints `1`, and the named file exists. `TODO(verify)`: that
a session loads it (`/memory` lists it): needs a signed-in session.

### Step 7. Connectors

1. **Stackshot:** done by the board guide's connector step (hand-off there).
2. **gbrain, local:** done in step 5.
3. **gbrain in claude.ai (optional, only if H0 said yes):** read upstream's `docs/guides/remote-mcp.md`, `docs/mcp/CLAUDE_COWORK.md` and the "A claude.ai connector can search but not save" entry of `docs/mcp/DEPLOY.md`. A PGLite brain cannot serve this and a second local session at once: say so first. Then, with the user:
   1. `gbrain mcp expose --funnel --dry-run`. Show the user the plan (it may install Tailscale and run `sudo`, publishes a public HTTPS address, installs a user service) and wait for a yes (hand-off H5). Without `--funnel` the address is tailnet-only and claude.ai cannot reach it. The tailnet needs MagicDNS, HTTPS certificates and the Funnel attribute: if one is missing the command stops, publishes nothing and names it; that is the user's to turn on in Tailscale's admin pages. Add `--enable-dcr` only if the user picks self-registration (point 3).
   2. Run it without `--dry-run`. Note the connector address it prints (`https://your-machine.your-tailnet.ts.net/mcp`). It also names the owner credential file (`~/.gbrain/serve/admin-token`): pass it only as `--admin-token-file ~/.gbrain/serve/admin-token`, never read or print it.
   3. Default, a client registered by you: `mkdir -p -m 700 ~/private`, then `gbrain mcp admin register claude-ai --redirect-uri https://claude.ai/api/mcp/auth_callback --token-endpoint-auth-method client_secret_post --scopes read,write --url <the connector address> --admin-token-file ~/.gbrain/serve/admin-token --credentials-out ~/private/claude-ai-client.json`, first with `--dry-run` and the preview shown, then without. The credentials file holds the OAuth client ID and client secret: never read, print or quote it. Tell the user its path. Self-registration instead (`--enable-dcr` on the `expose` command): claude.ai registers itself and every new connection waits for the user's approval in the brain's admin dashboard (`gbrain mcp admin login-link --url <the connector address> --admin-token-file ~/.gbrain/serve/admin-token` gives the owner a single-use link: hand it over, never fetch it). The address is public, so tell the user to approve only a request they started.
   4. The claude.ai step is the user's: Settings, Connectors, Add custom connector, a name, the `.../mcp` address; with a registered client, Advanced settings, paste the OAuth Client ID and OAuth Client Secret from the file (they open it themselves); connect and approve the consent screen including write (a read-only approval can search but not save).
   5. Check with `gbrain mcp expose --status`, and ask the user to have a chat call `get_brain_identity` and search.
   `TODO(verify)`: all of this item; the `register` flags are from `gbrain mcp admin register --help` (gbrain `0.60.583.0`) and upstream's docs.
4. **MCP portal (optional):** only if the user already runs one: add its tool-name prefix to `portal_prefixes` and its entry ids to `connector_entries` / `gbrain_entries` in `plugin.json`. `TODO(verify)`.

### Step 8. Check, and hand-off H4

```bash
claude plugin list
cd ~/stackshot && ./stackshot doctor
sala config | grep -E 'SALA_(OWNER_NAME|SCOPES|TZ)'
gbrain doctor | tail -3
```

Then ask the user (H4) to start a new Claude Code session and say what the first context shows: the board's brief, gbrain's context
block, and no hook error. (Lines starting `stackshot-plugin:` on stderr, such as `build-stops: no build_stops setting ...`, say what is switched
off; Claude Code shows them only with `claude --debug hooks`. They are not errors.)

## 5. Hand-offs (summary)

| # | When | The user does |
|---|---|---|
| H0 | start | answers the questions |
| H2 | step 5 | approves gbrain's `setup claude-code` plan |
| H3 | step 0 | installs `bun` |
| H4 | step 8 | starts a new session and reports its first context |
| H5 | step 7.3 | approves `gbrain mcp expose --funnel` and the client registration, then adds the claude.ai connector and pastes the client ID and secret into its Advanced settings |
| the board's and sala's own | their steps | secrets file, Google client, HTTPS front, claude.ai connector, `sudo loginctl enable-linger` |

## 6. Troubleshooting

| What you see | Likely cause | Do this |
|---|---|---|
| `stackshot-plugin: ...: no pages.py in ...` | the board's checkout is not at `~/stackshot` | set `stackshot_dir` |
| `gbrain is not on PATH; the write-back is off` | no gbrain, or `~/.bun/bin` missing | the hook adds `~/.bun/bin`; check the binary |
| the page gate speaks twice | `project-page.sh` entries in `settings.json` | remove them (step 4) |
| `gbrain setup claude-code`: `workspace directory does not exist` | `~/.claude` missing | step 0 |
| `gbrain doctor`: `harness_wiring` warns the per-turn hook is not loaded; `turn_hook_not_loaded` | a `claude plugin install/enable/disable/uninstall` dropped gbrain's marker | `gbrain setup claude-code --remove`, then `gbrain setup claude-code` |
| `gbrain setup`: refuses a live PGLite brain | another process holds it | find the holder's PID (`gbrain doctor` or `ps`) and ask the user before stopping it; never by name; or move to Postgres |
| `bun install` blocked a postinstall | Bun's default | nothing, unless gbrain then reports an unmigrated schema: `gbrain apply-migrations --yes --no-autopilot-install` (upstream issue 218) |
| `claude plugin install` cannot clone the repository | the box cannot reach `github.com` over https (proxy, firewall) | `git ls-remote https://github.com/howardpark-ai/stackshot-plugin`; tell the user what it printed |
| a hook error line on every prompt | a hook script failed | read the stderr line; run the hook by hand with sample JSON on stdin (`hooks/` header comments say what each takes) |
| `doctor`: hooks `1 of 4`, `missing` | the plugin is not installed or enabled at user scope, or the stackshot checkout is older than the release that counts the plugin's hooks | `claude plugin list` (step 3); update the board with `./deploy.sh`; never `client install` (step 1) |

## 7. Using the stack afterwards

- A project's sessions follow the board's page rules; the plugin's hooks remind and refuse as the person's guide section 4 says.
- To add a hook of the user's own, put it in their `settings.json` or in a plugin of their own; never in the plugin's folder.
- The rulebook is the user's: you edit it only when they dictate the change, and you commit it to their repository.

## 8. Updating

`claude plugin marketplace update stackshot-plugin`, then `claude plugin update --scope user stackshot@stackshot-plugin` (then a new session; checked on plugin 0.1.4: "already at the latest version" is the expected output when nothing is newer, a newer one appears only after a release raises the version); the board's `./deploy.sh`; sala's update (sala guide section 6);
gbrain: `GBRAIN_NO_AUTOPILOT_INSTALL=1 GBRAIN_NO_REEMBED=1 gbrain upgrade --no-autopilot-install` after reading its release notes (when a release needs a newer Bun it refuses with exit 78 and changes nothing: `bun upgrade`, then again).

## 9. Report to the user when done

One short message: what is installed (plugin version, gbrain version, board URL), which of the done-conditions hold, the
`doctor` result (its hooks line, and any line that is not `ok`) and why, what is off on purpose (write-back, expose, watchdog), what you could not check (the `TODO(verify)` items you
met), and the files you changed (`~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.config/stackshot/plugin.json`, the rulebook).
