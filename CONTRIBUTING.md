# Contributing

## Commit and PR text

Commit messages, branch names, PR titles and PR bodies carry nothing of any one instance owner's data: no board item ids
(`board#N`, a link to a board's `items/<n>` page), no quotes of the owner, no machine or session names, no personal addresses or
domains. Write the change in plain engineering terms. The link between a PR and a board item lives on the board, not in the text
here. A push to a public repository publishes every commit it carries, so the check runs before the text leaves the machine.

Turn the local checks on once per clone:

    git config core.hooksPath .githooks

- `.githooks/commit-msg` checks the message of every commit.
- `.githooks/pre-push` checks the branch name and the new commit messages of a push.
- CI (`.github/workflows/clean-text.yml`) checks the PR title, body, head branch and commit messages again, as a backstop.

All three call `tools/clean_text.py`. It always runs the generic patterns (board ids). Your own words to keep out (machine names,
session names, domains, your name) are a list of regular expressions, one per line, case-insensitive, that is never written into
the repository: put them in `${XDG_CONFIG_HOME:-$HOME/.config}/stackshot/clean-text-deny` or in the environment variable
`CLEAN_TEXT_DENY`; in CI the repository secret of that name. Without a list only the generic patterns run, and the check says so.

## Code and files

The tree carries no instance values either: every value that differs per owner (machine names, session names, accounts, domains,
connector entries) is a setting with a neutral default, read from an environment variable or from
`${XDG_CONFIG_HOME:-$HOME/.config}/stackshot/plugin.json` (see the README). `tests/test_no_instance_strings.py` fails on a stray one.

## Tests

    uv run --no-project python -m unittest discover -s tests -v

Each test makes its own temporary folder under a lock and removes it when it ends, pass or fail. A test never kills a process by
name and never touches the real home directory.

## Releasing a change

A change reaches an installed copy only when the version changes. `claude plugin update` compares the declared version, so a merge
alone leaves installed copies on the old one, with no error. Every PR that changes what ships (hooks, skills, agents, templates,
manifests) bumps `version` in both `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`, in that PR or in a follow-up
merged straight after it.

To update an install, run these two commands in this order:

    claude plugin marketplace update stackshot-plugin
    claude plugin update --scope user stackshot@stackshot-plugin

With more than one Claude config folder, do this once per folder, with `CLAUDE_CONFIG_DIR=<folder>` set on both commands. Use only
folders that already exist: pointing `CLAUDE_CONFIG_DIR` at a new path creates it.

A running session has the new hook only after its restart or `/reload-plugins`.

## License

MIT. A contribution comes in under the same license.
