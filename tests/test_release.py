"""The workflows must run the context builders of their own release."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip()


DOCTOR_TRIGGER = """  workflow_run:
    # The setup script (.github/aw/deploy-doctor/setup.py) fills in the next two lists.
    # By hand: the names of your workflows that deploy to or validate against a Salesforce org.
    workflows: ["Deploy", "Validate", "PR Validation"]
    types: [completed]
    # The branches whose runs the doctor looks at. gh-aw requires an
    # explicit list. For a pull request validation this is the branch of the
    # pull request, so include your feature branch patterns.
    branches:
      - main
      - develop
      - "feature/**"
      - "release/**"
      - "hotfix/**"
"""

PREFLIGHT_TRIGGER = """  pull_request:
    types: [opened, synchronize, reopened]
    # The folders that hold your Salesforce metadata. The setup script reads them from sfdx-project.json.
    paths:
      - "force-app/**"
"""


class Release(unittest.TestCase):
    def test_version_is_semantic(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_workflows_check_out_the_tool_at_this_version(self):
        for path in sorted((ROOT / "workflows").glob("*.md")):
            refs = re.findall(r"repository: rammc/sf-deploy-doctor\n\s+ref: (\S+)", path.read_text())
            self.assertTrue(refs, path.name)
            self.assertEqual(set(refs), {f"v{VERSION}"}, path.name)

    def test_readme_installs_this_version(self):
        pinned = re.findall(r"rammc/sf-deploy-doctor(?:/[\w-]+)?@(v[\w.]+)", (ROOT / "README.md").read_text())
        self.assertTrue(pinned)
        self.assertEqual(set(pinned), {f"v{VERSION}"})

    def test_lines_users_edit_stay_where_they_are(self):
        """The setup script and the users write into these blocks. `gh aw update` merges a new
        release with those local changes, and a change of ours on a neighbouring line turns
        into a merge conflict in every installed repository (it did, from 0.1.0 to 0.2.0).
        If this test fails, you changed such a block: move the change away from it, or accept
        the conflict knowingly, say so in the changelog and update the text below."""
        doctor = (ROOT / "workflows" / "deploy-doctor.md").read_text()
        preflight = (ROOT / "workflows" / "deploy-preflight.md").read_text()
        self.assertTrue(DOCTOR_TRIGGER in doctor, "trigger block of deploy-doctor.md changed")
        self.assertTrue(PREFLIGHT_TRIGGER in preflight, "trigger block of deploy-preflight.md changed")

    def test_changelog_has_an_entry(self):
        self.assertIn(f"## {VERSION}", (ROOT / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
