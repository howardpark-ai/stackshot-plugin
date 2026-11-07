<!--
Starter rulebook. Blank on purpose: each section holds one line saying what kind of rule belongs in it and one placeholder rule to
replace. The plugin ships no rules of its own. Write your own, delete the sections you do not want, and add sections of your own.

How to use it:
1. Copy this file out of the plugin, into a folder of your own that you keep in your own (private) git repository, for example
   ~/rulebook/RULES.md. Never edit the copy inside the plugin: a plugin update replaces it.
2. Import it from your Claude Code instructions file, ~/.claude/CLAUDE.md, with one line (the path is yours):

       @~/rulebook/RULES.md

   Claude Code reads an imported file at session start, in every project, next to whatever else CLAUDE.md holds. Put the line outside
   any block a tool manages in that file (gbrain's setup writes its own block between two marker comments; leave it alone).
3. Replace each placeholder with a rule of your own: one sentence of what to do and, after "Why:", one sentence of the reason. A rule
   with a reason is followed in the cases you did not think of; a rule without one is followed to the letter or ignored.

These rules are your own settings. The plugin, stackshot, sala and gbrain never read this file, never change it and never ship one
of their own: what you want from your sessions is yours to decide, and it stays on your machines.

Delete this comment when you have read it.
-->

# My rules

## How I want to be addressed and answered

What to call you, and which language and register the answers come in.

- <a rule>. Why: <the reason>.

## Answers and reports

How an answer or a report is shaped: what comes first, what it must say, how long it runs.

- <a rule>. Why: <the reason>.

## Safety

What must never happen, and what needs your yes before it does.

- <a rule>. Why: <the reason>.

## Connected services (mail, calendar, chat, documents)

What a session may do on its own in your connected services, and what it may do only when you ask.

- <a rule>. Why: <the reason>.

## Time and dates

Which time zone times are reported in, and how dates are written.

- <a rule>. Why: <the reason>.

## Code and engineering

The conventions, tests and tools a session follows when it builds or changes something.

- <a rule>. Why: <the reason>.

## The board and my sessions

What a session records, and where, when work waits on you or changes state.

- <a rule>. Why: <the reason>.

## Memory and notes

What a session looks up before it answers, and where a fact worth keeping is written down.

- <a rule>. Why: <the reason>.

## Corrections

What a session does when you correct it.

- <a rule>. Why: <the reason>.
