#!/usr/bin/env python3
"""subagent-model-gate.py - PreToolUse hook (matcher `Agent|Task`): build work goes to the cheaper model first, the strongest model
only as an escalation.

By default all build work goes to the `executor` agent (Sonnet); it escalates to the `deep-engineer` agent (Opus) only when the
executor fell short (repeated mistakes or errors). On every Agent (or older Task) call:
  1. `subagent_type` deep-engineer is refused unless the prompt has a line that starts with `Escalation:` (leading whitespace,
     list markers, a blockquote `>` and bold markers allowed; any case) with some text after it: which executor run, and how it
     fell short.
  2. A `model` override of opus or fable, or any model id containing either word, is refused on every type, deep-engineer
     included (its own type sets its model, so it needs no override).
  3. `general-purpose`, `claude`, `fork` or no type, with no `model` of sonnet or haiku: the call goes on, with a note as
     additionalContext that the subagent inherits the parent's model and that build work goes to the executor agent.
Every other call passes untouched (executor, sweeper, Explore, Plan ... with no model, or with sonnet or haiku).

The agents live in this plugin and answer to `<namespace>:deep-engineer` for every namespace in the setting `agent_namespaces`
(default `stackshot`; a setup that also keeps its own plugin of the same agents lists that plugin's name too), and to the bare name.

Never `ask`: a refusal is a JSON permissionDecision "deny" on stdout with exit 0; a note is hookSpecificOutput.additionalContext
with no permissionDecision, so the call goes on through the normal permission flow. It reads no file and runs nothing but the
settings lookup. Any error or unreadable input: silent exit 0 (fails open).

Not covered: a subagent another tool starts on its own (no PreToolUse Agent call for it), and the model a type itself sets (the hook
sees only the call's arguments).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _config  # noqa: E402

TOOLS = ("Agent", "Task")
INHERITS = ("", "general-purpose", "claude", "fork")
OPUS_WORDS = ("opus", "fable")
CHEAP_WORDS = ("sonnet", "haiku")
TAG = "(subagent-model-gate.py)"
# a line that starts with "Escalation:" - list markers (-, *, +, bullet, 1., 1)), a blockquote > and **bold** allowed - and has text after it
ESCALATION = re.compile(r"(?im)^[ \t]*(?:(?:[-*+•>]|\d+[.)])[ \t]*)*(?:\*\*|__)?escalation(?:\*\*|__)?[ \t]*:(.*)$")


def namespaces():
    return [n.lower() for n in _config.get_list("agent_namespaces", ["stackshot"])] or ["stackshot"]


def deep_names():
    return {"deep-engineer"} | {f"{n}:deep-engineer" for n in namespaces()}


def executor_name():
    return f"{namespaces()[0]}:executor"


def has_escalation(prompt):
    """Some `Escalation:` line carries text after it (bold markers and spaces alone are not text)."""
    return any(re.sub(r"[\s*_]", "", mt.group(1)) for mt in ESCALATION.finditer(prompt))


def deny(reason):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                        "permissionDecisionReason": reason}}, ensure_ascii=False))
    return 0


def note(text):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}},
                                ensure_ascii=False))
    return 0


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(ev, dict) or str(ev.get("tool_name") or "") not in TOOLS:
        return 0
    inp = ev.get("tool_input")
    if not isinstance(inp, dict):
        return 0
    stype = str(inp.get("subagent_type") or "").strip()
    kind = stype.lower()
    model = str(inp.get("model") or "").strip()
    m = model.lower()
    ex = executor_name()
    if any(w in m for w in OPUS_WORDS):
        return deny(f"Refused: model override '{model}' on {stype or 'a subagent with no type'}. Build work goes to {ex} (Sonnet); "
                    f"deep-engineer sets its own model and takes no override (escalation only, with an 'Escalation: ...' line). "
                    f"Drop the model, or pass model: sonnet or haiku. {TAG}")
    if kind in deep_names():
        if not has_escalation(str(inp.get("prompt") or "")):
            return deny(f"Refused: deep-engineer is an escalation only. Send it to {ex} first; escalate only after the executor "
                        f"fell short, with a line 'Escalation: <which executor run, and how it fell short>'. {TAG}")
        return 0
    if kind in INHERITS and not any(w in m for w in CHEAP_WORDS):
        if kind == "fork":
            return note(f"A fork inherits the parent's model and ignores a model override. Build work goes to {ex} (Sonnet); "
                        f"for anything else, start a fresh subagent with model: sonnet. {TAG}")
        return note(f"This subagent inherits the parent's model. Build work goes to {ex} (Sonnet); pass model: sonnet for "
                    f"anything else. {TAG}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
