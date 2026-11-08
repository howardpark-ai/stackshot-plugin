# Tests and CI

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
