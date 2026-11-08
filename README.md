# stackshot plugin for Claude Code

The shared Claude Code pieces of the stackshot stack in one plugin: hooks, a skill and three helper subagents. They depend on each
other, so they ship together and install with one command. Every hook that needs stackshot, sala or gbrain works without it: it
allows the call, writes one line to stderr and goes on, so the plugin never fails a session.

## Quick start

```
/plugin marketplace add howardpark-ai/stackshot-plugin
/plugin install stackshot@stackshot-plugin
```

You need `bash` and Python 3.12 or newer as `python3`. Check the install with `/plugin` and `/hooks`. More detail is in
[docs/install.md](docs/install.md).

## The main parts

- **Hooks** that make the board, sala and gbrain work together. They guard the board's repository, hand build work to builders, stop
  endless waits, check gbrain writes and stamp prompts.
- **The `spawn` skill**, which starts a named tmux + Claude Code session through `sala spawn`.
- **Three helper agents**: `executor`, `deep-engineer` and `sweeper`.
- **Settings**, read from `STACKSHOT_PLUGIN_<NAME>` environment variables or from `~/.config/stackshot/plugin.json`, all with
  neutral defaults.

## Documentation

- [The pieces of the plugin](docs/hooks.md): every hook, skill and agent and what it does.
- [Install](docs/install.md): the full install steps and requirements.
- [What is optional](docs/optional-parts.md): what each hook does when stackshot, sala or gbrain is missing.
- [Settings](docs/settings.md): every setting, moved pages and permits, and an example `plugin.json`.
- [What stays personal](docs/what-stays-personal.md): what is deliberately left out of the plugin.
- [Name resolution](docs/name-resolution.md): moving from another plugin's helper agent names.
- [Tests and CI](docs/tests-and-ci.md): how to run the tests, and the commit and PR text checks.
- [Full setup guide](docs/full-setup-guide.md), [for an AI assistant](docs/full-setup-guide-for-ai.md) and [run log](docs/full-setup-run.md): the whole stack, step by step.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md): commit and PR text rules, the tests, the merge queue and how a change is released.

## License

MIT, see [LICENSE](LICENSE).
