"""The full-setup guides and the starter rulebook: the files exist, their relative links resolve, the template is a blank starter (an
import line, sections with one description line and one placeholder rule each, no rules, no personal values) and the two guides agree on the facts they must agree on."""
import os
import re
import unittest

import _support as S

DOCS = os.path.join(S.ROOT, "docs")
GUIDES = ["full-setup-guide.md", "full-setup-guide-for-ai.md"]
TEMPLATE = os.path.join(S.ROOT, "templates", "RULES.md")
LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s#]+)(?:#[^)\s]*)?\)")


class FullSetupDocs(unittest.TestCase):
    def test_files_exist(self):
        for name in GUIDES + ["full-setup-run.md"]:
            self.assertTrue(os.path.isfile(os.path.join(DOCS, name)), name)
        self.assertTrue(os.path.isfile(TEMPLATE))

    def test_relative_links_resolve(self):
        for name in GUIDES + ["full-setup-run.md"]:
            path = os.path.join(DOCS, name)
            for target in LINK.findall(S.read(path)):
                self.assertTrue(os.path.exists(os.path.normpath(os.path.join(DOCS, target))), f"{name}: {target}")

    def test_guides_point_at_each_other(self):
        person, ai = (S.read(os.path.join(DOCS, n)) for n in GUIDES)
        self.assertIn("full-setup-guide-for-ai.md", person)
        self.assertIn("full-setup-guide.md", ai)

    def test_no_pip_and_no_npm_install(self):
        for name in GUIDES:
            text = S.read(os.path.join(DOCS, name))
            self.assertNotRegex(text, r"(?m)^\s*(pip3?|npm) install")
            self.assertNotIn("bun add -g gbrain", text)

    def test_template_is_a_blank_starter(self):
        text = S.read(TEMPLATE)
        self.assertRegex(text, r"(?m)^\s+@~/rulebook/RULES\.md\s*$")        # the import line, shown in the how-to comment
        sections = re.findall(r"(?m)^## .+$", text)
        self.assertGreaterEqual(len(sections), 6)
        placeholder = "- <a rule>. Why: <the reason>."
        for block in re.split(r"(?m)^## ", text)[1:]:
            self.assertEqual(re.findall(r"(?m)^- .*$", block), [placeholder], block.splitlines()[0])   # exactly the placeholder, no example rule
        # no line starting "- " anywhere but the placeholder bullet, and no name or zone placeholders
        self.assertEqual({line for line in text.splitlines() if line.startswith("- ")}, {placeholder})
        self.assertEqual(len(re.findall(r"(?m)^- ", text)), len(sections))
        self.assertNotIn("<your", text)
        self.assertIn("your own settings", text)

    def test_guides_assume_public_repositories(self):
        for name in GUIDES:
            text = S.read(os.path.join(DOCS, name))
            self.assertNotRegex(text, r"(?i)private until|private repositories")
            self.assertNotRegex(text, r"deploy key|read access")
        ai = S.read(os.path.join(DOCS, GUIDES[1]))
        self.assertNotRegex(ai, r"\bH1\b")
        for name in GUIDES:
            text = S.read(os.path.join(DOCS, name))
            self.assertNotIn("<your name>", text)
            self.assertNotIn("<your zone>", text)

    def test_guides_say_doctor_counts_the_plugins_hooks(self):
        for name in GUIDES:
            text = S.read(os.path.join(DOCS, name))
            self.assertIn("4 of 4", text, name)
            self.assertNotRegex(text, r"(?i)expected (in|with) the (full setup|plugin)", name)
            self.assertNotRegex(text, r"TODO\(verify\)`?: a stackshot release", name)

    def test_every_todo_is_marked_the_same_way(self):
        for name in GUIDES:
            text = S.read(os.path.join(DOCS, name))
            self.assertIn("TODO(verify)", text)
            self.assertNotRegex(text, r"(?i)\btodo\b(?!\(verify\))")


if __name__ == "__main__":
    unittest.main()
