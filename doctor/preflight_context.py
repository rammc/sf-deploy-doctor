#!/usr/bin/env python3
"""
Build the context file for the Deploy Doctor pre-flight agent.

Deterministic and read-only. Looks at what a pull request changes in a
Salesforce project and lists the things that typically make a deployment fail
or hurt after it succeeded: references to components that are not in the
source, deleted components that are still used, field type changes, new
required fields and validation rules, Apex without a test, profiles, flows,
imports of Lightning web components and Aura components that point to
Apex methods, fields, labels, resources or components the source lacks, and
custom fields and objects that a UI bundle (React, Angular) names.

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
        # <folder>/<bundle>/<file>: files directly below lwc/ (jsconfig.json) are not components
        if folder in parts[:-2]:
            return {"type": mtype, "name": parts[parts.index(folder) + 1]}
    for suffix, mtype in sorted(SUFFIX_TYPE.items(), key=lambda kv: -len(kv[0])):
        stem = base[: -len("-meta.xml")] if base.endswith("-meta.xml") and not suffix.endswith("-meta.xml") else base
        if stem.endswith(suffix):
            name = stem[: -len(suffix)]
            if "objects" in parts and mtype != "CustomObject":
                name = f"{parts[parts.index('objects') + 1]}.{name}"
            return {"type": mtype, "name": name}
    return None


def bundle_dir(path: str) -> str:
    """force-app/main/default/lwc/card/card.js -> force-app/main/default/lwc/card/"""
    parts = path.split("/")
    for folder in BUNDLE_DIRS.values():
        if folder in parts[:-2]:
            return "/".join(parts[: parts.index(folder) + 2]) + "/"
    return os.path.dirname(path) + "/"


def changed_components(root: Path, base: str, head: str, files: list[str] | None = None) -> list[dict]:
    out, seen = [], set()
    bundles: dict[str, dict] = {}
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
        if comp["type"] in BUNDLE_DIRS:
            # A bundle is one component made of several files. Its change is decided for the
            # bundle as a whole below: deleting one stylesheet does not delete the component.
            folder = bundle_dir(path)
            entry = bundles.get(folder)
            if not entry:
                entry = bundles[folder] = {**comp, "change": "modified", "file": folder, "oldFile": None,
                                           "files": [], "_changes": []}
                out.append(entry)
            entry["_changes"].append(change)
            if change != "deleted":
                entry["files"].append(path)
            continue
        key = (comp["type"], comp["name"], change)
        if key in seen:  # a class and its -meta.xml are one component
            if not path.endswith("-meta.xml"):
                next(c for c in out if (c["type"], c["name"], c["change"]) == key)["file"] = path
            continue
        seen.add(key)
        out.append({**comp, "change": change, "file": path, "oldFile": old_path if status == "R" else None})
    at_head = files if files is not None else files_at(root, head)
    for folder, entry in bundles.items():
        changes = entry.pop("_changes")
        remaining = [f for f in at_head if f.startswith(folder)]
        if not remaining:
            entry["change"] = "deleted"
        elif all(c == "added" for c in changes) and len(remaining) == len(entry["files"]):
            entry["change"] = "added"
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
    if comp["type"] == "LightningComponentBundle":
        # Other components use it as <c-my-card> in markup and as 'c/myCard' in imports.
        tag = "c-" + re.sub(r"[A-Z]", lambda m: "-" + m.group(0).lower(), leaf)
        users = sorted({f for f, _, line in grep_lines(root, head, tag) if re.search(re.escape(tag) + r"(?![a-z0-9_-])", line)}
                       | {f for f, _, line in grep_lines(root, head, f"c/{leaf}") if re.search(rf"c/{re.escape(leaf)}\b", line)})
    else:
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


# ── Lightning web components and Aura ───────────────────────────────────────

# import x from '@salesforce/<kind>/<target>'
SF_IMPORT = re.compile(r"""['"]@salesforce/([A-Za-z]+)/([^'"]+)['"]""")
# import x from 'c/other'  or  import('c/other')
MODULE_IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*['"]c/([A-Za-z0-9_]+)['"]""")
LWC_TAG = re.compile(r"<c-([a-z0-9_]+(?:-[a-z0-9_]+)*)")
AURA_TAG = re.compile(r"<c:([A-Za-z0-9_]+)")
AURA_CONTROLLER = re.compile(r"""\bcontroller\s*=\s*["']([A-Za-z0-9_.]+)["']""")
SCRIPT_SUFFIXES = (".js", ".ts", ".mjs")
# @salesforce/<kind> -> (folder, file suffix) of the component the import needs
IMPORT_SOURCES = {
    "resourceUrl": ("staticresources", ".resource-meta.xml"),
    "contentAssetUrl": ("contentassets", ".asset-meta.xml"),
    "customPermission": ("customPermissions", ".customPermission-meta.xml"),
    "messageChannel": ("messageChannels", ".messageChannel-meta.xml"),
}


