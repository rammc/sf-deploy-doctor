#!/usr/bin/env python3
"""
Build the context file for the Deploy Doctor pre-flight agent.

Deterministic and read-only. Looks at what a pull request changes in a
Salesforce project and lists the things that typically make a deployment fail
or hurt after it succeeded: references to components that are not in the
source, deleted components that are still used, field type changes, new
required fields and validation rules, Apex without a test, profiles, flows.

It never talks to an org. A signal therefore says what the source shows, not
what the org will do; the agent weighs the signals and writes the assessment.

Usage:
  preflight_context.py --base origin/main --head HEAD --out doctor/preflight.json

Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_context import BUNDLE_DIRS, TYPE_SUFFIX, find_reference  # noqa: E402

SUFFIX_TYPE = {suffix: mtype for mtype, suffix in TYPE_SUFFIX.items()}
SEVERITIES = ("high", "medium", "low")
# Custom API names: fields and objects (__c), relationships (__r), custom
# metadata types (__mdt), platform events (__e).
CUSTOM_NAME = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*?)__(c|r|mdt|e)\b")
MAX_EVIDENCE = 300


def git(root: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=check, capture_output=True, text=True).stdout


def files_at(root: Path, ref: str) -> list[str]:
    """The tree of the pull request head, independent of what is checked out."""
    return [l for l in git(root, "ls-tree", "-r", "--name-only", ref).splitlines() if l]


def files_mentioning(root: Path, ref: str, word: str, *pathspec: str) -> list[str]:
    out = git(root, "grep", "-l", "-w", "-F", "-I", word, ref, "--", *(pathspec or (".",)), check=False)
    return [l.split(":", 1)[1] for l in out.splitlines() if ":" in l]


def component_of(path: str) -> dict | None:
    """Metadata type and API name of a source file, or None if it is not metadata."""
    base = os.path.basename(path)
    parts = path.split("/")
    for mtype, folder in BUNDLE_DIRS.items():
        if folder in parts[:-1]:
            return {"type": mtype, "name": parts[parts.index(folder) + 1]}
    for suffix, mtype in sorted(SUFFIX_TYPE.items(), key=lambda kv: -len(kv[0])):
        stem = base[: -len("-meta.xml")] if base.endswith("-meta.xml") and not suffix.endswith("-meta.xml") else base
        if stem.endswith(suffix):
            name = stem[: -len(suffix)]
            if "objects" in parts and mtype != "CustomObject":
                name = f"{parts[parts.index('objects') + 1]}.{name}"
            return {"type": mtype, "name": name}
    return None


def changed_components(root: Path, base: str, head: str) -> list[dict]:
    out, seen = [], set()
    # No rename detection: the -meta.xml files of Apex classes are identical, so git pairs a
    # deleted class with an unrelated new one. For Salesforce a rename is a new component
    # plus a destructive change anyway.
    for line in git(root, "diff", "--name-status", "--no-renames", f"{base}...{head}").splitlines():
        cols = line.split("\t")
        status, path = cols[0][0], cols[-1]
        old_path = cols[1] if status == "R" else path
        comp = component_of(path)
        if not comp:
            continue
        change = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}.get(status, "modified")
        key = (comp["type"], comp["name"], change)
        if key in seen:  # a class and its -meta.xml are one component
            if not path.endswith("-meta.xml"):
                next(c for c in out if (c["type"], c["name"], c["change"]) == key)["file"] = path
            continue
        seen.add(key)
        out.append({**comp, "change": change, "file": path, "oldFile": old_path if status == "R" else None})
    return out


def added_lines(root: Path, base: str, head: str, path: str) -> list[tuple[int, str]]:
    """Lines this pull request adds to a file, with their line numbers at head."""
    lines, number = [], 0
    for raw in git(root, "diff", "-U0", f"{base}...{head}", "--", path).splitlines():
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if hunk:
            number = int(hunk.group(1))
        elif raw.startswith("+") and not raw.startswith("+++"):
            lines.append((number, raw[1:]))
            number += 1
    return lines


def file_at(root: Path, ref: str, path: str) -> str:
    return git(root, "show", f"{ref}:{path}", check=False)


def xml_value(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", text or "")
    return m.group(1).strip() if m else None


def strip_apex_noise(line: str) -> str:
    """Drop comments and string literals so names inside them do not count."""
    if line.lstrip().startswith(("*", "/*")):
        return ""
    line = re.sub(r"/\*.*?\*/", "", line)
    return re.sub(r"'(?:[^'\\]|\\.)*'", "''", line.split("//")[0])


def is_test_class(text: str) -> bool:
    return bool(re.search(r"@isTest\b", text or "", re.I))


class Signals:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, rule: str, severity: str, title: str, comp: dict | None, evidence: str, why: str, check: str,
            line: int | None = None) -> None:
        self.items.append({
            "rule": rule, "severity": severity, "title": title,
            "component": f"{comp['type']}:{comp['name']}" if comp else None,
            "file": comp.get("file") if comp else None, "line": line,
            "evidence": evidence[:MAX_EVIDENCE], "why": why, "check": check,
        })


def exists_in_source(files: list[str], name: str) -> bool:
    return find_reference(files, name) is not None


def check_references(root: Path, base: str, head: str, comp: dict, files: list[str], signals: Signals) -> None:
    """References this pull request introduces to components the source does not define."""
    seen: set[str] = set()
    path = comp["file"]
    is_apex = comp["type"] in ("ApexClass", "ApexTrigger")
    for number, line in added_lines(root, base, head, path):
        found: list[tuple[str, str]] = []
        if is_apex:
            for m in CUSTOM_NAME.finditer(strip_apex_noise(line)):
                if m.group(2) != "r":  # relationship names are not component names
                    found.append((m.group(0), m.group(0)))
        else:
            for tag in ("field", "apexClass", "object", "recordType", "referenceTo", "customPermission", "flow", "tab", "apexPage"):
                for m in re.finditer(rf"<{tag}>([^<]+)</{tag}>", line):
                    value = m.group(1).strip()
                    if tag in ("apexClass", "flow", "apexPage", "customPermission") or "__" in value:
                        found.append((value, m.group(0)))
        for name, shown in found:
            if name in seen:
                continue
            seen.add(name)
            leaf = name.rpartition(".")[2]
            if re.match(r"[A-Za-z0-9]+__[A-Za-z0-9_]+__(c|mdt|e)$", leaf):
                signals.add("package-reference", "low", "Reference to a component of a managed package", comp,
                            shown, "The name carries a namespace prefix, so it comes from a managed package.",
                            f"Make sure the package that provides `{name}` is installed in every target org.", number)
                continue
            if exists_in_source(files, name) or (is_apex and exists_in_source(files, leaf)):
                continue
            signals.add("reference-not-in-source", "high", "Reference to a component that is not in the repository", comp,
                        shown,
                        f"`{name}` is not defined anywhere in the source. The deployment only works if it already "
                        "exists in the target org; otherwise it fails with a missing-dependency error.",
                        f"Add `{name}` to this change, or confirm that it exists in every target org (and why it is "
                        "not in the repository).", number)


def check_deleted(root: Path, head: str, comp: dict, files: list[str], signals: Signals) -> None:
    leaf = comp["name"].rpartition(".")[2]
    users = [f for f in files_mentioning(root, head, leaf)
             if not os.path.basename(f).startswith(leaf + ".")]
    if users:
        signals.add("deleted-still-referenced", "high", "A deleted component is still referenced", comp,
                    ", ".join(users[:5]) + (f" and {len(users) - 5} more" if len(users) > 5 else ""),
                    "Other files in the source still use this name. They will fail to deploy, or the deletion in the "
                    "org will be blocked because the component is in use.",
                    "Remove or update the references in the listed files in the same change.")
    data_loss = comp["type"] in ("CustomField", "CustomObject")
    signals.add("destructive-change", "medium" if data_loss else "low",
                "A component is deleted from the source", comp, comp["file"],
                "Removing a file does not remove the component from the org. That needs a destructive change, and "
                + ("deleting a field or object deletes its data." if data_loss else "the org may still use it."),
                "Check that the pipeline applies destructive changes for this deployment"
                + (", and that the data is no longer needed or has been migrated." if data_loss else "."))


def check_field(root: Path, base: str, head: str, comp: dict, signals: Signals) -> None:
    new = file_at(root, head, comp["file"])
    old = file_at(root, base, comp["file"]) if comp["change"] == "modified" else ""
    new_type, old_type = xml_value(new, "type"), xml_value(old, "type")
    if old and old_type and new_type and old_type != new_type:
        signals.add("field-type-change", "high", "The type of an existing field changes", comp,
                    f"{old_type} -> {new_type}",
                    "Salesforce blocks many type changes through the Metadata API, and allowed ones can truncate or "
                    "drop data. Formula, roll-up and reference types are the usual blockers.",
                    "Check that this conversion is allowed and what happens to existing values; change the type in a "
                    "sandbox copy first.")
    if xml_value(new, "required") == "true" and xml_value(old, "required") != "true" and new_type != "MasterDetail":
        signals.add("new-required-field", "medium", "A field becomes required", comp, "<required>true</required>",
                    "Every Apex test, integration and flow that creates this object without the field starts to fail. "
                    "Failing tests in the target org block the deployment.",
                    "Search tests and automation that insert this object and set the field there, or use a default "
                    "value or a validation rule with a bypass instead.")
    if xml_value(new, "unique") == "true" and xml_value(old, "unique") != "true" and old:
        signals.add("field-becomes-unique", "medium", "An existing field becomes unique", comp, "<unique>true</unique>",
                    "The deployment fails if the org already holds duplicate values in this field.",
                    "Check the target org for duplicates before deploying.")


def check_validation_rule(root: Path, base: str, head: str, comp: dict, signals: Signals) -> None:
    new = file_at(root, head, comp["file"])
    old = file_at(root, base, comp["file"]) if comp["change"] == "modified" else ""
    if xml_value(new, "active") != "true":
        return
    if not old or xml_value(old, "active") != "true" or xml_value(old, "errorConditionFormula") != xml_value(new, "errorConditionFormula"):
        signals.add("validation-rule-active", "medium",
                    "An active validation rule is added or its condition changes", comp,
                    (xml_value(new, "errorMessage") or "")[:200],
                    "Records created by Apex tests, integrations and data loads must satisfy the rule from the moment "
                    "it is deployed. Tests that do not can fail the deployment.",
                    "Run the tests that create or update this object, and check integrations that write to it.")


def check_apex(root: Path, head: str, comp: dict, files: list[str], changed: list[dict], signals: Signals) -> None:
    source = file_at(root, head, comp["file"])
    if is_test_class(source):
        return
    if re.search(r"\b(interface|enum)\s+" + re.escape(comp["name"]) + r"\b", source):
        return
    tests = [f for f in files_mentioning(root, head, comp["name"], "*.cls")
             if f != comp["file"] and is_test_class(file_at(root, head, f))]
    if not tests:
        signals.add("apex-without-test", "medium", "No test class refers to this Apex code", comp, comp["file"],
                    "A deployment to production needs 75 % coverage; with RunSpecifiedTests every deployed class "
                    "needs it from the specified tests alone. For a trigger the tests have to do DML on its object.",
                    "Add a test class, or name the test that covers this code if it does so indirectly.")
        return
    changed_files = {c["file"] for c in changed}
    comp["tests"] = [{"file": t, "changedInThisPullRequest": t in changed_files} for t in tests[:10]]


def merge_same_reference(items: list[dict]) -> list[dict]:
    """One missing name used in twelve files is one finding, not twelve."""
    merged: dict[tuple, dict] = {}
    out = []
    for item in items:
        if item["rule"] not in ("reference-not-in-source", "package-reference"):
            out.append(item)
            continue
        key = (item["rule"], item["why"])
        if key in merged:
            first = merged[key]
            first["occurrences"] += 1
            if item["component"] not in first["alsoIn"] and item["component"] != first["component"] and len(first["alsoIn"]) < 10:
                first["alsoIn"].append(item["component"])
        else:
            merged[key] = {**item, "occurrences": 1, "alsoIn": []}
            out.append(merged[key])
    return out


def build(root: Path, base: str, head: str) -> dict:
    files = files_at(root, head)
    comps = changed_components(root, base, head)
    signals = Signals()
    for comp in comps:
        if comp["change"] == "deleted":
            check_deleted(root, head, comp, files, signals)
            continue
        check_references(root, base, head, comp, files, signals)
        if comp["type"] == "CustomField":
            check_field(root, base, head, comp, signals)
        elif comp["type"] == "ValidationRule":
            check_validation_rule(root, base, head, comp, signals)
        elif comp["type"] in ("ApexClass", "ApexTrigger"):
            check_apex(root, head, comp, files, comps, signals)
        elif comp["type"] == "Profile":
            signals.add("profile-change", "low", "A profile is part of the change", comp, comp["file"],
                        "A profile deploys only the permissions for components that are in the same deployment, and "
                        "profiles retrieved from one org rarely match another. This is a common source of "
                        "'no CustomField named ... found' and 'Unknown user permission' errors.",
                        "Prefer a permission set for the new access. If the profile has to change, deploy it together "
                        "with the components it refers to.")
        elif comp["type"] == "Flow":
            new = file_at(root, head, comp["file"])
            if xml_value(new, "status") == "Active" and comp["change"] == "modified":
                signals.add("active-flow-change", "medium", "An active flow is modified", comp, "<status>Active</status>",
                            "The change goes live as a new active version, or the deployment fails if the org does "
                            "not deploy flows as active. Paused interviews keep running on the old version.",
                            "Check the org setting 'Deploy processes and flows as active' and the flow tests.")

    signals.items = merge_same_reference(signals.items)

    by_type: dict[str, int] = {}
    for c in comps:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    order = {s: i for i, s in enumerate(SEVERITIES)}
    signals.items.sort(key=lambda s: (order[s["severity"]], s["rule"], s["component"] or ""))
    return {
        "range": {"base": base, "head": head},
        "summary": {
            "components": len(comps),
            "added": sum(c["change"] == "added" for c in comps),
            "modified": sum(c["change"] in ("modified", "renamed") for c in comps),
            "deleted": sum(c["change"] == "deleted" for c in comps),
            "byType": by_type,
        },
        "components": comps[:300],
        "signals": signals.items,
        "totals": {s: sum(x["severity"] == s for x in signals.items) for s in SEVERITIES},
        "legend": {
            "signals": "Each signal is a fact about the source that often leads to a failed or harmful deployment. "
                       "It is not a prediction: no org was consulted.",
            "severity": "high: likely to fail the deployment or lose data. medium: fails under common conditions. "
                        "low: worth a look.",
            "reference-not-in-source": "The name is not defined anywhere in the repository. It may still exist in "
                                       "the org (created by hand, standard feature, unmanaged leftovers).",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="git ref the pull request is compared against")
    ap.add_argument("--head", default="HEAD", help="git ref of the pull request (default: HEAD)")
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    context = build(Path(args.repo_dir).resolve(), args.base, args.head)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(context, indent=2, ensure_ascii=False) + "\n")
    t = context["totals"]
    print(f"preflight-context: {context['summary']['components']} component(s) changed, "
          f"{t['high']} high / {t['medium']} medium / {t['low']} low signal(s) -> {out}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"components={context['summary']['components']}\n")
            fh.write(f"signals={len(context['signals'])}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
