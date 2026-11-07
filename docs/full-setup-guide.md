# The full stackshot setup, for a person (draft v0.2, Fri Nov 6th 2026)

Written for a technical reader who has never seen stackshot, sala or gbrain, and for the Claude Code assistant that helps them.
Scope: the full setup, which is the board (stackshot), the session manager (sala), the memory (gbrain), this plugin, a starter rulebook
and the connectors, layered on the two guides that already exist. This guide does not repeat them; it links to them and says what
changes when the pieces run together.
Checked on Fri Nov 6th 2026 by running the steps in a throwaway home directory with no access to any existing setup, against this plugin
(version 0.1.2), the stackshot and salamander of that day, gbrain `0.60.583.0` and Claude Code 2.1.318. Later versions of the plugin and of gbrain were not re-run. The first full run
is recorded in [full-setup-run.md](full-setup-run.md).
Lines marked `TODO(verify)` are things those runs could not reach: a Google sign-in, a claude.ai login, root, a public host name, a gbrain
database server, or a signed-in Claude Code session.
The same procedure for an AI assistant is [full-setup-guide-for-ai.md](full-setup-guide-for-ai.md).

## 1. What the full setup is

Five pieces, each with its own guide. You can stop after any of the first three.

| Layer | What it gives you | Guide |
|---|---|---|
| **Board** (stackshot) | A task board for you and your Claude sessions, project pages, and one connector that works from claude.ai and Claude Code. | [board guide](https://github.com/howardpark-ai/stackshot/blob/main/docs/board-alone-guide.md) |
| **Sessions** (sala) | Long-lived Claude Code sessions in tmux that survive a reboot or a Claude Code update. | [sala guide](https://github.com/howardpark-ai/salamander/blob/main/docs/sala-guide.md) |
| **Plugin** (this repository) | The hooks, the `spawn` skill and three helper agents that make the board, sala and gbrain work together. | this guide, step 3 |
| **Memory** (gbrain) | A knowledge base your sessions search before they answer and write to as they learn. It is a separate open-source project. | [upstream docs](https://github.com/garrytan/gbrain), this guide step 5 |
| **Rulebook and connectors** | Your own standing instructions, imported by every session; claude.ai connectors for the board and the brain. | this guide, steps 6 and 7 |

Not included, on purpose: moving memory files between machines, session hand-off and pick-up, and a page-tidying job some setups run.
Claude Code's own memory and gbrain's own steps cover them. Habits that are one person's taste (reply language, time zone notes,
question-picker rules) are not in the plugin either: they belong in your own rulebook (step 6) or your own hooks (section 5).

Everything runs as your own user on one Linux box. No `sudo`, except the lines the board and sala guides name, and optionally Tailscale (step 7).

## 2. What you need before starting

Tick each line before step 1.

- [ ] Everything the board guide's checklist asks for, and the same for the sala guide. They are the first two steps below, so read both checklists now. In short: a Linux box with systemd, `git`, `curl`, `uv`, `tmux`, bash 4.4 or newer, a public HTTPS name for the board, a Google OAuth client, a claude.ai plan that allows custom connectors, and a private git repository for the board's data.
- [ ] Claude Code installed and signed in on the box (check: `claude --version`, then start `claude` once and finish the sign-in). The first run also creates `~/.claude/`, which the later steps write into.
- [ ] `python3` 3.12 or newer on `PATH` (the board guide asks for the same) and `bash`. The plugin's hooks use the standard library only.
- [ ] `bun` 1.4.4 or newer, for gbrain (check: `bun --version`; install it from [bun.sh](https://bun.sh)).
- [ ] A box that can reach `github.com` over https. The stack's repositories, `github.com/howardpark-ai/stackshot`, `github.com/howardpark-ai/salamander` and `github.com/howardpark-ai/stackshot-plugin`, are public: they clone and install with no GitHub account and no credentials.
- [ ] Time. The board and sala guides take an evening between them (the Google client and the HTTPS name are the slow parts). Steps 3 to 6 here take about half an hour.

Not needed: a database server (gbrain starts with a local file database), an API key for any model (gbrain starts keyless), Cloudflare, Slack.

## 3. Setup, step by step

Run each command on the server box, as your own user.

### Step 1. Install the board

Follow the [board guide](https://github.com/howardpark-ai/stackshot/blob/main/docs/board-alone-guide.md), steps 1 to 10, then stop. Two differences from the board guide when you run the full setup:

- **Skip its step 11** (`./stackshot client install`). That command adds four hooks to `~/.claude/settings.json` (the brief at session start, and the page gate's `nag`, `touch` and `check`), and this plugin carries the same three page-gate hooks; with both, each runs twice. Step 4 below adds the one you do need, the brief at session start.
- Name the claude.ai connector **Stackshot**, exactly, as the board guide says. The plugin's hooks read board writes by that name.

Good result: the board guide's step 10 test passes (a claude.ai chat calls the connector's `brief` tool and gets the empty board).

### Step 2. Install sala

Follow the [sala guide](https://github.com/howardpark-ai/salamander/blob/main/docs/sala-guide.md), steps 1 to 6, and step 9 (connect sala to your board), which you can do now because the board runs. Step 7 (the watchdog) is recommended: it revives dead panes and runs queued operations, and it updates Claude Code by itself, so read what that step says before you start it. Steps 8 and 10 (tmux-resurrect, the web UI) are optional.

Good result: `sala spawn first --cwd "$HOME" --dry-run` ends with `nothing made, recorded or typed`.

sala's step 3 puts its own hooks into `~/.claude/settings.json` (the busy check, the notice hook). They sit next to this plugin's hooks and do not overlap with them.

### Step 3. Install the plugin

```bash
claude plugin marketplace add howardpark-ai/stackshot-plugin
claude plugin install stackshot@stackshot-plugin
claude plugin list
```

(Inside a Claude Code session the same two steps are `/plugin marketplace add howardpark-ai/stackshot-plugin` and `/plugin install stackshot@stackshot-plugin`.) The repository is public, so the box needs no git credentials. Checked on plugin 0.1.4, in a clean home with no GitHub account, no SSH key and no login: both commands worked. The short name in the first command tries SSH first and, when that fails, retries over https by itself, so either form works.

Good result: `claude plugin list` shows `stackshot@stackshot-plugin`, enabled, scope `user`. In a session, `/hooks` lists the hooks and `/plugin` shows the plugin; a new session loads 3 agents (`executor`, `deep-engineer`, `sweeper`) and 1 skill (`spawn`).

The plugin works with whatever is installed. A hook that needs stackshot, sala or gbrain and does not find it **allows the call and writes one line to stderr** saying what is missing; none can fail a session. So the order of the steps does not matter for safety.

**The plugin's settings.** Most of the plugin needs no setting. Where it does, a value is read from the environment variable `STACKSHOT_PLUGIN_<NAME>` (upper case), then from `~/.config/stackshot/plugin.json`, then the default. The settings a new setup meets:

| Setting (file key) | Default | Set it when |
|---|---|---|
| `stackshot_dir` (env `STACKSHOT_DIR`) | `~/stackshot` | The board's checkout is somewhere else. The page gate and the guards import stackshot's code from it. |
| `owner_name` | `the owner` | Always: your first name. It must equal the board's `[instance] owner` (and sala's `SALA_OWNER_NAME`), because stackshot's router writes `[for <name>]` into prompts and the plugin stamps them. |
| `build_stops` | `{}` (off) | You want front agents to hand build work to a builder (an object naming each project, its builder session, the guarded repositories). A project is named by its permanent id, `prj_` and eight hex digits; `python3 ~/stackshot/tools/project.py show projects/<slug>` prints it. Leave it off until you run front and builder sessions. The full shape is in the header of `hooks/build-stops.py`. |
| `item_ref_patterns` | `["\b[a-z][\w-]*#\d+", "/items/[\w-]*\d"]` | Your board writes an item reference another way. |
| `agent_namespaces` | `["stackshot"]` | You also ship the helper agents from a plugin of your own (the README's "Name resolution"). |
| `gbrain_unset_env` | `[]` | gbrain's child process must not see some variables (for example a metered model key you keep away from it). |

The rest (`portal_prefixes`, `connector_entries`, `no_poll_hosts`, `ci_runs_hint` and others) are in the [README's settings table](../README.md#settings). An example `~/.config/stackshot/plugin.json` for a new setup:

```json
{
  "stackshot_dir": "~/stackshot",
  "owner_name": "Ann"
}
```

### Step 4. Put the board's brief into each session

The plugin gives a session the page gate (nag, check, keeper) but not the board's brief at session start. That one hook comes from stackshot. Open `~/.claude/settings.json` in an editor and add this entry under `"hooks"` (create the keys that are missing; keep what is there):

```json
"SessionStart": [
  {"hooks": [{"type": "command", "command": "bash ~/stackshot/client/hooks/session-start.sh", "timeout": 30}]}
]
```

If a `"SessionStart"` list already exists (sala's step 3 added one), add the inner `{"hooks": ...}` object to that list instead of making a second key.

Good result: start a new Claude Code session in any folder. Its first context holds the board's brief (`Board: empty.` on a new board). The script falls back to the data repository when the board server is not running.

The same script also prints the page keeper's note (the Current state of the project pages a session keeps), and the plugin's own `SessionStart` hook prints it too, so a session that keeps a page sees that note twice at session start. It is the same text twice and does no harm.

`./stackshot doctor` counts the page-gate hooks the plugin carries, as long as the plugin is installed and enabled for your user (`claude plugin list` shows scope `user`). With the plugin and the brief hook in place its hooks line is `ok`: `Claude Code hooks in ~/.claude/settings.json: 4 of 4 (3 of them from the stackshot plugin)`. The plugin's keeper hook also counts as the session-start page hook, so the line already reads `4 of 4 (4 of them from the stackshot plugin)` before you add the brief hook: doctor does not check for the brief. It says `1 of 4` and `missing` when the plugin is not enabled for your user and only the brief hook is in place, or when your stackshot checkout predates this counting (update the board as the board guide's section 6 says: `./deploy.sh`, never a `git pull` first). Do not run `./stackshot client install` to fix that: it would write the page-gate hooks into `settings.json` a second time, and each would run twice.

### Step 5. Install gbrain (from its own docs)

This guide installs the plain upstream gbrain. The steps are upstream's, and they change faster than this guide: read the current ones in the [gbrain README](https://github.com/garrytan/gbrain#install) and the [Claude Code page](https://github.com/garrytan/gbrain/blob/master/docs/mcp/CLAUDE_CODE.md). At the time of writing they are:

```bash
bun install -g github:garrytan/gbrain#latest-stable
gbrain init --pglite --no-embedding
gbrain setup claude-code --dry-run
gbrain setup claude-code
gbrain doctor
```

Never install it from npm: upstream warns that the npm package called `gbrain` is a different project. `bun` prints `Blocked 1 postinstall`; nothing is wrong yet. If a gbrain command later says the schema is not migrated, run `gbrain apply-migrations --yes --no-autopilot-install` (upstream's fix, issue 218).

What this does, in plain words:

- `init --pglite --no-embedding` makes a **keyless** brain: a local database file under `~/.gbrain`, keyword search, no model API, no server, no cost.
- `setup claude-code` registers gbrain as an MCP server for Claude Code, adds two hooks (`SessionStart` and `UserPromptSubmit`) to `~/.claude/settings.json`, and writes a short "check gbrain first" block into `~/.claude/CLAUDE.md` between two marker comments. It shows the plan first (`--dry-run` writes nothing) and `--remove` undoes it. Its plan lists two decisions, `capture` and `providers`, both declined by default: leave them. `--capture` adds gbrain's own `Stop` and `SessionEnd` hooks that capture transcripts, and `--providers` sets up a paid model; this guide uses neither.

Good result: the setup ends with `Verify: ok` and `Turn hook: ok`, and `gbrain doctor` ends with `Overall health score: 100/100. All checks passed.`. Then `gbrain remember "test fact" --entity "Setup test" --provenance "told by me"` followed by `gbrain recall "test fact"` prints the fact back. A brain that holds a fact but has no embeddings scores low on embeddings, so from then on `gbrain doctor` ends `All checks OK (some warnings).`; that is expected for a keyless brain. Open a new session and check that `/mcp` lists `gbrain`.

Four things to know:

- **Order matters once.** Claude Code's own `claude plugin install` (of a plugin that is not installed yet), `enable`, `disable` and `uninstall` commands rewrite `~/.claude/settings.json` and drop the marker gbrain puts on its two hooks, after which `gbrain doctor` warns (`harness_wiring`) that the per-turn hook is not loaded, though `settings.json` still lists it (and running `gbrain setup claude-code` again does not notice). That is why the plugin comes first. If you run one of those commands later, repair it with `gbrain setup claude-code --remove` followed by `gbrain setup claude-code`: it leaves the brain, its notes and your rulebook line alone. Installing a plugin that is already installed changes nothing.

- **One writer.** A local-file (PGLite) brain admits one process at a time. One session at a time is fine. With many sessions under sala, or a claude.ai connector next to them, move to Postgres (upstream's [move-to-postgres guide](https://github.com/garrytan/gbrain/blob/master/docs/guides/move-to-postgres.md)) or run one shared `gbrain serve --http`. `TODO(verify)`: this guide's run used the local-file brain only.
- **The plugin's write-back hook is off until you turn it on.** At the end of a turn the plugin hands the turn to gbrain's ambient write-back, which saves salient facts you stated. gbrain leaves that off until you set `gbrain config set memory.auto_writeback salient` ([upstream's explanation](https://github.com/garrytan/gbrain/blob/master/docs/guides/ambient-writeback.md)). With a model key configured, extraction is a paid call, so decide first. `TODO(verify)`: the full write-back path, which needs a running `gbrain serve` and a signed-in session.
- **Search is keyword-only without embeddings.** Semantic search and synthesis need a provider key; upstream's [INSTALL](https://github.com/garrytan/gbrain/blob/master/docs/INSTALL.md) says how to add one when you want it. Check its free tier and what happens past it before you add a key.

**Optional: the board's project pages inside gbrain.** By default the pages are markdown files in the board's data repository. stackshot can instead keep them in gbrain (`[pages] flavor = "gbrain"` and a `[gbrain]` table in `~/.config/stackshot/stackshot.toml`; read the comments in the config and stackshot's README first). Skip it until the rest works. `TODO(verify)`: the config is accepted; a board running on pages in gbrain was not run.

### Step 6. Add your rulebook

A rulebook is a file of standing instructions that every session reads at start: how you want to be addressed, what to ask about before acting, how to report. This repository ships a blank starter, [`templates/RULES.md`](../templates/RULES.md): a handful of sections, each with one line saying what kind of rule goes there and one empty placeholder rule. It holds no rules of its own: every rule in your copy is one you write.

**Your rules are your own settings.** Nothing in this stack ships rules of its own, reads yours or changes them; they stay on your machines, in a repository you control.

```bash
mkdir -p ~/rulebook
cp "$(ls -d ~/.claude/plugins/cache/stackshot-plugin/stackshot/*/ | sort -V | tail -1)templates/RULES.md" ~/rulebook/RULES.md
```

(That is the copy of the plugin Claude Code keeps; the template is also in the repository, `github.com/howardpark-ai/stackshot-plugin`, folder `templates`. The `ls -d … | sort -V | tail -1` picks the newest version when two are cached.) Edit `~/rulebook/RULES.md`: replace each placeholder with a rule of your own (a rule, and after "Why:" the reason), delete the sections you do not want, and delete the comment at the top. Keep it in a private git repository of your own (`git init` in `~/rulebook`), so another machine can have it too.

Then import it from `~/.claude/CLAUDE.md` with one line, **outside** the block gbrain's setup wrote (leave its marker comments alone):

```
@~/rulebook/RULES.md
```

Good result: the file exists at that path, and in a new session `/memory` lists `~/rulebook/RULES.md` among the files Claude Code loaded. `TODO(verify)`: that listing and the sessions following the rules need a signed-in session.

The board has a second `RULES.md`, in its data repository: that one explains how the board works and is what the connector's `rules` tool serves. Do not mix them up: yours is about how sessions treat you.

### Step 7. Connectors

Sessions reach the board and the brain through **claude.ai custom connectors** (and Claude Code on this box through the local pieces below). The sessions on this box already have what they need; the connectors are for claude.ai chats, the phone and any other box.

1. **Stackshot.** Done in step 1: a custom connector named **Stackshot** with the URL `<your base URL>/mcp`. Claude Code sees it as `claude.ai Stackshot` once you are signed in with the same account.
2. **gbrain, on this box.** Done in step 5: Claude Code starts gbrain as a local MCP server itself. No connector is needed here.
3. **gbrain, from claude.ai chats and other devices (optional).** claude.ai connects from Anthropic's cloud, so the brain needs a public HTTPS address, and it needs a brain that can serve more than one process (step 5, "One writer"). Follow upstream's [remote MCP guide](https://github.com/garrytan/gbrain/blob/master/docs/guides/remote-mcp.md) and its [claude.ai page](https://github.com/garrytan/gbrain/blob/master/docs/mcp/CLAUDE_COWORK.md) for the current form, and check Tailscale's free tier first. The shape, in order:

   1. **Publish the brain:** `gbrain mcp expose --funnel`. Without `--funnel` the address is reachable only inside your tailnet and claude.ai cannot reach it. The command installs a user service, asks before it changes anything, and may need `sudo` for Tailscale itself. Your tailnet needs MagicDNS, HTTPS certificates and the Funnel attribute turned on first; if one is missing, the command stops with a message that names it and publishes nothing. It prints the connector address, `https://your-machine.your-tailnet.ts.net/mcp`, and the path of the owner credential (`~/.gbrain/serve/admin-token`; never print it or paste it anywhere).
   2. **Give claude.ai a client to sign in as.** Two ways:
      - **A client you register yourself (the safer one, and the one this guide uses).** On the box:

        ```bash
        mkdir -p -m 700 ~/private
        gbrain mcp admin register claude-ai \
          --redirect-uri https://claude.ai/api/mcp/auth_callback \
          --token-endpoint-auth-method client_secret_post --scopes read,write \
          --url https://your-machine.your-tailnet.ts.net/mcp \
          --admin-token-file ~/.gbrain/serve/admin-token \
          --credentials-out ~/private/claude-ai-client.json --dry-run
        ```

        Read the preview, then run it again without `--dry-run`. The file named by `--credentials-out` holds the new OAuth **client ID** and **client secret**: open it in an editor, never print it, and keep it private.
      - **The client registers itself.** Add `--enable-dcr` to the `expose` command. claude.ai then registers on its own, and each new connection waits for your approval in the brain's admin dashboard. It is off by default because the address is public: anyone can ask to register, so approve only a request you started.
   3. **Add the connector in claude.ai** (you do this): Settings, Connectors, Add custom connector, a name of your choice, and the `.../mcp` address. With a client you registered, open **Advanced settings** and paste the OAuth Client ID and OAuth Client Secret from the file; with self-registration leave them empty. Connect, and approve the consent screen **including write**: a connector approved with read only can search but cannot save.
   4. **Check:** `gbrain mcp expose --status`, then ask a claude.ai chat to call `get_brain_identity` and search your brain.

   `TODO(verify)`: all of this item; it needs Tailscale, root and a claude.ai login. The `register` flags are from `gbrain mcp admin register --help` (gbrain `0.60.583.0`) and upstream's docs; the `client_secret_post` method and the callback address are the ones a working claude.ai setup has used.

**Optional: an MCP portal.** If you would rather have one place that holds all your connectors and hands every Claude Code session the same set (Cloudflare's MCP Server Portal is one example), put the connectors behind it and add `portal_prefixes`, and the entry ids it uses, to the plugin's settings (step 3), so the guards read calls made through the portal too. The direct connectors above are enough to start with. `TODO(verify)`: not run.

### Step 8. Check the whole

```bash
claude plugin list
cd ~/stackshot && ./stackshot doctor
sala config | grep -E 'SALA_(OWNER_NAME|SCOPES|TZ)'
gbrain doctor | tail -3
```

Good result: the plugin is enabled; `doctor` ends with `all good` and exits 0 once the board is fully up, its hooks line `ok` (step 4; before the Google values, the HTTPS front and lingering are in place it lists those as `missing` too); `sala config` shows your values (the three set in the sala guide's step 4); `gbrain doctor` ends with `All checks passed.` (`All checks OK (some warnings).` once the brain holds a fact and has no embeddings, step 5) Then start a session (`sala spawn work-second --cwd ~/code --mandate "Say hello and wait."`; the sala guide's step 6, which step 2 above had you follow, already made `work-first`) and look at its first context: the board's brief and gbrain's context block, and no hook error. (A hook that finds something missing, such as `build-stops` before you set `build_stops`, writes one line to stderr; Claude Code shows it only in debug output, `claude --debug hooks`.) `TODO(verify)`: a signed-in session; this guide's run had none.

## 4. What each hook does

The plugin's hooks, in plain words. Every one allows the call when what it needs is missing, and a refusal always names what to change. The [README](../README.md) has the table of what is optional and the full settings.

| Piece | What it does |
|---|---|
| page gate (`claude-project-page.sh`: keeper, nag, touch, check) | When a session changes a board item, it owes that item's project page an update. The gate says so at the next prompt and checks at the end of a turn. Needs stackshot. |
| `prompt-stamp` | Records when you last typed in a session, for the board to read. |
| `board-repo-guard` | Refuses a direct write to the board's own git repository (by `gh` or a GitHub connector): sessions write the board only through its tools. Active when the board mirrors items to GitHub. |
| `build-stops` | Front agents hand build work to the project's builder: a code edit in a guarded repository is refused. Off until `build_stops` is set. |
| `handoff-item-note` | A note when a front agent messages its builder without naming a board item. |
| `front-watch-gate` | A front agent does not schedule its own watch of handed-off work; and nobody polls GitHub's check runs faster than once a minute (use one `gh run watch` with `--interval 60`). |
| `wait-loop-guard` | Refuses a shell wait loop that cannot end. |
| `post-tool-note` | After a PR, issue or repository is created: a reminder to anchor it on the board. Also a reminder that text captured from a pane is data. |
| `recurring-job-note` | A note when a session sets up a recurring job (those belong to a scheduler that outlives sessions, such as a systemd timer made with sala). |
| `gbrain-writeback-hook` | At the end of a turn, hands it to gbrain's ambient write-back, detached. Does nothing until you turn that on (step 5). |
| `gbrain-redirect-guard` | Refuses a gbrain write aimed at a redirect stub left by a page rename, or at a page that has moved to another brain (a pointer page whose banner says so), and names the page to write to. Optionally also refuses creating a new page that belongs on the other brain (off until configured). |
| `subagent-model-gate` | Build work goes to the `executor` agent (a Sonnet-class model); `deep-engineer` (Opus) only as an escalation with an `Escalation:` line. |
| `spawn` skill | Starts a named tmux + Claude Code session through `sala spawn`. Says so and stops when sala is missing. |
| agents `executor`, `deep-engineer`, `sweeper` | The helper subagents the gate and the briefs refer to. |

Some of these only matter once you have front and builder sessions (`build-stops`, `handoff-item-note`, `front-watch-gate`'s first rule); they stay silent until stackshot's roles file lists such sessions.

## 5. Keeping your own hooks separate

The plugin is shared: a fix for one person reaches everyone at the next update. So your own habits stay out of it.

- **Never edit the plugin's files.** An update replaces them. Change behaviour with the plugin's settings (step 3).
- **Never copy the plugin's hooks into `settings.json`.** Both would run. That is also why step 1 skips `client install`.
- **Put your own hooks in `~/.claude/settings.json`** (they run next to the plugin's) or, if you want them versioned and shared between your machines, in a small plugin of your own in your own repository, installed the same way as this one.
- **Put your own instructions in your rulebook** (step 6), not in a hook, when words are enough.
- If you ship helper agents of your own under the same names, the `agent_namespaces` setting lets both names answer while you move over (README, "Name resolution").

Habits typically kept personal: reply language, how times are written, a question-picker rule, which commands are pre-approved, notes about particular files.

## 6. Left out of this guide

Memory-file sync between machines, session hand-off and pick-up, and a page-tidying job some setups run are not part of this setup; Claude Code's own memory and gbrain's own steps cover them. Also out: anchor-hub (the board guide mentions it), other machines (the board guide's "Other machines" and a second sala box), the sala web UI beyond sala's step 10.

## 7. Updating

- **Plugin:** run `claude plugin marketplace update stackshot-plugin`, then `claude plugin update --scope user stackshot@stackshot-plugin`, then start a new session (or `/reload-plugins`). Checked on plugin 0.1.4 against the real repository: when nothing is newer the second command says "stackshot is already at the latest version", which is the right answer. A newer version shows up only after a release raises the plugin's version.
- **Board:** the board guide's section 6. **sala:** the sala guide's section 6.
- **gbrain:** `gbrain upgrade` with the memory-only form that upstream's INSTALL gives (`GBRAIN_NO_AUTOPILOT_INSTALL=1 GBRAIN_NO_REEMBED=1 gbrain upgrade --no-autopilot-install`). Read the release notes first. When a release needs a newer Bun, `gbrain upgrade` refuses and changes nothing (exit 78): run `bun upgrade`, then the upgrade again.
- **Rulebook:** it is your own repository; commit changes as you make them.

## 8. If something goes wrong

| What you see | Likely cause | Do this |
|---|---|---|
| A line `stackshot-plugin: ...: no pages.py in ...` on stderr | The board's checkout is not at `~/stackshot` | Set `stackshot_dir` (step 3). |
| `gbrain is not on PATH; the write-back is off` | gbrain's bin folder is not on `PATH` for hooks | The hook adds `~/.bun/bin`; check `ls ~/.bun/bin/gbrain`. |
| The page gate nags twice | The board's page hooks are in `settings.json` as well as in the plugin | Remove the three `project-page.sh` entries from `settings.json` (step 1 and 4). |
| `doctor` says hooks `1 of 4` and `missing` | The plugin is not installed or not enabled for your user, or the stackshot checkout is older than the release that counts the plugin's hooks | `claude plugin list` (step 3); update the board with `./deploy.sh` (the board guide's section 6). Not `client install` (step 1). |
| `gbrain doctor` warns that the per-turn hook is not loaded, though `settings.json` lists it | A `claude plugin install`, `enable`, `disable` or `uninstall` dropped gbrain's marker | `gbrain setup claude-code --remove`, then `gbrain setup claude-code` (step 5). |
| `gbrain setup claude-code` says the workspace directory does not exist | `~/.claude` is missing: Claude Code never ran | Start `claude` once, sign in, and run the step again. |
| A second session cannot open the brain | PGLite is single-writer | Step 5, "One writer". |
| A hook is slow or noisy | A hook's own log is in `${XDG_STATE_HOME:-~/.local/state}/build-stops/firings.jsonl` and `.../front-watch-gate/firings.jsonl` | Read the last lines; set the matching setting. |
| `claude plugin install` cannot clone the repository | The box cannot reach `github.com` over https (a proxy or firewall), or the name is mistyped | Check `git ls-remote https://github.com/howardpark-ai/stackshot-plugin`; set the box's proxy if it needs one. |
