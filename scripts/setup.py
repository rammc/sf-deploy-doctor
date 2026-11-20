#!/usr/bin/env python3
"""
Adapt the installed Deploy Doctor workflows to this repository.

Run it once in the root of your repository after `gh aw add`:

  python3 .github/aw/deploy-doctor/setup.py            # show what would change
  python3 .github/aw/deploy-doctor/setup.py --write    # change the files
  gh aw compile

It reads your GitHub Actions workflows, finds the ones that deploy to or
validate against a Salesforce org, and writes their names into the trigger of
`deploy-doctor.md`, together with the branches they run on. It reads
`sfdx-project.json` and writes your package directories into the path filter
of `deploy-preflight.md`. Nothing leaves your machine, and nothing but these
two files is changed.

Override what it finds with --workflows and --branches (comma separated).

Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# A workflow, action or script that contains one of these deploys Salesforce metadata.
DEPLOY_HINT = re.compile(
    r"\bsf\s+project\s+deploy\b|\bsfdx?\s+force:(?:source|mdapi):deploy\b|\bsf\s+deploy\s+metadata\b"
    r"|\bsfdx-deploy\b|\bsf\s+package\s+install\b|\bsfp\s+(?:deploy|install|validate|release)\b"
    r"|\bsfpowerscripts\b|\bsf\s+hardis:project:deploy\b", re.I)
LONG_LIVED = ("main", "master", "develop", "development", "dev", "staging", "release", "production", "prod",
              "integration", "int", "qa", "uat", "test")
MAX_PATTERNS = 20


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def workflow_name(path: Path, text: str, root: Path) -> str:
    m = re.search(r"^name:\s*(.+?)\s*$", text, re.M)
    if m:
        return m.group(1).strip("\"'")
    return path.relative_to(root).as_posix()  # GitHub uses the file path when there is no name


def triggers(text: str) -> str:
    """The `on:` block of a workflow as text (also when written as `"on":` or `on: [push]`)."""
    m = re.search(r"^[\"']?on[\"']?:(.*?)(?=^\S|\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def runs_on_its_own(text: str) -> bool:
    """False for a reusable workflow that is only ever called by another one."""
    names = set(re.findall(r"\b(workflow_call|push|pull_request|pull_request_target|workflow_dispatch|schedule|"
                           r"release|workflow_run|merge_group|issue_comment|repository_dispatch|deployment)\b",
                           triggers(text)))
    return bool(names - {"workflow_call"})


def local_uses(text: str) -> list[str]:
    """Paths of local actions and reusable workflows a workflow calls."""
    return [u.rstrip("/") for u in re.findall(r"uses:\s*[\"']?\./([^\s\"'@]+)", text)]


def deploys(root: Path, text: str, seen: set[str] | None = None) -> bool:
    if DEPLOY_HINT.search(text):
        return True
    seen = seen if seen is not None else set()
    for used in local_uses(text):
        if used in seen:
            continue
        seen.add(used)
        target = root / used
        files = [target] if target.is_file() else [target / "action.yml", target / "action.yaml"]
        if any(deploys(root, read(f), seen) for f in files if f.is_file()):
            return True
    # scripts the workflow runs: `run: ./scripts/deploy.sh`, `bash ci/validate.sh`, npm scripts are not followed
    for script in re.findall(r"(?:^|\s)(?:bash\s+|sh\s+|\./)([\w./-]+\.sh)\b", text):
        if script not in seen:
            seen.add(script)
            if DEPLOY_HINT.search(read(root / script)):
                return True
    return False


def deploy_workflows(root: Path) -> list[dict]:
    found = []
    folder = root / ".github" / "workflows"
    for path in sorted(list(folder.glob("*.yml")) + list(folder.glob("*.yaml"))):
        if path.name.endswith(".lock.yml"):
            continue  # compiled agentic workflows
        text = read(path)
        if deploys(root, text) and runs_on_its_own(text):
            found.append({"name": workflow_name(path, text, root), "file": path.relative_to(root).as_posix(),
                          "text": text})
    return found


def trigger_branches(text: str) -> list[str]:
    """Branch names and patterns listed under `branches:` in the triggers of a workflow."""
    out = []
    block = triggers(text)
    for m in re.finditer(r"^\s*branches:\s*\[([^\]]*)\]", block, re.M):
        out += [b.strip().strip("\"'") for b in m.group(1).split(",")]
    for m in re.finditer(r"^(\s*)branches:\s*\n((?:\1\s+-\s*.+\n?)+)", block, re.M):
        out += [b.strip().strip("\"'") for b in re.findall(r"-\s*(.+)", m.group(2))]
    return [b for b in out if b and not b.startswith("!")]


def git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_branches(root: Path) -> list[str]:
    names = []
    for line in git(root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").splitlines():
        name = re.sub(r"^refs/(?:heads|remotes/[^/]+)/", "", line.strip())
        if name and name != "HEAD":
            names.append(name)
    return names


def default_branch(root: Path) -> str | None:
    ref = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    return ref.split("/", 1)[1] if "/" in ref else None


def is_wildcard_only(pattern: str) -> bool:
    return not re.sub(r"[*/?]", "", pattern)


def branch_patterns(root: Path, workflows: list[dict]) -> tuple[list[str], list[str]]:
    """Patterns for the branches a deployment can run on, and branches they do not cover.

    gh-aw wants specific names or scoped patterns here, not a bare wildcard. A pull
    request validation runs on the branch of the pull request, so the prefixes of
    the existing branches (feature/, fix/, ...) are turned into patterns.
    """
    patterns: list[str] = []

    def add(p: str) -> None:
        if p and p not in patterns and not is_wildcard_only(p):
            patterns.append(p)

    add(default_branch(root) or "")
    for wf in workflows:
        for b in trigger_branches(wf["text"]):
            add(b)
    existing = repo_branches(root)
    for name in existing:
        if name in LONG_LIVED:
            add(name)
    prefixes: dict[str, int] = {}
    for name in existing:
        if "/" in name:
            prefix = name.split("/", 1)[0]
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
    # Flat names that start with a ticket key or a shared word: PROJ-123-fix-login -> PROJ-*
    flat: dict[str, int] = {}
    for name in existing:
        m = re.match(r"([A-Za-z][A-Za-z0-9]*)[-_]", name) if "/" not in name else None
        if m and name not in LONG_LIVED:
            flat[m.group(1)] = flat.get(m.group(1), 0) + 1
    ranked = [(count, f"{prefix}/**") for prefix, count in prefixes.items()]
    ranked += [(count, f"{prefix}-*") for prefix, count in flat.items() if count >= 3]
    for _, pattern in sorted(ranked, key=lambda kv: (-kv[0], kv[1])):
        add(pattern)
    if not patterns:
        patterns = ["main"]
    patterns = patterns[:MAX_PATTERNS]
    regexes = [re.compile("^" + re.escape(p).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$") for p in patterns]
    uncovered = sorted({n for n in existing if not any(r.match(n) for r in regexes)})
    return patterns, uncovered


def package_paths(root: Path) -> list[str]:
    try:
        project = json.loads(read(root / "sfdx-project.json") or "{}")
    except ValueError:
        return []
    paths = []
    for entry in project.get("packageDirectories", []):
        path = str(entry.get("path", "")).strip("./").strip("/")
        if path and f"{path}/**" not in paths:
            paths.append(f"{path}/**")
    return paths


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_doctor_trigger(text: str, names: list[str], branches: list[str]) -> str:
    text = re.sub(r"^(\s*)workflows:\s*\[.*\]\s*$",
                  lambda m: f"{m.group(1)}workflows: [{', '.join(quote(n) for n in names)}]", text, count=1, flags=re.M)

    def block(m: re.Match) -> str:
        indent = m.group(1)
        return f"{indent}branches:\n" + "".join(f"{indent}  - {quote(b)}\n" for b in branches)

    return re.sub(r"^([ \t]*)branches:[ \t]*\n(?:\1[ \t]+-.*\n)+", block, text, count=1, flags=re.M)


def set_preflight_paths(text: str, paths: list[str]) -> str:
    def block(m: re.Match) -> str:
        indent = m.group(1)
        return f"{indent}paths:\n" + "".join(f"{indent}  - {quote(p)}\n" for p in paths)

    return re.sub(r"^([ \t]*)paths:[ \t]*\n(?:\1[ \t]+-.*\n)+", block, text, count=1, flags=re.M)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-dir", default=".", help="root of your repository (default: current directory)")
    ap.add_argument("--workflows", help="names of your deploy workflows, comma separated (skips detection)")
    ap.add_argument("--branches", help="branch names or patterns, comma separated (skips detection)")
    ap.add_argument("--write", action="store_true", help="change the files; without it the result is only shown")
    args = ap.parse_args()

    root = Path(args.repo_dir).resolve()
    folder = root / ".github" / "workflows"
    doctor, preflight = folder / "deploy-doctor.md", folder / "deploy-preflight.md"
    if not doctor.is_file() and not preflight.is_file():
        print("Deploy Doctor is not installed here. Run first:\n  gh aw add rammc/sf-deploy-doctor", file=sys.stderr)
        return 1

    found = deploy_workflows(root)
    names = [n.strip() for n in args.workflows.split(",") if n.strip()] if args.workflows else [w["name"] for w in found]
    patterns, uncovered = branch_patterns(root, found)
    if args.branches:
        patterns, uncovered = [b.strip() for b in args.branches.split(",") if b.strip()], []
    paths = package_paths(root)
    problems = 0

    if doctor.is_file():
        print("Deploy Doctor (diagnosis of failed runs)")
        if names:
            for w in found if not args.workflows else []:
                print(f"  deploy workflow found: {w['name']}  ({w['file']})")
            print(f"  workflows: {', '.join(names)}")
            print(f"  branches:  {', '.join(patterns)}")
            if uncovered:
                shown = ", ".join(uncovered[:5]) + (f" and {len(uncovered) - 5} more" if len(uncovered) > 5 else "")
                print(f"  note: not covered by these patterns: {shown}. Runs on such branches get no diagnosis; "
                      "add a pattern with --branches if you need them.")
            before = read(doctor)
            after = set_doctor_trigger(before, names, patterns)
            if args.write and after != before:
                doctor.write_text(after)
        else:
            problems += 1
            print("  No workflow that deploys to Salesforce was found in .github/workflows.\n"
                  "  Name yours with: --workflows \"My Deploy Workflow,My Validation\"")
    if preflight.is_file():
        print("Pre-flight (assessment of pull requests)")
        if paths:
            print(f"  paths:     {', '.join(paths)}")
            before = read(preflight)
            after = set_preflight_paths(before, paths)
            if args.write and after != before:
                preflight.write_text(after)
        else:
            print("  No package directories found in sfdx-project.json; the path filter stays as it is (force-app/**).")

    if problems:
        return 1
    if args.write:
        print("\nWritten. Next:\n  gh aw compile\n  git add .github && git commit -m \"Add Deploy Doctor\"\n"
              "Merge it to your default branch; the workflows start to run from there.")
    else:
        print("\nNothing was changed. Run again with --write to apply this.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
