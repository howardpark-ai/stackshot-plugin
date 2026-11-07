"""The tree carries no instance values: none of the author's machines, accounts, domains, session names or project ids, in any file or
file name. Only the maintainer's repository URLs (this plugin's, and the stack's two sibling repositories the guides link to) and the
copyright / author name in the license and the two manifests are allowed.

The instance patterns are not in the repository: tests/_support.py reads them from CLEAN_TEXT_DENY or the per-user deny file, as
tools/clean_text.py does. Without a deny list only the generic patterns run, and the test says so."""
import os
import re
import sys
import unittest

import _support as S

SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache"}
# A link to the maintainer's repositories is allowed, whoever the owner is (as a GitHub address, or as the owner/repo argument of
# `marketplace add`); so is the copyright line of the license and the name value of the two manifests.
ALLOWED_URL = re.compile(r"github\.com/[\w.-]+/(?:stackshot-plugin|stackshot|salamander)(?![\w-])"
                         r"|(?<=marketplace add )[\w.-]+/stackshot-plugin(?![\w-])", re.I)
MAINTAINER_TEXT = re.compile(r'(?m)^Copyright \(c\) \d{4} .*$|"name":\s*"[^"]*"')
MAINTAINER_FILES = {"LICENSE", os.path.join(".claude-plugin", "plugin.json"), os.path.join(".claude-plugin", "marketplace.json")}


def tree_files():
    for dirpath, dirnames, filenames in os.walk(S.ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            yield os.path.relpath(full, S.ROOT), full


class NoInstanceStrings(unittest.TestCase):
    def test_tree_has_none(self):
        """Every file name and line is checked against the generic patterns and, when available, the deny list. A failure names the
        file, the line number and the pattern ("deny pattern N"), never the text that matched."""
        if S.INSTANCE_NOTE:
            print(f"\nnote: {S.INSTANCE_NOTE}", file=sys.stderr)
        found = []
        count = 0
        for rel, full in tree_files():
            count += 1
            hits = S.TREE_RX.hits(rel)
            if hits:
                found.append(f"file #{count}: in the file name ({', '.join(hits)})")
            try:
                text = S.read(full)
            except (UnicodeDecodeError, OSError):
                continue
            text = ALLOWED_URL.sub("", text)
            if rel in MAINTAINER_FILES:
                text = MAINTAINER_TEXT.sub("", text)
            for n, line in enumerate(text.splitlines(), 1):
                hits = S.TREE_RX.hits(line)
                if hits:
                    found.append(f"{rel}:{n}: {', '.join(hits)}")
        self.assertGreater(count, 20)                                  # the walk really saw the tree
        self.assertEqual(found, [], "instance strings in the tree:\n" + "\n".join(found))

    def test_the_check_itself_catches_them(self):
        # the generic patterns are not vacuous: they hit a sample of each kind (the instance patterns are not in the repository);
        # the samples are built from pieces so that this file does not trip the tree check
        for sample in ["/home/" + "someone/x", "board" + "#123", "board-tmp-x" + "#7"]:
            self.assertTrue(S.TREE_RX.hits(sample), sample)
        self.assertEqual(S.OWNER_RX.hits("/home/" + "someone/x"), [])      # an install path in a hook's output is not an owner string
        for fine in ["stackshot", "sala spawn", "owner", "acme/widgets", "the owner", "/home", "issue number 12", "# 12", "tracker#12"]:
            self.assertEqual(S.TREE_RX.hits(fine), [], fine)


if __name__ == "__main__":
    unittest.main()
