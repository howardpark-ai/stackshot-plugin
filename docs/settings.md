# Settings

A setting is read, first match wins, from the environment variable `STACKSHOT_PLUGIN_<NAME>` (upper case; a plain string, a
comma-separated list, or JSON), then from the key `<name>` of `${XDG_CONFIG_HOME:-$HOME/.config}/stackshot/plugin.json`, then the
default. Claude Code passes the `env` block of `settings.json` to hooks, so either place works.

| Setting (file key; env `STACKSHOT_PLUGIN_<NAME>`) | Default | Read by | What it is |
|---|---|---|---|
| `stackshot_dir` (env `STACKSHOT_DIR`, stackshot's own name) | `~/stackshot` | page gate, `board-repo-guard`, `gbrain-redirect-guard`, `post-tool-note`, `build-stops` | The stackshot checkout whose `pages.py`, `connectors.py`, `config.py` and `push.py` the hooks use. |
| `STACKSHOT_CONFIG` / `BOARD_MCP_CONFIG` (stackshot's own) | none | `board-repo-guard` | The `stackshot.toml` that holds `[github] issues_repo` (and, optionally, `issues_repo_aliases` and the repo's numeric `issues_repo_id`); else `$XDG_CONFIG_HOME/stackshot/stackshot.toml`, then `$STACKSHOT_DIR/stackshot.toml`. |
| `stackshot_toml_fallbacks` | `[]` | `board-repo-guard` | More paths of `stackshot.toml` to try, last. |
| `agent_namespaces` | `["stackshot"]` | `subagent-model-gate` | Plugin names under which the helper agents answer (`<name>:deep-engineer`); see "[Name resolution](name-resolution.md)". |
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

## Moved pages and permits

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
