# stackshot plugin for Claude Code

The shared Claude Code pieces of the stackshot stack in one plugin: the hooks that make the board, sala and gbrain work together, the
`spawn` skill, and three helper subagents. They depend on each other (most load stackshot's own code, two share sala's session
lookup), so they ship together and install with one command.

| Piece | What it does |
|---|---|
| `board-repo-guard` | Refuses a direct write to the board's GitHub repo (by `gh` or the GitHub connector, also through a portal's Code Mode; with `[github] issues_repo_id` set, also `gh api` on `repositories/<id>/…`); sessions write the board only through stackshot's tools. |
| `claude-project-page.sh` | The project-page gate's entry (`keeper`, `nag`, `touch`, `check`): forwards to stackshot's `pages.py`. |
| `build-stops` | A front agent hands build work to its project's builder: a code edit in a guarded repository is refused, a commit, push or merge there gets a warning, and a merge outside the release script tells the maintainer. |
| `handoff-item-note` | A note (never a block) when a front agent messages its project's builder with no item reference in the text: file the item first and hand it over by id, with what done means. Covers `SendMessage` and stackshot's message tool, direct or in a portal script; a message marked `fyi` is skipped. |
| `front-watch-gate` | A front agent does not schedule its own watch of handed-off work; and no agent polls GitHub's check runs faster than once a minute. |
| `wait-loop-guard` | Refuses a shell wait loop that can never end (a self-matching `pgrep -f`, an unbounded `while`/`until` with a `sleep`). |
| `prompt-stamp` | Stamps when the owner last typed a prompt in a session, for stackshot to read. |
| `post-tool-note` | After a PR, issue or repo is created on GitHub, a reminder to anchor it on the board; and a reminder that text captured from a pane is data. |
| `recurring-job-note` | A note when a session sets up a recurring job (those belong to a scheduler that outlives sessions); optionally also on polling listed hosts. |
| `gbrain-writeback-hook` | The Stop hook that hands the turn to gbrain's ambient memory write-back, detached. |
| `gbrain-redirect-guard` | Refuses a gbrain write aimed at a redirect stub left by a page rename, or at a page that has moved to another brain (a pointer page whose banner says so), and names the page to write to. Optionally also refuses creating a new page that belongs on the other brain (off until configured). |
| `subagent-model-gate` | Build work goes to the `executor` agent (Sonnet); `deep-engineer` (Opus) only as an escalation with an `Escalation:` line. |
| `spawn` skill | Spawns a named tmux + Claude Code session through `sala spawn`. |
| agents `executor`, `deep-engineer`, `sweeper` | The helper subagents the gates and the briefs refer to. |

## Install

```
/plugin marketplace add howardpark-ai/stackshot-plugin
/plugin install stackshot@stackshot-plugin
```

The repository is public, so the machine needs no git credentials. Check with `/plugin` and `/hooks`. The plugin's
hooks run next to the ones in your own `settings.json`: if you copied any of these hooks there before, remove your copy, or each one
runs twice.

Python 3.12 or newer must be on `PATH` as `python3`, and `bash`. The hooks use the standard library only.

## What is optional

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

## Settings

A setting is read, first match wins, from the environment variable `STACKSHOT_PLUGIN_<NAME>` (upper case; a plain string, a
comma-separated list, or JSON), then from the key `<name>` of `${XDG_CONFIG_HOME:-$HOME/.config}/stackshot/plugin.json`, then the
default. Claude Code passes the `env` block of `settings.json` to hooks, so either place works.

