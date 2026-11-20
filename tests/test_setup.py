"""Tests for scripts/setup.py. Run with: python3 -m unittest discover -s tests"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import setup  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def write(repo, rel, text):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        W = ".github/workflows"
        write(self.repo, f"{W}/validate.yml",
              "name: PR Validation\non:\n  pull_request:\n    branches: [main, 'release/**']\n"
              "jobs:\n  v:\n    steps:\n      - run: sf project deploy start --dry-run\n")
        write(self.repo, f"{W}/deploy.yml",
              "name: \"Deploy to Production\"\non:\n  push:\n    branches:\n      - main\n      - hotfix/*\n"
              "jobs:\n  d:\n    uses: ./.github/workflows/reusable-deploy.yml\n")
        write(self.repo, f"{W}/reusable-deploy.yml",
              "name: Reusable deploy\non:\n  workflow_call:\njobs:\n  d:\n    steps:\n      - uses: ./.github/actions/deploy\n")
        write(self.repo, ".github/actions/deploy/action.yml", "runs:\n  steps:\n    - run: sf project deploy start\n")
        write(self.repo, f"{W}/script.yml", "on: workflow_dispatch\njobs:\n  d:\n    steps:\n      - run: ./ci/deploy.sh\n")
        write(self.repo, "ci/deploy.sh", "sfdx force:source:deploy -p force-app\n")
        write(self.repo, f"{W}/lint.yml", "name: Lint\non: pull_request\njobs:\n  l:\n    steps:\n      - run: npm run lint\n")
        write(self.repo, f"{W}/other.lock.yml", "name: Agentic\non: push\njobs:\n  a:\n    steps:\n      - run: sf project deploy start\n")
        write(self.repo, "sfdx-project.json", '{"packageDirectories": [{"path": "force-app", "default": true}, {"path": "./packages/core/"}]}')
        for name in ("deploy-doctor.md", "deploy-preflight.md"):
            write(self.repo, f"{W}/{name}", (ROOT / "workflows" / name).read_text())
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        for branch in ("develop", "feature/a", "feature/b", "bugfix/c", "PROJ-1-x", "PROJ-2-y", "PROJ-3-z", "lonely"):
            git(self.repo, "branch", branch)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_workflows_that_deploy(self):
        found = {w["name"] for w in setup.deploy_workflows(self.repo)}
        # direct command, through a reusable workflow and a local action, through a script;
        # not the reusable workflow itself, not the linter, not a compiled agentic workflow
        self.assertEqual(found, {"PR Validation", "Deploy to Production", ".github/workflows/script.yml"})

    def test_branch_patterns(self):
        patterns, uncovered = setup.branch_patterns(self.repo, setup.deploy_workflows(self.repo))
        for expected in ("main", "release/**", "hotfix/*", "develop", "feature/**", "bugfix/**", "PROJ-*"):
            self.assertIn(expected, patterns)
        self.assertFalse(any(setup.is_wildcard_only(p) for p in patterns))
        self.assertEqual(uncovered, ["lonely"])

    def test_package_paths(self):
        self.assertEqual(setup.package_paths(self.repo), ["force-app/**", "packages/core/**"])

    def test_write_changes_only_trigger_and_paths(self):
        folder = self.repo / ".github/workflows"
        before = {n: (folder / n).read_text() for n in ("deploy-doctor.md", "deploy-preflight.md")}
        run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts/setup.py"), "--repo-dir", str(self.repo), *a],
                                        capture_output=True, text=True)
        dry = run()
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(before["deploy-doctor.md"], (folder / "deploy-doctor.md").read_text())
        self.assertEqual(run("--write").returncode, 0)
        doctor = (folder / "deploy-doctor.md").read_text()
        self.assertTrue('workflows: ["Deploy to Production", ".github/workflows/script.yml", "PR Validation"]' in doctor)
        self.assertTrue('      - "feature/**"\n' in doctor)
        self.assertFalse('"hotfix/**"' in doctor)  # the shipped example list is replaced
        preflight = (folder / "deploy-preflight.md").read_text()
        self.assertTrue('    paths:\n      - "force-app/**"\n      - "packages/core/**"\n  permissions:' in preflight)
        # everything below the trigger is untouched
        self.assertEqual(before["deploy-doctor.md"].split("workflow_dispatch:")[1], doctor.split("workflow_dispatch:")[1])
        self.assertEqual(before["deploy-preflight.md"].split("permissions:", 1)[1], preflight.split("permissions:", 1)[1])
        self.assertEqual(run("--write").returncode, 0)  # running it twice changes nothing
        self.assertEqual(doctor, (folder / "deploy-doctor.md").read_text())

    def test_overrides_and_nothing_found(self):
        for name in ("validate.yml", "deploy.yml", "script.yml"):
            (self.repo / ".github/workflows" / name).unlink()
        run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts/setup.py"), "--repo-dir", str(self.repo), *a],
                                        capture_output=True, text=True)
        self.assertEqual(run().returncode, 1)
        ok = run("--workflows", "Ship it, Check it", "--branches", "main,team/**", "--write")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        doctor = (self.repo / ".github/workflows/deploy-doctor.md").read_text()
        self.assertTrue('workflows: ["Ship it", "Check it"]' in doctor)
        self.assertTrue('    branches:\n      - "main"\n      - "team/**"\n  workflow_dispatch:' in doctor)


if __name__ == "__main__":
    unittest.main()
