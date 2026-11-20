"""The workflows must run the context builders of their own release."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip()


class Release(unittest.TestCase):
    def test_version_is_semantic(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_workflows_check_out_the_tool_at_this_version(self):
        for path in sorted((ROOT / "workflows").glob("*.md")):
            refs = re.findall(r"repository: rammc/sf-deploy-doctor\n\s+ref: (\S+)", path.read_text())
            self.assertTrue(refs, path.name)
            self.assertEqual(set(refs), {f"v{VERSION}"}, path.name)

    def test_readme_installs_this_version(self):
        pinned = re.findall(r"sf-deploy-doctor/deploy-(?:doctor|preflight)@(\S+)", (ROOT / "README.md").read_text())
        self.assertTrue(pinned)
        self.assertEqual(set(pinned), {f"v{VERSION}"})

    def test_changelog_has_an_entry(self):
        self.assertIn(f"## {VERSION}", (ROOT / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