def camel(tag: str) -> str:
    """c-my-child -> myChild"""
    head, *rest = tag.split("-")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def has_file(files: list[str], folder: str, basename: str) -> bool:
    tail = f"/{folder}/{basename}"
    return any(("/" + f).endswith(tail) for f in files)


def has_bundle(files: list[str], name: str, folders=("lwc",)) -> bool:
    return any(f"/{folder}/{name}/" in "/" + f for f in files for folder in folders)


def strip_script_noise(line: str) -> str:
    stripped = line.lstrip()
    return "" if stripped.startswith(("//", "*", "/*")) else line


def strip_apex_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source or "", flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


def aura_enabled_methods(source: str) -> set[str]:
    """Lower-cased names of the methods of an Apex class that carry @AuraEnabled."""
    source = strip_apex_comments(source)
    source = re.sub(r"@(?!AuraEnabled\b)\w+\s*\([^)]*\)", "", source, flags=re.I)
    found = re.finditer(r"@AuraEnabled(?:\s*\([^)]*\))?([^(;{}=]*?)\b([A-Za-z_]\w*)\s*\(", source, flags=re.I)
    return {m.group(2).lower() for m in found}


def declares_method(source: str, method: str) -> bool:
    return bool(re.search(r"\b" + re.escape(method) + r"\s*\(", strip_apex_comments(source), flags=re.I))


def apex_class_file(files: list[str], name: str) -> str | None:
    wanted = name.lower() + ".cls"
    hits = [f for f in files if os.path.basename(f).lower() == wanted]
    return sorted(hits, key=len)[0] if hits else None


def custom_labels(root: Path, head: str, files: list[str]) -> set[str]:
    names: set[str] = set()
    for f in files:
        if f.endswith(".labels-meta.xml"):
            names.update(re.findall(r"<fullName>([^<]+)</fullName>", file_at(root, head, f)))
        elif f.endswith(".label-meta.xml"):  # decomposed labels: one file per label
            names.add(os.path.basename(f)[: -len(".label-meta.xml")])
    return names


def grep_lines(root: Path, ref: str, text: str, *pathspec: str) -> list[tuple[str, int, str]]:
    """(file, line number, line) for every line at `ref` that contains `text`."""
    out = git(root, "grep", "-n", "-F", "-I", text, ref, "--", *(pathspec or (".",)), check=False)
    hits = []
    for raw in out.splitlines():
        cols = raw.split(":", 3)
        if len(cols) == 4 and cols[2].isdigit():
            hits.append((cols[1], int(cols[2]), cols[3]))
    return hits


