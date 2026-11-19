"""The model gate hook: build work goes to the executor first; deep-engineer only as an escalation, with an `Escalation:` line; no opus
model override; a fable override passes only for design and analysis (general-purpose, claude or no type, with a `Design:` line);
a note when a subagent inherits the parent's model.

Each test runs the hook under a temp HOME (locked while it runs, removed after, pass or fail). Nothing here starts a long-lived process
or touches the real home directory."""
import json
import os
import unittest

from _support import HOOKS, context, decision, env_for, reason, run_hook, temp_home

HOOK = next(f for f in sorted(os.listdir(HOOKS)) if f.endswith("model-gate.py"))   # the hook that gates subagent models

DES = "Design: how should the gate treat fable subagents, item 900"
ESC = "Escalation: executor run 1 failed twice on the same test"


def call(tool="Agent", **inp):
    with temp_home() as home:
        code, out, _err = run_hook(HOOK, {"tool_name": tool, "tool_input": inp}, env_for(home))
    assert code == 0
    return out


class SubagentModelGate(unittest.TestCase):
    def test_fable_design_passes_with_note(self):
        for tool in ("Agent", "Task"):
            for stype in ("general-purpose", "claude", None):
                for line in (DES, "- " + DES, "> " + DES, "**Design:** the gate question, item 900", "design: the question"):
                    with self.subTest(tool=tool, stype=stype, line=line):
                        inp = {"model": "fable", "prompt": "Analyze.\n" + line + "\nDone means: a recommendation."}
                        if stype is not None:
                            inp["subagent_type"] = stype
                        out = call(tool, **inp)
                        self.assertEqual(decision(out), "note")
                        n = context(out)
                        for word in ("design and analysis only", "stackshot:executor", "400 data retention", "subscription lane"):
                            self.assertIn(word, n)

    def test_fable_model_ids_containing_the_word_pass_too(self):
        out = call(subagent_type="general-purpose", model="claude-fable-5-2", prompt=DES)
        self.assertEqual(decision(out), "note")

    def test_fable_without_design_line_refused_and_names_the_route(self):
        out = call(subagent_type="general-purpose", model="fable", prompt="Analyze this.")
        self.assertEqual(decision(out), "deny")
        self.assertIn("'Design: <the question and its item>'", reason(out))

    def test_fable_design_refused_on_other_types(self):
        for stype in ("stackshot:executor", "stackshot:sweeper", "stackshot:deep-engineer", "deep-engineer", "fork", "Explore"):
            with self.subTest(stype=stype):
                self.assertEqual(decision(call(subagent_type=stype, model="fable", prompt=DES + "\n" + ESC)), "deny")

    def test_opus_always_refused_even_with_a_design_line(self):
        for model in ("opus", "claude-opus-4-8", "opus-fable", "fable-opus"):
            with self.subTest(model=model):
                self.assertEqual(decision(call(subagent_type="general-purpose", model=model, prompt=DES)), "deny")

    def test_design_line_without_text_refused(self):
        for prompt in ("Design:", "Design:   ", "Design: **", "**Design:**", "Design:\nthe question on the next line",
                       "The Design: line is missing here"):
            with self.subTest(prompt=prompt):
                self.assertEqual(decision(call(subagent_type="general-purpose", model="fable", prompt=prompt)), "deny")

    def test_deep_engineer_needs_an_escalation_line(self):
        self.assertEqual(decision(call(subagent_type="stackshot:deep-engineer", prompt="Fix it.")), "deny")
        self.assertEqual(decision(call(subagent_type="stackshot:deep-engineer", prompt="Fix it.\n" + ESC)), "allow")
        self.assertEqual(decision(call(subagent_type="deep-engineer", prompt="Fix it.\n- **Escalation:** run 2 failed")), "allow")

    def test_inheriting_types_get_a_note_and_cheap_models_pass(self):
        self.assertEqual(decision(call(subagent_type="general-purpose", prompt="Do it.")), "note")
        self.assertEqual(decision(call(prompt="Do it.")), "note")
        self.assertIn("ignores a model override", context(call(subagent_type="fork", prompt="Do it.")))
        self.assertEqual(decision(call(subagent_type="general-purpose", model="sonnet", prompt="Do it.")), "allow")
        self.assertEqual(decision(call(subagent_type="stackshot:executor", prompt="Do it.")), "allow")

    def test_other_tools_and_malformed_input_fail_open(self):
        with temp_home() as home:
            env = env_for(home)
            for payload in ("", "not json", "[]", json.dumps({"tool_name": "Agent", "tool_input": "x"}),
                            json.dumps({"tool_name": "Bash", "tool_input": {"command": "x", "model": "opus"}})):
                with self.subTest(payload=payload):
                    code, out, _err = run_hook(HOOK, payload, env)
                    self.assertEqual((code, out), (0, ""))


if __name__ == "__main__":
    unittest.main()