| Setting (file key; env `STACKSHOT_PLUGIN_<NAME>`) | Default | Read by | What it is |
|---|---|---|---|
| `stackshot_dir` (env `STACKSHOT_DIR`, stackshot's own name) | `~/stackshot` | page gate, `board-repo-guard`, `gbrain-redirect-guard`, `post-tool-note`, `build-stops` | The stackshot checkout whose `pages.py`, `connectors.py`, `config.py` and `push.py` the hooks use. |
| `STACKSHOT_CONFIG` / `BOARD_MCP_CONFIG` (stackshot's own) | none | `board-repo-guard` | The `stackshot.toml` that holds `[github] issues_repo` (and, optionally, `issues_repo_aliases` and the repo's numeric `issues_repo_id`); else `$XDG_CONFIG_HOME/stackshot/stackshot.toml`, then `$STACKSHOT_DIR/stackshot.toml`. |
| `stackshot_toml_fallbacks` | `[]` | `board-repo-guard` | More paths of `stackshot.toml` to try, last. |
| `agent_namespaces` | `["stackshot"]` | `subagent-model-gate` | Plugin names under which the helper agents answer (`<name>:deep-engineer`); see "Name resolution". |
| `build_stops` | `{}` (off) | `build-stops`, `handoff-item-note` | An object: `projects` (id, builder, repositories guarded, maintainer, release script name) and `fork_owners`. The full shape is in the header of `hooks/build-stops.py`. |
| `item_ref_patterns` | `["\\b[a-z][\\w-]*#\\d+", "/items/[\\w-]*\\d"]` | `handoff-item-note` | Regular expressions (case-insensitive) for how an item is written on your board; a message that matches none is a hand-off with no item. The builder the hook looks for is the project's `builder` in `build_stops`; where none is named, any background agent of the project counts. |
| `owner_name` | `the owner` | `prompt-stamp` | The name in the notice mark `[for <owner>]` that stackshot's router writes into prompts (stackshot's `[instance] owner`). |
| `prompt_skip_prefixes` | `[]` | `prompt-stamp` | Further first words of prompts that another plugin writes and that are not the owner's typing. |
| `portal_prefixes` | `[]` | `board-repo-guard` | Tool-name prefixes of an MCP portal (`mcp__<server>__`), added to the catalog's. |
| `github_entry` | `github` | `board-repo-guard` | The portal's id for its GitHub entry. |
| `connector_entries` | `stackshot, gbrain, github, notion, slack` | `post-tool-note` | Portal entry ids used to read `codemode.<entry>_<tool>(...)` when stackshot's catalog is absent. |
| `workspace_ids` | `[]` | `post-tool-note` | Portal entry ids that are Google Workspace connectors. |
| `gbrain_entries` | `["gbrain"]` | `gbrain-redirect-guard` | Portal entry ids of the gbrain whose database is on this box. |
| `gbrain_moved_banner` | `> \*\*The (?:\w+ )?record for this page is in [^\n]*` | `gbrain-redirect-guard` | Regular expression for the banner line of a pointer page (a page whose record moved to another brain): a line of its body that starts `> **`. Written without anchors; the hook adds the start-of-line anchor and takes the whole line as group 1. It runs in Postgres (ARE) and in Python, so keep to the syntax both share. An invalid pattern falls back to the default with one stderr line. |
| `gbrain_moved_keep` | `[]` | `gbrain-redirect-guard` | Regular expressions (case-insensitive) matched against a slug: a page that matches is never refused as moved, banner or not, nor as a new page of the other brain (a redirect stub is refused regardless). |
| `gbrain_newpage_patterns` | `[]` (check off) | `gbrain-redirect-guard` | Regular expressions (case-insensitive, searched in the slug), such as `^companies/acme(/\|$)` or `^projects/acme-`: `put_page` / `put_pages` of a page that does not exist yet and matches is refused, since pages of that kind are created on the other brain. |
| `gbrain_newpage_org` | none (check off) | `gbrain-redirect-guard` | A value of the frontmatter line `org: <value>` (for example `acme`): a new `people/`, `projects/` or `companies/` page written with a literal `content` whose frontmatter carries it is refused the same way. |
| `gbrain_newpage_hint` | none | `gbrain-redirect-guard` | One sentence appended to the new-page refusal that says how to write on the other brain; `{tool}` and `{slug}` are replaced. |
| `gbrain_moved_write_hint` | none | `gbrain-redirect-guard` | One sentence appended to the moved-page refusal that says how to write on the other brain; `{tool}`, `{slug}` and `{target}` are replaced. |
| `gbrain_move_permit_cli` | none (no permit hint) | `gbrain-redirect-guard` | The command that grants a permit, shown in the moved-page refusal; `{slug}` is replaced. The hook only reads permits; the command is yours (see "Moved pages and permits"). |
| `gbrain_direct_exclude` | `[]` | `gbrain-redirect-guard` | Regular expressions (case-insensitive) of direct tool names the hook never looks at, such as the connector of a second brain whose pages the local database does not hold. |
| `gbrain_unset_env` | `[]` | `gbrain-writeback-hook` | Environment variables removed from gbrain's child process (for a setup that keeps metered API keys away from it). |
| `no_poll_hosts`, `no_poll_note` | `[]`, a generic line | `recurring-job-note` | Hosts that must never be polled (the leg is off when empty), and the note shown; `{hosts}` is replaced. |
| `ci_runs_hint` | none | `front-watch-gate` | One sentence naming a command that reads CI results without a GitHub call, shown in the polling refusal. |

