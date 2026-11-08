# The pieces of the plugin

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
