# What stays personal

Habits that are one person's choice are not part of the stack and are not in this plugin: the time and reply-language notes, the
question-picker gate, connector permission and write notes, the safe-command list, the cache-file note, memory triage, the rulebook
hints, the page-shape guard, the portal write-return note and the guard for the owner's own notes repository. Two more are left out
for now: the role-based gate for a background builder's main thread (its rule is the fleet's own), and a Claude Code hook that
checks the text of a push or a PR (its deny list is instance data; the git hooks in `.githooks/` do the same on any machine, see
`CONTRIBUTING.md`).