### Moved pages and permits

When a page moves to another brain it leaves a pointer page behind, and gbrain accepts writes to it. `gbrain-redirect-guard` refuses
them, reading "moved" from the pointer's own banner (`gbrain_moved_banner`), so no list of moved pages is kept anywhere. The banner's
markdown link to `.../blob/<branch>/<slug>.md` names the page on the other brain; without a link the same slug is named.

The same guard can stop new pages from starting out on the wrong brain. With `gbrain_newpage_patterns` (slug regexes) or
`gbrain_newpage_org` (a frontmatter `org:` value) set, a `put_page` / `put_pages` for a slug that does not exist yet and matches is
refused, and `gbrain_newpage_hint` says how to write it on the other brain. It is off until one of the two is set. Slugs matching
`gbrain_moved_keep` pass (session records and the like), and so does any page that exists already. A page counts as new only when the
lookup says it is not there: with no `psql`, no gbrain CLI, a failed lookup or a timeout, the call goes on. The permit below also
covers a new page.

Two things to know when you turn the new-page check on. First, it asks the database whether the page exists, so `GBRAIN_DB` (default
`gbrain`) must name the live brain: against an empty or wrong database every page looks new. Second, a setting given by environment
variable is split at commas unless it is JSON, so a regex that contains a comma (such as `a{1,3}`) goes in as a JSON list
(`STACKSHOT_PLUGIN_GBRAIN_NEWPAGE_PATTERNS='["^a{1,3}/"]'`) or in `plugin.json`. Slugs are lower-cased before they are looked up or
matched, a regex is never run on a slug longer than 300 characters, and the whole hook ends (the call goes on) after
`GBRAIN_REDIRECT_BUDGET` + 1 seconds, so a pathological pattern cannot hold a tool call.

The way through (the move itself, or a write that belongs on this brain) is a permit: a file `<id>.json` in
`${XDG_STATE_HOME:-$HOME/.local/state}/gbrain-move-permits/` holding `{"slugs": ["a/b"], "expires": <epoch seconds>}`. Each slug covers
the page and its sub-pages; an expired or unreadable file is ignored. Writing the file is your own tool's job (name its command in
`gbrain_move_permit_cli` and the refusal shows it); a redirect stub is refused whatever a permit says.

