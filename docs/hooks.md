# The pieces of the plugin

The shared Claude Code pieces of the stackshot stack in one plugin: the hooks that make the board, sala and gbrain work together, the
`spawn` skill, and three helper subagents. They depend on each other (most load stackshot's own code, two share sala's session
lookup), so they ship together and install with one command.

| Piece | What it does |
|---|---|
| `board-repo-guard` | Refuses a direct write to the board's GitHub repo (by `gh` or the GitHub connector, also through a portal's Code Mode; with `[github] issues_repo_id` set, also `gh api` on `repositories/<id>/…`); sessions write the board only through stackshot's tools. |
| `claude-project-page.sh` | The project-page gate's entry (`keeper`, `nag`, `touch`, `check`): forwards to stackshot's `pages.py`. |
| `build-stops` | A front agent hands build work to its project's builder: a code edit in a guarded repository is refused, a commit, push or merge there gets a warning, and a merge outside the release script tells the maintainer. |
| `handoff-item-note` | A note (never a block) when a front agent messages its project's builder with no item reference in the text: file the item first and hand it over by id, with the user-visible result and what done means. Covers `SendMessage` and stackshot's message tool, direct or in a portal script; a message marked `fyi` is skipped. |
| `front-watch-gate` | A front agent does not schedule its own watch of handed-off work; and no agent polls GitHub's check runs faster than once a minute. |
| `wait-loop-guard` | Refuses a shell wait loop that can never end (a self-matching `pgrep -f`, an unbounded `while`/`until` with a `sleep`). |
| `no-ask-watch` | The Stop hook for front agents: a turn that nobody typed (a peer note, a task notification) and that ended with a sign-off such as "Nothing here needs you" is a leak of something that did not need the agent. Each one is posted to stackshot as a `catch` record (`client.py post catch -`, the body on stdin), so whoever fixes the source can trace it. Never blocks; with no stackshot checkout it keeps a local log and writes one stderr line per catch. `no-ask-watch.py --replay` posts the log later. |
| `prompt-stamp` | Stamps when the owner last typed a prompt in a session, for stackshot to read. |
| `post-tool-note` | After a PR or an issue is created on GitHub, anchors it on the board itself through stackshot's machine client (`client.py post anchor-add -`) and prints one line saying what was anchored and to which item, or project-wide. The item is picked only when exactly one open item names the session as its mover and that item has exactly one open step of the session's own (`client.py get open-records <session>`); an item that is waiting (a stored wait, or a phase that is not alive) or belongs to another project never counts. With none or several the anchor is project-wide, and the line says which case applied; if the server refuses the item with a 400, the add is made once more project-wide. The project is the one in the session's roles entry. The hook anchors a URL only when the create call itself printed it: the command line ends in `gh pr create` or `gh issue create` as a real command (not inside quotes, a heredoc or a comment) and the last line of its stdout is exactly the URL, or a connector call's result holds exactly one pull request or issue URL; otherwise it prints the reminder with that reason ("the hook could not tell from the call's shape or output that the link came from the create call itself"). A conditional (`||`), backgrounded or otherwise unusual shell form is never trusted: only plain commands (`cd`, `git`, `echo` and the like) may come before the create, and nothing after it. A URL is anchored once per session. When anything fails (no roles entry, no client or token, server not reached, a refusal, a timeout) it prints the older reminder to anchor by hand, with the reason after it. A created repo or branch gets the reminder alone. Also a reminder that text captured from a pane is data, and, when the capture has no `-e`, that text after the prompt sign may be Claude Code's dim suggested prompt (a plain capture drops the dimming). |
| `recurring-job-note` | A note when a session sets up a recurring job (those belong to a scheduler that outlives sessions); optionally also on polling listed hosts. |
| `gbrain-writeback-hook` | The Stop hook that hands the turn to gbrain's ambient memory write-back, detached. |
| `gbrain-redirect-guard` | Refuses a gbrain write aimed at a redirect stub left by a page rename, or at a page that has moved to another brain (a pointer page whose banner says so), and names the page to write to. Optionally also refuses creating a new page that belongs on the other brain (off until configured). The lookup is a local `psql` or the gbrain CLI, or a command of your own for a brain whose database is elsewhere (`gbrain_lookup_command`). |
| `subagent-model-gate` | Build work goes to the `executor` agent (Sonnet); `deep-engineer` (Opus) only as an escalation with an `Escalation:` line. A `fable` model override passes only for design and analysis, on `general-purpose` or `claude` with a `Design:` line. |
| `spawn` skill | Spawns a named tmux + Claude Code session through `sala spawn`. |
| agents `executor`, `deep-engineer`, `sweeper` | The helper subagents the gates and the briefs refer to ([below](#the-helper-agents)). |

## The helper agents

All three start small: without CLAUDE.md files and without tools such a helper has no use for. Both are fields of the agent file
that Claude Code's [subagent page](https://code.claude.com/docs/en/sub-agents) describes.

**No CLAUDE.md files** (`omitClaudeMd: true`, Claude Code 2.1.271 or later; an older version ignores the field and loads them). The
user, project and local CLAUDE.md files, and what they import, are left out of the agent's starting context. Managed policy files
still load, and so do a subdirectory's `CLAUDE.md` and matching path-scoped rules once the agent works with files there. Each agent's
own instructions tell it to follow a rules note given at its start; `executor` and `deep-engineer` are also told to read a
repository's `CLAUDE.md`, `AGENTS.md` or `CONTRIBUTING` file before changing anything there.

**Some tools left out** (`disallowedTools`). An agent gets the tools Claude Code gives a subagent in your session, minus these:

| Left out | What the agent cannot do |
|---|---|
| `Skill` | Invoke skills. Its start carries no list of them either (seen on Claude Code 2.1.332; the documentation says only that the agent cannot invoke them). |
| `Agent`, `Workflow` | Start agents of its own. |
| `SendMessage`, `PushNotification`, `AskUserQuestion` | Send messages, notify you or ask you. It reports to the session that called it. |
| `Artifact` | Publish artifacts. |
| `ScheduleWakeup`, `CronCreate`, `CronDelete`, `CronList`, `RemoteTrigger` | Schedule work. |
| `EnterPlanMode`, `ExitPlanMode`, `EnterWorktree`, `ExitWorktree` | Switch itself into plan mode or into a worktree. |

- Everything else stays, among them `Read`, `Edit`, `Write`, `NotebookEdit`, `Bash`, `WebFetch`, `WebSearch`, `ToolSearch`,
  `Monitor` and `TaskStop`, and the tools of your MCP servers and connectors.
- Claude Code already withholds some of the names above from subagents on its own, from background ones most of all
  ([available tools](https://code.claude.com/docs/en/sub-agents#available-tools)); for those the line changes nothing.

**To give them your rules**, add a [`SubagentStart` hook](https://code.claude.com/docs/en/hooks#subagentstart) to your own settings
that returns them as `additionalContext`, with the matcher `^stackshot:(executor|deep-engineer|sweeper)$`; the plugin ships no such
hook. A rule that matters for one job can also go into the brief.

**To change their tools**, copy the agent file into `~/.claude/agents/` (or a project's `.claude/agents/`), edit its
`disallowedTools` or `omitClaudeMd` line, and call the copy by its own name, without the `stackshot:` prefix. Remove a name to give
a tool back. To narrow further, add names: `mcp__<server>` leaves out one MCP server's tools and `mcp__*` leaves out all of them
([restrict which tools a subagent can use](https://code.claude.com/docs/en/sub-agents#restrict-which-tools-a-subagent-can-use)), or
set a `tools` allowlist. A plugin update replaces the plugin's files, and your copy does not change the `stackshot:` agent. A copy
does not follow later plugin versions.

## How `post-tool-note` reads a command

The hook never looks at the exit status of the command it reads (a command that fails fires no PostToolUse hook at all, so every command it sees exited 0), which leaves the shell scanner as the only check that the link on the last line of the output is the one the create printed. The scanner therefore has to read the command exactly as bash does, and anything it does not model in full is untrusted: the whole command gets the reminder to anchor by hand and nothing is anchored. It models quotes and escapes, `$(...)` and backticks, heredocs (a delimiter word that ends where bash ends it, a closing line that is exactly the delimiter), comments that start a word, the separators `&&`, `;`, `|` and a newline, and a backslash-newline that follows a blank. A `||` or a lone `&`, a `(` or `((`, a delimiter word that goes on, a heredoc that is never closed, a backslash-newline that would join two tokens, a NUL byte, a command longer than 200,000 characters and `$(` nested too deep are all untrusted.

The create itself is read against a short whitelist (`words_are_literal`), because the scanner trusts only a few boring shapes. It must be a call that prints its link: no `--web`, `-w`, `--dry-run` or help flag, read after quotes and escapes are removed (`"--web"` and `--we\b` are `--web`). A word of one dash whose leading letters hold a `w` or an `h` is read as quiet whatever follows those letters (`-w`, `-dw`, and also `-wt1`, `-hb1`, `-dwt1`, `-wH.`): gh lets a flag that takes a value swallow the rest of its word, so the scanner cannot tell where the flags end and a value starts, and it errs toward untrusted. Every flag word must be one of the create's own flags, as `gh pr create --help` and `gh issue create --help` list them (the table `GH_FLAGS` in the hook; a test fails when the installed gh lists one the table lacks), and a flag that takes a value takes the NEXT word, as gh does: a long flag without `=`, or a cluster of short flags whose last letter is a value letter (`-m`, `-p`, `-r`, `-T`, `--type`, `-dt` and the like, not only the eight below). So in `gh pr create -m -t "-w"` and `gh pr create -dt -b "-w"` the quoted `-w` is a flag of its own and the command is untrusted; a flag word the table does not know (`--bogus`, `-x`, `--website`, `gh issue create --fill`) makes the command untrusted too. The word `create` must stand alone between plain blanks (space or tab; a form feed, a carriage return or a no-break space is part of a word for bash). Outside single quotes a word of the create holds only plain characters, escapes and quoted parts, and none of `< > ( ) ; & | ~ * ? [ ] { } !`, a backtick, `$@`, `$*` or a NUL byte (the word `case` is refused only inside `$(...)` or a backtick substitution, see the next paragraph; `echo case; gh pr create --fill` stays trusted). The only redirections are the whole words `2>&1` and `2>/dev/null` and a `<<'WORD'` opener (for `--body-file -`). The one place an expansion may stand is inside double quotes in the value of a flag that takes one (`--title`/`-t`, `--body`/`-b`, `--body-file`/`-F`, `--repo`/`-R`, `--base`/`-B`, `--head`/`-H`, `--label`/`-l`, `--assignee`/`-a`, as the next word or the rest of the same word): `$name` and `${name}` there, and `$(...)` only as the whole value `"$(cat <<'WORD' ... WORD)"`. So `--body "$(cat <<'EOF' ... EOF)"`, `--title "x $Y"`, a literal `--fill` and `cd dir && git push && gh pr create --title t --body b` stay trusted, and a trailing `# comment` is read as bash reads it (it ends the line) and does not untrust the create.

Shapes the whitelist refuses, though real commands (the whole command then gets the reminder to anchor by hand): a redirect of the create's input or output (`< f`, `> f`, `>&2`, `<<EOF` unquoted, `<<<x`, `&>`) or one glued to a word (`--title t --web</dev/null`); a leading `~` or `~/x`; a glob (`*`, `?`, `[`) or a brace list; `!`; `$name`, `$(...)` or a backtick outside the value of a flag that takes one, or unquoted in a value; `$(...)` or a backtick inside a value other than the heredoc body (`--title "$(git log -1 --format=%s)"`); `"$@"` and `"$*"` anywhere; and a `$(...)` or backtick substitution anywhere in the command that holds the bare word `case`, because a case pattern's `)` would end the substitution early in the scanner.

Known limits, kept on purpose: the scanner trusts only a short whitelist, not everything gh accepts, so it does not try to tell every word gh would reject from one it would take. A stray positional word (`gh pr create --fill x`), a flag of the create placed before `pr` (`gh -t x pr create`) and a lone backslash at the very end of the command (bash hands it to gh as a word of its own) are read as trusted. Each of them makes gh itself exit non-zero, and a command that fails fires no PostToolUse hook, so the hook never sees them; nothing is anchored from such a call. A `PATH=... gh pr create` prefix (a `NAME=value` word before `gh`) is trusted, and so is a create run through an alias or a function of the session's shell that redefines `gh`, `echo`, `cat`, `ls` or `git`: the scanner reads the command text, not the shell it runs in, and cannot see either of them.