class Ui:
    """Checks of what Lightning components import from the rest of the source."""

    def __init__(self, root: Path, base: str, head: str, files: list[str], signals: Signals) -> None:
        self.root, self.base, self.head, self.files, self.signals = root, base, head, files, signals
        self._labels: set[str] | None = None
        self._apex: dict[tuple[str, str], str] = {}
        self.seen: set[tuple] = set()

    def labels(self) -> set[str]:
        if self._labels is None:
            self._labels = custom_labels(self.root, self.head, self.files)
        return self._labels

    def apex_source(self, ref: str, path: str) -> str:
        if (ref, path) not in self._apex:
            self._apex[(ref, path)] = file_at(self.root, ref, path)
        return self._apex[(ref, path)]

    def missing(self, comp: dict, file: str, line: int, shown: str, name: str, what: str) -> None:
        if (comp["name"], name) in self.seen:
            return
        self.seen.add((comp["name"], name))
        self.signals.add("reference-not-in-source", "high", "Reference to a component that is not in the repository",
                         {**comp, "file": file}, shown.strip(),
                         f"`{name}` ({what}) is not defined anywhere in the source. The deployment only works if it "
                         "already exists in the target org; otherwise it fails with a missing-dependency error.",
                         f"Add `{name}` to this change, or confirm that it exists in every target org (and why it is "
                         "not in the repository).", line)

    def package(self, comp: dict, file: str, line: int, shown: str, name: str) -> None:
        if (comp["name"], name) in self.seen:
            return
        self.seen.add((comp["name"], name))
        self.signals.add("package-reference", "low", "Reference to a component of a managed package",
                         {**comp, "file": file}, shown.strip(),
                         "The name carries a namespace prefix, so it comes from a managed package.",
                         f"Make sure the package that provides `{name}` is installed in every target org.", line)

    def apex_import(self, comp: dict, file: str, line: int, shown: str, target: str) -> None:
        parts = target.split(".")
        if len(parts) == 3:  # namespace.Class.method
            self.package(comp, file, line, shown, target)
            return
        if len(parts) != 2:
            return
        cls, method = parts
        path = apex_class_file(self.files, cls)
        if not path:
            self.missing(comp, file, line, shown, cls, "Apex class")
            return
        source = self.apex_source(self.head, path)
        if method.lower() in aura_enabled_methods(source):
            return
        if (comp["name"], target) in self.seen:
            return
        self.seen.add((comp["name"], target))
        declared = declares_method(source, method)
        self.signals.add(
            "apex-method-not-exposed", "high",
            "A component imports an Apex method that is not available to it", {**comp, "file": file}, shown.strip(),
            (f"`{cls}` declares `{method}` without `@AuraEnabled`." if declared
             else f"`{cls}` (`{path}`) has no method `{method}`.")
            + " The deployment of the component fails with \"Unable to find Apex action method referenced as "
            f"'{target}'\".",
            (f"Annotate `{cls}.{method}` with `@AuraEnabled`, or import a method that is." if declared
             else f"Add the method to `{cls}`, or correct the name in the import."), line)

    def script_line(self, comp: dict, file: str, line: int, text: str) -> None:
        text = strip_script_noise(text)
        for m in SF_IMPORT.finditer(text):
            kind, target = m.group(1), m.group(2)
            if kind in ("apex", "apexContinuation"):
                self.apex_import(comp, file, line, text, target)
            elif kind == "schema":
                self.schema_import(comp, file, line, text, target)
            elif kind == "label":
                namespace, _, label = target.partition(".")
                if namespace != "c":
                    self.package(comp, file, line, text, target)
                elif label and label not in self.labels():
                    self.missing(comp, file, line, text, label, "custom label")
            elif kind in IMPORT_SOURCES:
                folder, suffix = IMPORT_SOURCES[kind]
                name = target[:-3] if kind == "messageChannel" and target.endswith("__c") else target
                if "__" in name:
                    self.package(comp, file, line, text, target)
                elif not has_file(self.files, folder, name + suffix):
                    self.missing(comp, file, line, text, name, IMPORT_LABEL[kind])
        for m in MODULE_IMPORT.finditer(text):
            if not has_bundle(self.files, m.group(1)):
                self.missing(comp, file, line, text, m.group(1), "Lightning web component")

    def schema_import(self, comp: dict, file: str, line: int, shown: str, target: str) -> None:
        obj, _, rest = target.partition(".")
        first = rest.split(".")[0] if rest else ""
        for name in (obj, first):
            if re.match(r"[A-Za-z0-9]+__[A-Za-z0-9_]+__(c|r|mdt|e)$", name):
                self.package(comp, file, line, shown, target)
                return
        if obj.endswith(("__c", "__mdt", "__e")) and not exists_in_source(self.files, obj):
            self.missing(comp, file, line, shown, obj, "object")
            return
        if first.endswith(("__c", "__r")):
            field = first[:-1] + "c"  # a relationship Owner__r belongs to the field Owner__c
            if not exists_in_source(self.files, f"{obj}.{field}"):
                self.missing(comp, file, line, shown, f"{obj}.{field}", "field")

    def markup_line(self, comp: dict, file: str, line: int, text: str) -> None:
        if file.endswith(".html"):
            for m in LWC_TAG.finditer(text):
                name = camel(m.group(1))
                if not has_bundle(self.files, name):
                    self.missing(comp, file, line, text, name, "Lightning web component")
            return
        for m in AURA_TAG.finditer(text):
            if not has_bundle(self.files, m.group(1), ("aura", "lwc")):
                self.missing(comp, file, line, text, m.group(1), "Lightning component")
        for m in AURA_CONTROLLER.finditer(text):
            name = m.group(1)
            if "." in name:
                self.package(comp, file, line, text, name)
            elif not apex_class_file(self.files, name):
                self.missing(comp, file, line, text, name, "Apex class")

    def check_bundle(self, comp: dict) -> None:
        """What the changed files of a bundle newly refer to."""
        for file in comp.get("files", []):
            if "/__tests__/" in file or "/__mocks__/" in file:
                continue  # Jest files are not deployed
            is_script = file.endswith(SCRIPT_SUFFIXES)
            if not is_script and not file.endswith((".html", ".cmp", ".app", ".evt", ".intf")):
                continue
            for number, line in added_lines(self.root, self.base, self.head, file):
                (self.script_line if is_script else self.markup_line)(comp, file, number, line)

    def check_apex_change(self, comp: dict) -> None:
        """A modified class must still offer every method the components import from it."""
        before = aura_enabled_methods(self.apex_source(self.base, comp["file"]))
        after = aura_enabled_methods(self.apex_source(self.head, comp["file"]))
        if not before - after:
            return
        for file, number, line in grep_lines(self.root, self.head, f"@salesforce/apex/{comp['name']}."):
            user = component_of(file)
            if not user or not file.endswith(SCRIPT_SUFFIXES) or "/__tests__/" in file:
                continue
            for m in SF_IMPORT.finditer(strip_script_noise(line)):
                cls, _, method = m.group(2).partition(".")
                if cls == comp["name"] and method.lower() in before - after:
                    self.apex_import(user, file, number, line, m.group(2))