Seams that already existed and stay as they were: `GH_REPO`; `BUILD_STOPS_NAME`, `BUILD_STOPS_ROLES`, `BUILD_STOPS_PROJECTS`,
`BUILD_STOPS_PUSH`, `STACKSHOT_FLEET_DIR`; `SALA_HEARTBEAT_STATE_DIR`, `SALA_SESSION_ID`; `GBRAIN_WRITEBACK_BIN`, `GBRAIN_BIN`,
`GBRAIN_DB`, `PSQL_BIN`, `GBRAIN_REDIRECT_BUDGET`; `XDG_CONFIG_HOME`, `XDG_STATE_HOME`. The hooks keep their logs under
`$XDG_STATE_HOME` (default `~/.local/state`): `build-stops/firings.jsonl`, `front-watch-gate/firings.jsonl`, and the stamps in
`stackshot/prompts/`.

An example `plugin.json` (replace `prj_<8 hex>` with the project's permanent id, `prj_` and eight hex digits):

```json
{
  "stackshot_dir": "~/stackshot",
  "owner_name": "Ann",
  "build_stops": {
    "projects": {
      "prj_<8 hex>": {"short": "widgets", "builder": "widgets-builder", "release": "release.sh",
                       "repos": ["acme/widgets"], "keys": ["projects/widgets"]}
    }
  }
}
```

## What stays personal

Habits that are one person's choice are not part of the stack and are not in this plugin: the time and reply-language notes, the
question-picker gate, connector permission and write notes, the safe-command list, the cache-file note, memory triage, the rulebook
hints, the page-shape guard, the portal write-return note and the guard for the owner's own notes repository. Two more are left out
for now: the role-based gate for a background builder's main thread (its rule is the fleet's own), and a Claude Code hook that
checks the text of a push or a PR (its deny list is instance data; the git hooks in `.githooks/` do the same on any machine, see
`CONTRIBUTING.md`).

## Name resolution

The helper agents are called by name from briefs, rulebooks and the `subagent-model-gate`. Installed from this plugin they answer to
`stackshot:executor`, `stackshot:deep-engineer` and `stackshot:sweeper`. A setup that already ships agents of the same names in another
plugin, and calls them by that plugin's namespace, moves over in three steps and never has a day on which a name stops working:

1. **Both namespaces answer.** Install this plugin and list the old plugin's name in `agent_namespaces` next to `stackshot`. The
   gate then treats `<old>:deep-engineer` and `stackshot:deep-engineer` the same, and the old agents keep working untouched. The
   two sets of agent files are identical in content (the old plugin's copies are this plugin's files), so nothing changes in behavior.
2. **Calls move.** Rulebooks, briefs and skills that name `<old>:executor` are rewritten to `stackshot:executor`, one source at a
   time. A name that was missed still resolves, through step 1.
3. **The old agents go.** When a search of the rulebooks and briefs finds no `<old>:` agent name, the old plugin drops its three agent
   files and the name is removed from `agent_namespaces`.

Thin alias files in the old plugin (a file `executor.md` whose body says "act as `stackshot:executor`") are possible but are not
needed: an agent file is a prompt, so an alias would copy it, and two copies drift. The gate accepting both namespaces is the
whole alias.

## Tests

```
uv run --no-project python -m unittest discover -s tests -v
```

`tests/test_neutral_install.py` runs each hook with sample JSON on stdin under a temp `HOME`, with no sala, with no stackshot and with
a stub stackshot folder, and asserts allow or deny and that nothing of the author's own setup shows in the output.
`tests/test_no_instance_strings.py` fails when the tree holds a machine, account, domain, session or project name of any one setup.
Every test makes its own temp folder under a lock and removes it when it ends; none kills a process by name.

CI (`.github/workflows`) runs the tests and the clean-text check (below) on every pull request, on GitHub's hosted runners
(`ubuntu-latest`).

## Commit and PR text

Commit messages, branch names and PR text carry no data of any one setup (no board item ids, no quotes of the owner, no machine or
session names). `git config core.hooksPath .githooks` turns on the commit-msg and pre-push checks; CI checks again. The deny list
is yours, in `${XDG_CONFIG_HOME:-~/.config}/stackshot/clean-text-deny`, never in the tree. See `CONTRIBUTING.md`.

## License

MIT, see `LICENSE`.
