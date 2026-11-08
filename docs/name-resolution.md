# Name resolution

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