# ── UI bundles: React, Angular and other front-end applications ─────────────

# ns__Name__c: a component of a managed package
NAMESPACED_NAME = re.compile(r"\b[A-Za-z][A-Za-z0-9]*__[A-Za-z][A-Za-z0-9_]*?__(?:c|r|mdt|e)\b")
UI_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".graphql", ".gql", ".html")
# Not part of what the application sends to the org: dependencies, build output, tests.
UI_SKIP = re.compile(r"/(?:node_modules|dist|build|coverage|public|__tests__|__mocks__|e2e|tests?)/"
                     r"|\.(?:test|spec|stories|d)\.[a-z]+$|\.min\.js$")


def check_ui_bundle(root: Path, base: str, head: str, comp: dict, files: list[str], signals: Signals) -> None:
    """Custom fields and objects the application newly names that the source does not define.

    A UI bundle talks to the org at run time (GraphQL, REST), so nothing here fails the
    deployment: the query fails later, in front of a user.
    """
    seen: set[str] = set()
    for file in comp.get("files", []):
        if not file.endswith(UI_SOURCE_SUFFIXES) or UI_SKIP.search("/" + file):
            continue
        for number, line in added_lines(root, base, head, file):
            text = strip_script_noise(line)
            for m in NAMESPACED_NAME.finditer(text):
                if m.group(0) not in seen:
                    seen.add(m.group(0))
                    signals.add("package-reference", "low", "Reference to a component of a managed package",
                                {**comp, "file": file}, line.strip(),
                                "The name carries a namespace prefix, so it comes from a managed package.",
                                f"Make sure the package that provides `{m.group(0)}` is installed in every target org.",
                                number)
            for m in CUSTOM_NAME.finditer(NAMESPACED_NAME.sub("", text)):
                name = m.group(0)
                if m.group(2) == "r" or name in seen:  # relationship names are not component names
                    continue
                seen.add(name)
                if not exists_in_source(files, name):
                    signals.add("ui-reference-not-in-source", "medium",
                                "A front-end application uses a field or object that is not in the repository",
                                {**comp, "file": file}, line.strip(),
                                f"`{name}` is not defined anywhere in the source. The bundle deploys regardless, but a "
                                "query or request that names it fails at run time in an org that does not have it, "
                                "or for a user who may not see it.",
                                f"Add `{name}` to this change, or confirm that it exists in every target org and that "
                                "the users of the application have access to it.", number)


IMPORT_LABEL = {
    "resourceUrl": "static resource",
    "contentAssetUrl": "content asset",
    "customPermission": "custom permission",
    "messageChannel": "message channel",
}


def merge_same_reference(items: list[dict]) -> list[dict]:
    """One missing name used in twelve files is one finding, not twelve."""
    merged: dict[tuple, dict] = {}
    out = []
    for item in items:
        if item["rule"] not in ("reference-not-in-source", "package-reference", "ui-reference-not-in-source"):
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
    comps = changed_components(root, base, head, files)
    signals = Signals()
    ui = Ui(root, base, head, files, signals)
    for comp in comps:
        if comp["change"] == "deleted":
            check_deleted(root, head, comp, files, signals)
            continue
        if comp["type"] == "UIBundle":
            check_ui_bundle(root, base, head, comp, files, signals)
            continue
        if comp["type"] in BUNDLE_DIRS:
            ui.check_bundle(comp)
            continue
        check_references(root, base, head, comp, files, signals)
        if comp["type"] == "ApexClass" and comp["change"] == "modified":
            ui.check_apex_change(comp)
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
            "ui-reference-not-in-source": "A UI bundle (React, Angular or another front-end application) names a "
                                          "custom field or object that the repository does not define. This is a "
                                          "run-time risk, not a deployment failure.",
            "apex-method-not-exposed": "A Lightning component imports an Apex method that the class in the source "
                                       "does not declare, or declares without @AuraEnabled. An org reports such "
                                       "errors only in part, so a failed deployment rarely shows all of them.",
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
