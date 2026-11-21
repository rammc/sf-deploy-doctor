#!/usr/bin/env python3
"""
Build the context file for the Deploy Doctor agent.

Deterministic and read-only. Takes the output of a failed Salesforce
deployment or validation (`sf project deploy start|validate`), either from the
logs of a GitHub Actions run or from a file, and turns it into one JSON
document: every component failure, test failure and coverage warning, matched
against a catalog of known error patterns and against the repository (which
file is it, was that file changed in this run, does a referenced component
exist in the source).

The agent reads this file. It never gets the raw log and never talks to the
org, so numbers and facts in its report come from here.

Usage:
  deploy_context.py --run-id 123456 --out doctor/context.json
  deploy_context.py --log-file deploy.log [--base SHA --head SHA] --out ctx.json
  deploy_context.py --result-json deploy-result.json --out ctx.json

Env: GH_TOKEN (CI) or a logged-in `gh` for --run-id; GITHUB_REPOSITORY optional.
Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.parse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT_ECHO = re.compile(r"^(?:[^\t]*\t[^\t]*\t)?(?:\ufeff)?(?:\d{4}-\d{2}-\d{2}T[\d:.]+Z )?\x1b\[36;1m")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# `gh run view --log` prefixes "job<TAB>step<TAB>"; raw job logs start with a timestamp.
TIMESTAMP = re.compile(r"^(?:﻿)?\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SKIP_DIRS = {".git", "node_modules", ".sf", ".sfdx", "dist", "coverage", ".deploy-doctor"}
MAX_EXCERPT = 1200

# Metadata type -> how its source file is named. Enough for the common types;
# everything else falls back to a search by component name.
TYPE_SUFFIX = {
    "ApexClass": ".cls",
    "ApexTrigger": ".trigger",
    "ApexPage": ".page",
    "ApexComponent": ".component",
    "CustomObject": ".object-meta.xml",
    "CustomField": ".field-meta.xml",
    "ValidationRule": ".validationRule-meta.xml",
    "RecordType": ".recordType-meta.xml",
    "ListView": ".listView-meta.xml",
    "WebLink": ".webLink-meta.xml",
    "CompactLayout": ".compactLayout-meta.xml",
    "FieldSet": ".fieldSet-meta.xml",
    "PermissionSet": ".permissionset-meta.xml",
    "PermissionSetGroup": ".permissionsetgroup-meta.xml",
    "Profile": ".profile-meta.xml",
    "Flow": ".flow-meta.xml",
    "Layout": ".layout-meta.xml",
    "FlexiPage": ".flexipage-meta.xml",
    "CustomTab": ".tab-meta.xml",
    "CustomApplication": ".app-meta.xml",
    "CustomMetadata": ".md-meta.xml",
    "CustomLabels": ".labels-meta.xml",
    "QuickAction": ".quickAction-meta.xml",
    "GlobalValueSet": ".globalValueSet-meta.xml",
    "StandardValueSet": ".standardValueSet-meta.xml",
    "Queue": ".queue-meta.xml",
    "Group": ".group-meta.xml",
    "Role": ".role-meta.xml",
    "SharingRules": ".sharingRules-meta.xml",
    "NamedCredential": ".namedCredential-meta.xml",
    "RemoteSiteSetting": ".remoteSite-meta.xml",
    "EmailTemplate": ".email-meta.xml",
    "Report": ".report-meta.xml",
    "Dashboard": ".dashboard-meta.xml",
    "StaticResource": ".resource-meta.xml",
}
# Components that are a folder of files. UI bundles hold a whole front-end application
# (React, Angular, Vue or anything else that builds to static files).
BUNDLE_DIRS = {"LightningComponentBundle": "lwc", "AuraDefinitionBundle": "aura", "UIBundle": "uiBundles"}


# ── input: log text ─────────────────────────────────────────────────────────

def clean_lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        # GitHub echoes the script of a step in bold cyan before running it. That is the
        # command, not its output: `echo "::error::..."` in a script is not an error.
        if SCRIPT_ECHO.search(raw):
            continue
        line = ANSI.sub("", raw)
        if line.count("\t") >= 2:  # gh run view --log: job \t step \t message
            line = line.split("\t", 2)[2]
        out.append(TIMESTAMP.sub("", line).rstrip())
    return out


def redact(text: str) -> str:
    """Usernames and record ids from the org do not belong in an issue."""
    text = EMAIL.sub("<user>", text or "")
    # Aura descriptors look like URLs (markup://c:myCard). The safe-output filter of the
    # workflow redacts unknown URL schemes, so write them the way an import does: c/myCard.
    text = re.sub(r"\bmarkup://([A-Za-z0-9_]+):", r"\1/", text)
    return RECORD_ID.sub(lambda m: "<id>" if is_record_id(m.group(0)) else m.group(0), text)


# A record id is 15 characters, or 18 with the checksum. The key prefix (first three
# characters) differs per object and there are hundreds of them, so the shape decides:
# standard prefixes start with a digit, custom objects, custom metadata, external
# objects and knowledge with a, m, e or k followed by a digit.
RECORD_ID = re.compile(r"(?<![A-Za-z0-9_])[0-9A-Za-z]{15}(?:[A-Z0-5]{3})?(?![A-Za-z0-9_])")


def is_record_id(word: str) -> bool:
    body = word[:15]
    if not (body[0].isdigit() or (body[0] in "aemk" and body[1].isdigit())):
        return False
    # After the prefix come the instance and a zero-padded counter, so real ids carry "00" early.
    digits = sum(c.isdigit() for c in body)
    return "00" in body[3:10] and digits < 15 and not re.fullmatch(r"[0-9a-f]{15}|[0-9a-f]{18}", word)


def parse_table(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    """Read a box-drawing table that begins after `start`; returns rows and the end index.

    A cell that wraps continues on the next line with the leading cells empty,
    so those lines are appended to the previous row.
    """
    rows: list[list[str]] = []
    i = start
    seen_table = False
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(("┌", "├", "└", "+-", "|-")):
            seen_table = True
            if line.startswith("└"):
                return rows, i + 1
        elif line.startswith(("│", "|")):
            seen_table = True
            cells = [c.strip() for c in re.split(r"[│|]", line.strip("│|"))]
            if rows and len(cells) >= 2 and not cells[0] and not cells[1]:
                for k, cell in enumerate(cells[: len(rows[-1])]):
                    if cell:
                        rows[-1][k] = (rows[-1][k] + "\n" + cell).strip()
            else:
                rows.append(cells)
        elif seen_table or (line and i > start + 2):
            return rows, i
        i += 1
    return rows, i


def parse_plain_table(lines: list[str], start: int) -> tuple[list[dict], int]:
    """Read the table the CLI prints without a terminal (CI): columns aligned by position.

     Type         Name     Problem                 Line:Column
    ------------------------------------------------------------
     ApexClass    Foo      Variable does not ...   4:18
                           continuation of the problem text
    """
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not re.match(r"\s*Type\s+Name\s+Problem", lines[i]):
        return [], start
    header = lines[i]
    name_at, problem_at = header.index("Name"), header.index("Problem")
    pos_at = header.find("Line:Column")
    i += 1
    if i < len(lines) and set(lines[i].strip()) <= {"-", "─"}:
        i += 1
    rows: list[dict] = []
    while i < len(lines) and lines[i].strip():
        line = lines[i]
        head = line[:problem_at]
        if not head.strip():
            if rows:
                rows[-1]["problem"] += "\n" + line.strip()
        elif re.match(r"\s*\S+\s+\S", head):
            problem, position = line[problem_at:], ""
            if pos_at > 0 and re.fullmatch(r"\d+:\d+", line[pos_at:].strip() or "-"):
                problem, position = line[problem_at:pos_at], line[pos_at:].strip()
            rows.append({"type": line[:name_at].strip(), "name": line[name_at:problem_at].strip(),
                         "problem": problem.strip(), "line:column": position})
        else:
            break
        i += 1
    return rows, i


def split_position(problem: str) -> tuple[str, int | None, int | None]:
    m = re.search(r"\s*\((\d+):(\d+)\)\s*$", problem)
    if not m:
        return problem, None, None
    return problem[: m.start()].rstrip(), int(m.group(1)), int(m.group(2))


def parse_log(text: str) -> dict:
    return parse_log_lines(clean_lines(text))


def parse_log_lines(lines: list[str]) -> dict:
    out = {"deployment": {}, "componentFailures": [], "testFailures": [], "coverageWarnings": [], "otherErrors": []}
    dep = out["deployment"]
    joined = "\n".join(lines)

    m = re.search(r"^\s*Status:\s*(Succeeded|SucceededPartial|Failed|Canceled|Canceling|InProgress|Pending)\b", joined, re.M)
    if m:
        dep["status"] = m.group(1)
    m = re.search(r"Deploy ID:\s*(0Af\w+)", joined)
    if m:
        dep["deployId"] = m.group(1)
    dep["dryRun"] = bool(re.search(r"\(dry-run\)|Dry-run complete|deploy validate|Validating Deployment", joined))
    # A CI log repeats the progress line; the last one is the final state.
    progress = re.findall(r"Components:\s*(\d+)/(\d+)", joined)
    if progress:
        dep["componentsDeployed"], dep["componentsTotal"] = int(progress[-1][0]), int(progress[-1][1])
    progress = re.findall(r"Tests:\s*(\d+)/(\d+)", joined)
    if progress:
        dep["testsCompleted"], dep["testsTotal"] = int(progress[-1][0]), int(progress[-1][1])

    summary = re.search(r"Test Results Summary\s+Passing:\s*(\d+)\s+Failing:\s*(\d+)\s+Total:\s*(\d+)", joined)
    if summary:
        dep["tests"] = {"passing": int(summary.group(1)), "failing": int(summary.group(2)), "total": int(summary.group(3))}

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r"Component Failures \[\d+\]", line):
            plain, end = parse_plain_table(lines, i + 1)
            if plain:
                cells, i = plain, end
            else:
                rows, i = parse_table(lines, i + 1)
                header = [h.lower() for h in rows[0]] if rows else []
                cells = [dict(zip(header, row)) for row in rows[1:]]
            for cell in cells:
                problem, ln, col = split_position(cell.get("problem", ""))
                pos = re.match(r"(\d+):(\d+)", cell.get("line:column", ""))
                if pos:
                    ln, col = int(pos.group(1)), int(pos.group(2))
                out["componentFailures"].append({
                    "type": cell.get("type", ""), "name": cell.get("name", ""),
                    "problem": problem, "line": ln, "column": col,
                })
            continue
        if re.match(r"Test Failures \[\d+\]", line):
            i += 1
            current = None
            while i < len(lines):
                s = lines[i].strip()
                if s.startswith("•"):
                    current = {"test": s.lstrip("• ").strip(), "message": "", "stackTrace": ""}
                    out["testFailures"].append(current)
                elif current is not None and s.startswith("message:"):
                    current["message"] = s[len("message:"):].strip()
                elif current is not None and s.startswith("stacktrace:"):
                    current["_in_stack"] = True
                    rest = s[len("stacktrace:"):].strip()
                    if rest:
                        current["stackTrace"] = rest
                elif current is not None and current.get("_in_stack") and s:
                    if re.match(r"(Test Results Summary|Component Failures|Dry-run complete)", s):
                        break
                    current["stackTrace"] = (current["stackTrace"] + "\n" + s).strip()
                elif re.match(r"(Test Results Summary|Dry-run complete)", s):
                    break
                i += 1
            for t in out["testFailures"]:
                t.pop("_in_stack", None)
            continue
        i += 1

    human = parse_human_tests(lines)
    if human:
        known = {t["test"] for t in out["testFailures"]}
        out["testFailures"] += [t for t in human["failures"] if t["test"] not in known]
        if human["total"] or human["failures"]:
            dep.setdefault("tests", {"passing": max(human["total"] - len(human["failures"]), 0),
                                     "failing": len(human["failures"]), "total": human["total"]})

    # Coverage warnings are printed as oclif warnings and wrap across lines.
    warn = re.sub(r"\n\s*›\s+", " ", joined)
    for m in re.finditer(r"Warning:\s*(?:([\w.]+) - )?((?:Test coverage of selected|Average test coverage across)[^\n]+?required)", warn):
        out["coverageWarnings"].append({"name": m.group(1) or "(org-wide)", "message": re.sub(r"\s+", " ", m.group(2))})

    if not out["componentFailures"] and not out["testFailures"] and not out["coverageWarnings"]:
        # Nothing structured: keep the lines that look like the actual error.
        seen = set()
        for k, line in enumerate(lines):
            s = line.strip()
            if s.startswith("##[group]"):
                continue  # the title of a step, not its output
            if re.search(r"(##\[error\]|::error::|^Error(?: \([^)]+\))?:|^ERROR|ERROR running|Deploy failed|\bError: )", s) and s not in seen:
                seen.add(s)
                if re.match(r"Error(?: \([^)]+\))?:", s):
                    # The CLI continues its message on the following lines, up to a blank one.
                    for more in lines[k + 1:k + 7]:
                        if not more.strip() or more.startswith("##["):
                            break
                        s += " " + more.strip()
                out["otherErrors"].append(s[:MAX_EXCERPT])
        out["otherErrors"] = out["otherErrors"][:20]
        # What the job printed right before it gave up, for errors no pattern above catches.
        first_error = next((k for k, l in enumerate(lines) if "##[error]" in l), len(lines))
        tail = [l.strip() for l in lines[:first_error] if l.strip() and not l.startswith("##[")]
        out["logTail"] = tail[-15:]
    return out


# ── input: sf --json result ─────────────────────────────────────────────────

def as_list(value) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def parse_result_json(data: dict) -> dict:
    result = data.get("result") or data
    details = result.get("details") or {}
    rt = details.get("runTestResult") or {}
    out = {
        "deployment": {
            "status": result.get("status"),
            "deployId": result.get("id"),
            "dryRun": bool(result.get("checkOnly")),
            "componentsDeployed": result.get("numberComponentsDeployed"),
            "componentsTotal": result.get("numberComponentsTotal"),
            "testsCompleted": result.get("numberTestsCompleted"),
            "testsTotal": result.get("numberTestsTotal"),
            "tests": {"passing": int(rt.get("numTestsRun") or 0) - int(rt.get("numFailures") or 0),
                      "failing": int(rt.get("numFailures") or 0), "total": int(rt.get("numTestsRun") or 0)},
        },
        "componentFailures": [], "testFailures": [], "coverageWarnings": [], "otherErrors": [],
    }
    for f in as_list(details.get("componentFailures")):
        out["componentFailures"].append({
            "type": f.get("componentType", ""), "name": f.get("fullName", ""),
            "problem": f.get("problem", ""),
            "line": int(f["lineNumber"]) if str(f.get("lineNumber", "")).isdigit() else None,
            "column": int(f["columnNumber"]) if str(f.get("columnNumber", "")).isdigit() else None,
        })
    for f in as_list(rt.get("failures")):
        out["testFailures"].append({
            "test": f"{f.get('name', '')}.{f.get('methodName', '')}",
            "message": f.get("message", ""), "stackTrace": f.get("stackTrace") or "",
        })
    for w in as_list(rt.get("codeCoverageWarnings")):
        out["coverageWarnings"].append({"name": w.get("name") or "(org-wide)", "message": w.get("message", "")})
    if not out["componentFailures"]:
        # The CLI also lists each file with its state; some results carry the errors only there.
        for f in as_list(result.get("files")):
            if isinstance(f, dict) and f.get("state") == "Failed" and f.get("error"):
                out["componentFailures"].append({
                    "type": f.get("type", ""), "name": f.get("fullName", ""), "problem": f.get("error", ""),
                    "line": int(f["lineNumber"]) if str(f.get("lineNumber", "")).isdigit() else None,
                    "column": int(f["columnNumber"]) if str(f.get("columnNumber", "")).isdigit() else None,
                })
    # A deployment can fail as a whole, without naming a component (a locked record, a timeout).
    if result.get("errorMessage"):
        out["otherErrors"].append(str(result["errorMessage"])[:MAX_EXCERPT])
    if data.get("message") and not (out["componentFailures"] or out["testFailures"] or out["otherErrors"]):
        out["otherErrors"].append(str(data.get("message"))[:MAX_EXCERPT])
    return out


def parse_test_json(data: dict) -> dict:
    """Result of `sf apex run test --result-format json`: tests that run as their own step."""
    result = data.get("result") or data
    summary = result.get("summary") or {}

    def number(key: str) -> int:
        return int(summary.get(key) or 0) if str(summary.get(key) or 0).isdigit() else 0

    out = {"deployment": {}, "componentFailures": [], "testFailures": [], "coverageWarnings": [], "otherErrors": [],
           "tests": {"passing": number("passing"), "failing": number("failing"), "total": number("testsRan"),
                     "outcome": summary.get("outcome")}}
    for t in as_list(result.get("tests")):
        if isinstance(t, dict) and str(t.get("Outcome", "")).lower() in ("fail", "compilefail"):
            name = t.get("FullName") or f"{(t.get('ApexClass') or {}).get('Name', '')}.{t.get('MethodName', '')}"
            out["testFailures"].append({"test": name, "message": t.get("Message") or "",
                                        "stackTrace": t.get("StackTrace") or ""})
    return out


def parse_human_tests(lines: list[str]) -> dict | None:
    """The table `sf apex run test` prints without --result-format json.

    Columns are aligned by position; a failed test continues on the following
    lines with its stack trace, the name column left empty.
    """
    failures, total, found = [], 0, False
    i = 0
    while i < len(lines):
        if lines[i].strip() != "=== Test Results":
            m = re.match(r"Tests Ran\s+(\d+)", lines[i].strip())
            if m and found:
                total += int(m.group(1))
            i += 1
            continue
        header = lines[i + 1] if i + 1 < len(lines) else ""
        cols = {name: header.find(name) for name in ("TEST NAME", "OUTCOME", "MESSAGE", "RUNTIME")}
        if cols["TEST NAME"] < 0 or cols["OUTCOME"] < 0 or cols["MESSAGE"] < 0:
            i += 1
            continue
        found = True
        end = cols["RUNTIME"] if cols["RUNTIME"] > 0 else None
        current = None
        i += 2
        while i < len(lines):
            line = lines[i]
            if line.startswith("===") or line.startswith("##["):
                break
            name = line[:cols["OUTCOME"]].strip()
            outcome = line[cols["OUTCOME"]:cols["MESSAGE"]].strip()
            text = line[cols["MESSAGE"]:end].strip() if len(line) > cols["MESSAGE"] else ""
            if name and not set(name) <= {"─", "-", " "}:
                current = None
                if outcome in ("Fail", "CompileFail"):
                    current = {"test": name, "message": text, "stackTrace": ""}
                    failures.append(current)
            elif current is not None and text:
                if re.match(r"(Class|Trigger|AnonymousBlock)[.:]|External entry point|\(System Code\)", text):
                    current["stackTrace"] = (current["stackTrace"] + "\n" + text).strip()
                else:
                    current["message"] = (current["message"] + " " + text).strip()
            i += 1
    return {"failures": failures, "total": total} if found else None


def is_cli_error(data: dict) -> bool:
    """The envelope the CLI prints with --json when a command fails: name, message, exit code."""
    return (isinstance(data.get("name"), str) and isinstance(data.get("message"), str)
            and (data.get("exitCode") not in (None, 0) or data.get("status") not in (None, 0))
            and not is_deploy_result(data))


def json_documents(lines: list[str]):
    """Pretty-printed JSON objects in a log: from a line that is `{` to the next line that is `}`."""
    start = None
    for i, line in enumerate(lines):
        if start is None:
            if line == "{":
                start = i
        elif line == "}":
            try:
                data = json.loads("\n".join(lines[start:i + 1]))
            except ValueError:
                data = None
            if isinstance(data, dict):
                yield data
            start = None


def is_deploy_result(data: dict) -> bool:
    result = data.get("result")
    return isinstance(result, dict) and ("details" in result or "numberComponentErrors" in result)


def is_test_result(data: dict) -> bool:
    result = data.get("result")
    return isinstance(result, dict) and isinstance(result.get("summary"), dict) and "outcome" in result["summary"]


def parse_text(text: str) -> dict:
    """Everything a log says about deployments and test runs.

    A job can deploy several times and run the tests as a separate step, so every
    result in the log counts, not only the first one.
    """
    lines = clean_lines(text)
    deploys, tests, cli_errors = [], [], []
    for data in json_documents(lines):
        if is_deploy_result(data):
            deploys.append(parse_result_json(data))
        elif is_test_result(data):
            tests.append(parse_test_json(data))
        elif is_cli_error(data):
            cli_errors.append(f"{data['name']}: {data['message']}"[:MAX_EXCERPT])
    if deploys or tests:
        failed = [d for d in deploys if d["deployment"].get("status") not in ("Succeeded", "SucceededPartial")
                  or d["componentFailures"] or d["testFailures"] or d["otherErrors"]]
        out = {"deployment": dict((failed or deploys or [{"deployment": {}}])[0]["deployment"]),
               "componentFailures": [], "testFailures": [], "coverageWarnings": [], "otherErrors": []}
        for part in failed + tests:
            for key in ("componentFailures", "testFailures", "coverageWarnings", "otherErrors"):
                for item in part[key]:
                    if item not in out[key]:
                        out[key].append(item)
        if deploys:
            out["deployment"]["resultsInLog"] = {"total": len(deploys), "failed": len(failed)}
        if tests:
            sums = [t["tests"] for t in tests]
            out["testRuns"] = {"runs": len(tests), "passing": sum(x["passing"] for x in sums),
                               "failing": sum(x["failing"] for x in sums), "total": sum(x["total"] for x in sums)}
            if out["testRuns"]["failing"]:
                current = out["deployment"].get("tests") or {"passing": 0, "failing": 0, "total": 0}
                out["deployment"]["tests"] = {k: int(current.get(k) or 0) + out["testRuns"][k]
                                              for k in ("passing", "failing", "total")}
        out["source"] = "log-json"
    else:
        out = parse_log_lines(lines)
        out["source"] = "log"
    for message in cli_errors:
        if message not in out["otherErrors"]:
            out["otherErrors"].append(message)
    steps = failed_steps(lines)
    out["failedSteps"] = steps[:MAX_FAILED_STEPS]
    for step in steps[MAX_FAILED_STEPS:]:
        step.pop("_raw")
    out["failedStepsOmitted"] = len(steps) - len(out["failedSteps"])
    # A failed step may print the human-readable result of a deployment while other
    # steps printed JSON. Read it too and keep what the JSON did not already say.
    for step in out["failedSteps"]:
        raw = step.pop("_raw")
        if out["source"] == "log" or len(raw) > 20000:
            continue
        extra = parse_log_lines(raw)
        known = {(f["type"], f["name"]) for f in out["componentFailures"]}
        out["componentFailures"] += [f for f in extra["componentFailures"] if (f["type"], f["name"]) not in known]
        tests = {t["test"] for t in out["testFailures"]}
        out["testFailures"] += [t for t in extra["testFailures"] if t["test"] not in tests]
        seen = {w["message"] for w in out["coverageWarnings"]}
        out["coverageWarnings"] += [w for w in extra["coverageWarnings"] if w["message"] not in seen]
    return out


ERRORISH = re.compile(r"(?i)\b(error|errors|failed|failure|exception|not found|timed? ?out|timeout|denied|invalid|"
                      r"cannot|can't|unable|refused|unauthori[sz]ed|forbidden|missing|exit code)\b")
MAX_FAILED_STEPS = 6  # per job
MAX_FAILED_STEPS_RUN = 24


def parse_jobs(logs: list[tuple[str, str]]) -> dict:
    """One context for a run with several failed jobs.

    A pipeline that deploys to several orgs (a matrix, or one job per environment)
    fails in each of them for its own reasons. Every job is read on its own, and
    every finding says in `jobs` where it occurred; the same finding in three jobs
    is listed once with three names. A run with one failed job reads as before.
    """
    if len(logs) == 1:
        return parse_text(logs[0][1])
    out = {"deployment": {}, "componentFailures": [], "testFailures": [], "coverageWarnings": [],
           "otherErrors": [], "failedSteps": [], "failedStepsOmitted": 0, "jobs": []}
    runs = {"runs": 0, "passing": 0, "failing": 0, "total": 0}
    for name, text in logs:
        part = parse_text(text)
        for key in ("componentFailures", "testFailures", "coverageWarnings"):
            for item in part[key]:
                same = next((x for x in out[key] if {k: v for k, v in x.items() if k != "jobs"} == item), None)
                if same:
                    same["jobs"].append(name)
                else:
                    out[key].append({**item, "jobs": [name]})
        for message in part["otherErrors"]:
            same = next((x for x in out["otherErrors"] if x["text"] == message), None)
            if same:
                same["jobs"].append(name)
            else:
                out["otherErrors"].append({"text": message, "jobs": [name]})
        room = max(0, MAX_FAILED_STEPS_RUN - len(out["failedSteps"]))
        out["failedSteps"] += [{"job": name, **step} for step in part["failedSteps"][:room]]
        out["failedStepsOmitted"] += part["failedStepsOmitted"] + max(0, len(part["failedSteps"]) - room)
        if "testRuns" in part:
            for key in runs:
                runs[key] += part["testRuns"][key]
        failing = (part["deployment"].get("tests") or {}).get("failing", 0)
        out["jobs"].append({"name": name, "source": part["source"], "deployment": part["deployment"],
                            "findings": {"componentFailures": len(part["componentFailures"]),
                                         "testFailures": max(len(part["testFailures"]), int(failing or 0)),
                                         "testFailuresWithDetails": len(part["testFailures"]),
                                         "coverageWarnings": len(part["coverageWarnings"]),
                                         "otherErrors": len(part["otherErrors"]),
                                         "failedSteps": len(part["failedSteps"])}})
        if "logTail" in part and "logTail" not in out:
            out["logTail"] = part["logTail"]
    # Which jobs a finding is NOT in is the fact a reader needs to tell a cause in the
    # change (every job) from a cause in one org. Stated here so nobody has to infer it.
    names = [j["name"] for j in out["jobs"]]
    for key in ("componentFailures", "testFailures", "coverageWarnings", "otherErrors"):
        for item in out[key]:
            item["inAllFailedJobs"] = len(item["jobs"]) == len(names)
            item["notInJobs"] = [n for n in names if n not in item["jobs"]]
    if runs["runs"]:
        out["testRuns"] = runs
    # The fields a reader of a single-job run expects stay where they are.
    first = next((j for j in out["jobs"] if j["deployment"].get("status") not in (None, "Succeeded")), None) \
        or next((j for j in out["jobs"] if j["deployment"]), None)
    if first:
        out["deployment"] = dict(first["deployment"])
        out["deployment"]["job"] = first["name"]
    out["source"] = "log-per-job"
    return out


def failed_steps(lines: list[str]) -> list[dict]:
    """What each failed step printed, for failures no parser above understands.

    GitHub marks the end of a failed step with `##[error]` and its beginning with
    `##[group]Run <command>`. Kept per step: the command, the lines that look like
    an error, and the last lines before it stopped. Warnings are counted, not kept:
    a deployment prints hundreds of them and they bury the one line that matters.
    """
    steps, last_end = [], 0
    for i, line in enumerate(lines):
        if "##[error]" not in line:
            continue
        error = line.split("##[error]", 1)[1].strip()
        if steps and i - last_end <= 1:  # a second ##[error] line of the same step
            steps[-1]["error"] += " | " + error
            last_end = i + 1
            continue
        start = i
        while start > last_end and not lines[start].startswith("##[group]Run "):
            start -= 1
        command = lines[start][len("##[group]Run "):].strip() if lines[start].startswith("##[group]Run ") else None
        # GitHub echoes the script and the environment first; the output starts after ##[endgroup].
        first = next((k + 1 for k in range(start, i) if lines[k].startswith("##[endgroup]")), start + 1)
        raw = lines[first:i]
        body = [l.strip() for l in raw if l.strip() and not l.startswith("##[")]
        warnings = [l for l in body if re.match(r"(?:›\s*)?Warning:", l)]
        kept = [l for l in body if not re.match(r"(?:›\s*)?Warning:", l)]
        signals = list(dict.fromkeys(l for l in kept if ERRORISH.search(l) and len(l) < 600))
        steps.append({"command": command, "error": error, "outputLines": len(body), "warnings": len(warnings),
                      "errorLines": signals[-25:], "lastLines": kept[-20:], "_raw": raw})
        last_end = i + 1
    return steps


# ── repository ──────────────────────────────────────────────────────────────

def index_repo(root: Path) -> list[str]:
    files = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            files.append(os.path.relpath(os.path.join(base, n), root))
    return files


def find_source(files: list[str], mtype: str, name: str) -> str | None:
    if not name:
        return None
    if mtype in BUNDLE_DIRS:
        marker = f"/{BUNDLE_DIRS[mtype]}/{name}/"
        hits = [f for f in files if marker in "/" + f]
        return os.path.dirname(hits[0]) + "/" if hits else None
    parent, _, leaf = name.rpartition(".")
    suffix = TYPE_SUFFIX.get(mtype)
    candidates = []
    for f in files:
        base = os.path.basename(f)
        if suffix and base == leaf + suffix:
            candidates.append(f)
        elif not suffix and (base.startswith(leaf + ".") or base.startswith(name + ".")):
            candidates.append(f)
    if parent:  # child of an object: prefer the file below that object's folder
        scoped = [f for f in candidates if f"/{parent}/" in "/" + f]
        candidates = scoped or candidates
    return sorted(candidates, key=len)[0] if candidates else None


def find_reference(files: list[str], ref: str) -> str | None:
    """Does a component with this API name exist anywhere in the source?

    `Account.Field__c` only counts when the file sits below the Account folder.
    """
    if not ref:
        return None
    parent, _, leaf = ref.rpartition(".")
    hits = [f for f in files
            if os.path.basename(f).startswith(leaf + ".") or f"/{leaf}/" in "/" + f]
    if parent:
        hits = [f for f in hits if f"/{parent}/" in "/" + f]
    return sorted(hits, key=len)[0] if hits else None


def changed_files(root: Path, base: str | None, head: str | None) -> list[str] | None:
    """The files between base and head, or None when the range is not known.

    Without a base there is no guess: the last commit alone says nothing about a
    release branch with forty commits, and "not changed" would then be wrong.
    """
    if not head or not base:
        return None
    try:
        out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", f"{base}...{head}"],
                             check=True, capture_output=True, text=True).stdout
        return [l for l in out.splitlines() if l]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# ── classification ──────────────────────────────────────────────────────────

def load_catalog(path: Path | None = None) -> dict:
    return json.loads((path or HERE / "catalog.json").read_text())


def classify(text: str, catalog: dict) -> dict:
    for p in catalog["patterns"]:
        m = re.search(p["pattern"], text or "", re.I)
        if m:
            refs = {k: m.group(v) for k, v in p.get("refs", {}).items() if m.group(v)}
            return {"patternId": p["id"], "category": p["category"], "title": p["title"],
                    "advice": p["advice"], "refs": refs}
    return {"patternId": None, "category": "unknown", "title": None, "advice": None, "refs": {}}


def reference_name(refs: dict) -> str | None:
    """API name of the component a failure says is missing."""
    if refs.get("field") and refs.get("object"):
        return f"{refs['object']}.{refs['field']}"
    for key in ("name", "field", "object", "object2", "type"):
        if refs.get(key):
            return refs[key]
    return None


BASIS = {
    "pull-request": "the base of the pull request",
    "last-successful-run": "the commit of the last successful run of the same workflow on this branch",
    "given": "the base passed on the command line",
}


def enrich(parsed: dict, root: Path | None, base: str | None, head: str | None, catalog: dict,
           basis: dict | None = None) -> dict:
    files = index_repo(root) if root else []
    changed = changed_files(root, base, head) if root else None
    changed_set = set(changed or [])
    basis = dict(basis or {"kind": "given" if base else None})
    if changed is None:
        why = basis.get("why") or ("no base to compare with" if not base else
                                   f"git could not compare {base[:12]} with the deployed commit")
        basis = {"kind": None, "why": why}

    def touched(path: str | None):
        if path is None or changed is None:
            return None
        if path.endswith("/"):
            return any(c.startswith(path) for c in changed_set)
        stem = path[:-len("-meta.xml")] if path.endswith("-meta.xml") else path
        return path in changed_set or stem in changed_set or (stem + "-meta.xml") in changed_set

    for f in parsed["componentFailures"]:
        f["problem"] = redact(f["problem"])[:MAX_EXCERPT]
        f.update({k: v for k, v in classify(f["problem"], catalog).items()})
        f["sourceFile"] = find_source(files, f["type"], f["name"]) if root else None
        f["changedInThisRun"] = touched(f["sourceFile"])
        ref = reference_name(f["refs"])
        if ref and f["category"] == "missing-dependency":
            where = find_reference(files, ref) if root else None
            f["reference"] = {"name": ref, "inRepository": (where is not None) if root else None,
                              "sourceFile": where, "changedInThisRun": touched(where)}
    for t in parsed["testFailures"]:
        t["message"] = redact(t["message"])[:MAX_EXCERPT]
        t["stackTrace"] = redact(t["stackTrace"])[:MAX_EXCERPT]
        t.update(classify(t["message"], catalog))
        cls = t["test"].split(".")[0]
        t["sourceFile"] = find_source(files, "ApexClass", cls) if root else None
        t["changedInThisRun"] = touched(t["sourceFile"])
        frames = re.findall(r"(?:Class|Trigger)\.([A-Za-z0-9_]+)\.", t["stackTrace"])
        involved = []
        for c in dict.fromkeys(frames):
            if c == cls:
                continue
            src = find_source(files, "ApexClass", c) or find_source(files, "ApexTrigger", c) if root else None
            involved.append({"name": c, "sourceFile": src, "changedInThisRun": touched(src)})
        t["involved"] = involved
    for w in parsed["coverageWarnings"]:
        w.update(classify(w["message"], catalog))
        if w["name"] != "(org-wide)" and root:
            w["sourceFile"] = find_source(files, "ApexClass", w["name"]) or find_source(files, "ApexTrigger", w["name"])
            w["changedInThisRun"] = touched(w["sourceFile"])
    classified_other = []
    for e in parsed["otherErrors"]:
        where = {k: e[k] for k in ("jobs", "inAllFailedJobs", "notInJobs") if k in e} if isinstance(e, dict) else {}
        e = redact(e["text"] if isinstance(e, dict) else e)
        classified_other.append({"text": e, **classify(e, catalog), **where})
    parsed["otherErrors"] = classified_other
    for step in parsed.get("failedSteps", []):
        step["command"] = redact(step["command"] or "")[:300] or None
        step["error"] = redact(step["error"])[:300]
        step["errorLines"] = [redact(l)[:400] for l in step["errorLines"]]
        step["lastLines"] = [redact(l)[:400] for l in step["lastLines"]]
        known = {}
        for line in step["errorLines"] + step["lastLines"]:
            found = classify(line, catalog)
            if found["patternId"]:
                known.setdefault(found["patternId"], {"patternId": found["patternId"], "category": found["category"],
                                                      "title": found["title"], "advice": found["advice"]})
        step["knownPatterns"] = list(known.values())
    if "logTail" in parsed:
        parsed["logTail"] = [redact(l)[:300] for l in parsed["logTail"]]

    # Without a terminal the CLI prints how many tests failed but not which ones.
    per_job = [(j["name"], j["findings"]["testFailures"], j["findings"]["testFailuresWithDetails"])
               for j in parsed.get("jobs", [])]
    if not per_job:
        per_job = [(None, (parsed["deployment"].get("tests") or {}).get("failing", 0), len(parsed["testFailures"]))]
    failing = max(n for _, n, _ in per_job)
    parsed["detailsMissing"] = []
    for job, reported, with_details in per_job:
        if reported <= with_details:
            continue
        parsed["detailsMissing"].append({
            "what": "testFailures",
            **({"job": job} if job else {}),
            "reported": reported,
            "withDetails": with_details,
            "why": "The log states the number of failed tests but not their names and messages. "
                   "The Salesforce CLI leaves these details out when it runs without a terminal.",
            "fix": "Add a step after the deployment that prints the full result into the log: "
                   "`sf project deploy report --use-most-recent --json` with `if: failure()`. "
                   "Deploy Doctor reads that result on the next failure.",
        })
    by_category: dict[str, int] = {}
    for item in parsed["componentFailures"] + parsed["testFailures"] + parsed["coverageWarnings"] + parsed["otherErrors"]:
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
    parsed["totals"] = {
        "componentFailures": len(parsed["componentFailures"]),
        "testFailures": max(len(parsed["testFailures"]), failing),
        "testFailuresWithDetails": len(parsed["testFailures"]),
        "coverageWarnings": len(parsed["coverageWarnings"]),
        "otherErrors": len(parsed["otherErrors"]),
        "byCategory": by_category,
    }
    parsed["changedFiles"] = None if changed is None else {"count": len(changed), "files": changed[:200]}
    parsed["changeRange"] = {
        "known": changed is not None, "base": base if changed is not None else None, "head": head,
        "basis": basis.get("kind"),
        "comparedWith": BASIS.get(basis.get("kind")) if changed is not None else None,
        **({"run": basis["run"]} if changed is not None and basis.get("run") else {}),
        **({"environments": basis["environments"]} if changed is not None and basis.get("environments") else {}),
        **({"why": basis["why"]} if changed is None else {}),
    }
    parsed["categories"] = catalog["categories"]
    parsed["legend"] = {
        "changedInThisRun": "true: the file changed between changeRange.base and the deployed commit. false: the file exists and did not change in that range. null: unknown, because no source file was found or the range is not known (changeRange.known is false). A null says nothing about who caused the failure.",
        "changeRange": "What the deployed commit was compared with. basis pull-request: the files of the pull request. basis last-successful-run: everything since this workflow last succeeded on this branch, which is what the failed run tried to deploy on top of a working state. If environments is set, that earlier run deployed to the same environments as the failed jobs; a successful run to another org is never used as the base.",
        "run.environments": "The environments (orgs) the failed jobs deployed to, from the GitHub environment of the job or from the rule in .github/deploy-doctor.json. Empty means the run does not say where it deployed.",
        "inAllFailedJobs": "Only when several jobs failed. true: every failed job reported this finding, so it does not depend on one org. false: notInJobs lists the failed jobs that did not report it.",
        "jobs": "Only when several jobs failed: the failed jobs a finding occurred in. A pipeline with one job per org or a matrix fails per environment; the job name usually carries the environment.",
        "reference.inRepository": "For a missing dependency: whether the referenced component exists in the source at all. true means it exists but did not reach the org with this deployment; false means it is not in the repository.",
        "patternId": "Entry of the error catalog that matched. null means no known pattern; classify from the text and say so.",
    }
    return parsed


# ── GitHub run ──────────────────────────────────────────────────────────────

def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True).stdout.decode("utf-8", "replace")


def job_log(repo: str, job_id) -> str:
    """Job logs carry ANSI colours. Newer gh versions refuse to print them
    without --allow-escape-sequences, older ones do not know the flag."""
    path = f"repos/{repo}/actions/jobs/{job_id}/logs"
    try:
        return gh("api", path, "--allow-escape-sequences")
    except subprocess.CalledProcessError as err:
        if b"unknown flag" not in (err.stderr or b""):
            raise
        return gh("api", path)


CONFIG_PATH = ".github/deploy-doctor.json"
ENVIRONMENT_SOURCES = ("github-environment", "job-name", "run-title", "none")
MAX_BASE_CANDIDATES = 8


def parse_config(text: str | None) -> dict:
    """The optional configuration file. A broken file is reported and ignored, never fatal."""
    if not text or not text.strip():
        return {}
    try:
        config = json.loads(text)
    except ValueError as err:
        print(f"::warning::Deploy Doctor ignores {CONFIG_PATH}: it is not valid JSON ({err})")
        return {}
    if not isinstance(config, dict):
        print(f"::warning::Deploy Doctor ignores {CONFIG_PATH}: it has to be a JSON object")
        return {}
    return config


def environment_rule(config: dict) -> dict:
    """How to tell which org a run deployed to. Default: the GitHub environment of the job."""
    rule = (config.get("doctor") or {}).get("environment") or {}
    source = rule.get("from") or "github-environment"
    if source not in ENVIRONMENT_SOURCES:
        print(f"::warning::Deploy Doctor ignores doctor.environment in {CONFIG_PATH}: "
              f"`from` has to be one of {', '.join(ENVIRONMENT_SOURCES)}")
        return {"from": "github-environment"}
    if source in ("job-name", "run-title"):
        pattern = str(rule.get("pattern") or "")
        try:
            if not pattern or len(pattern) > 200:
                raise re.error("missing or longer than 200 characters")
            return {"from": source, "pattern": re.compile(pattern)}
        except re.error as err:
            print(f"::warning::Deploy Doctor ignores doctor.environment in {CONFIG_PATH}: pattern {err}")
            return {"from": "github-environment"}
    return {"from": source}


def matched_name(pattern: re.Pattern, text: str | None) -> str | None:
    m = pattern.search(text or "")
    if not m:
        return None
    return (m.group(1) if m.groups() and m.group(1) else m.group(0)).strip() or None


def environments_of(repo: str, run: dict, rule: dict, jobs: list[dict] | None = None) -> dict | None:
    """The environment each job of a run deployed to: {job id: name}. The key None stands for
    every job of the run. An empty result means the run names no environment; None means it
    could not be found out."""
    source = rule.get("from")
    if source == "none":
        return {}
    if source == "run-title":
        name = matched_name(rule["pattern"], run.get("display_title"))
        return {None: name} if name else {}
    if source == "job-name":
        if jobs is None:
            try:
                jobs = json.loads(gh("api", f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")).get("jobs", [])
            except (subprocess.CalledProcessError, ValueError, KeyError):
                return None
        found = {j["id"]: matched_name(rule["pattern"], j.get("name")) for j in jobs}
        return {k: v for k, v in found.items() if v}
    # A job with `environment:` creates a deployment whose status links back to the job.
    found = {}
    try:
        deployments = json.loads(gh("api", f"repos/{repo}/deployments?sha={run['head_sha']}&per_page=100"))
        for d in deployments:
            statuses = json.loads(gh("api", f"repos/{repo}/deployments/{d['id']}/statuses?per_page=1"))
            link = re.search(r"/actions/runs/(\d+)/job/(\d+)", (statuses[0].get("log_url") or "") if statuses else "")
            if link and link.group(1) == str(run.get("id")) and d.get("environment"):
                found[int(link.group(2))] = d["environment"]
    except (subprocess.CalledProcessError, ValueError, KeyError, TypeError):
        return None
    return found


def environments_in(found: dict | None, jobs: list[dict] | None = None) -> list[str]:
    """The names behind a result of environments_of, for all jobs or for the given ones."""
    if not found:
        return []
    if jobs is None:
        return sorted(set(found.values()))
    return sorted({found.get(j.get("id")) or found.get(None) for j in jobs} - {None})


def change_base(repo: str, run: dict, rule: dict | None = None, target: list[str] | None = None,
                parametrized: bool = False) -> dict:
    """What the deployed commit has to be compared with to see what this run changed.

    `target` are the environments the failed jobs deployed to. An earlier run only counts as
    the working state if it deployed to all of them: the last green run to UAT says nothing
    about what a run to production changes there."""
    head = run.get("head_sha")
    prs = run.get("pull_requests") or []
    if prs:
        return {"kind": "pull-request", "base": prs[0]["base"]["sha"], "pullRequest": prs[0]["number"]}
    if str(run.get("event", "")).startswith("pull_request"):
        # GitHub leaves `pull_requests` empty for a pull request from a fork.
        try:
            for pr in json.loads(gh("api", f"repos/{repo}/commits/{head}/pulls")):
                if pr.get("head", {}).get("sha") == head:
                    return {"kind": "pull-request", "base": pr["base"]["sha"], "pullRequest": pr["number"]}
        except (subprocess.CalledProcessError, ValueError):
            pass
        return {"kind": None, "why": "the pull request of this run could not be determined"}
    if not target and parametrized:
        return {"kind": None, "why": "the run was started by hand with inputs, and neither a GitHub environment nor "
                                     f"{CONFIG_PATH} says which org it deployed to; an earlier successful run "
                                     "may have deployed somewhere else"}
    # A push or a manual run: everything since the workflow last succeeded on this branch.
    try:
        branch = urllib.parse.quote(run.get("head_branch") or "", safe="")
        earlier = json.loads(gh("api", f"repos/{repo}/actions/workflows/{run['workflow_id']}/runs"
                                       f"?branch={branch}&status=success&per_page=30")).get("workflow_runs", [])
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return {"kind": None, "why": "the earlier runs of this workflow could not be listed"}
    looked_at = 0
    for old in earlier:
        if not (old.get("run_number", 0) < run.get("run_number", 0) and old.get("head_sha") != head):
            continue
        found = {"kind": "last-successful-run", "base": old["head_sha"],
                 "run": {"number": old.get("run_number"), "url": old.get("html_url")}}
        if not target:
            return found
        if looked_at == MAX_BASE_CANDIDATES:
            break
        looked_at += 1
        if set(target) <= set(environments_in(environments_of(repo, old, rule or {}))):
            return {**found, "environments": target}
    if target:
        return {"kind": None, "why": f"none of the last {looked_at} successful run(s) of this workflow on this branch "
                                     f"deployed to {', '.join(target)}, so there is nothing to compare with"}
    return {"kind": None, "why": "this workflow has not succeeded on this branch before, so there is nothing to compare with"}


def takes_inputs(root: Path | None, run: dict) -> bool:
    """A run started by hand from a workflow that declares inputs: one of them may choose the org."""
    if root is None or run.get("event") != "workflow_dispatch":
        return False
    try:
        text = (root / str(run.get("path") or "").split("@")[0]).read_text(errors="replace")
    except OSError:
        return False
    return bool(re.search(r"^\s*workflow_dispatch:\s*\n(?:\s*(?:#.*)?\n)*?\s+inputs:", text, re.M))


def load_run(run_id: str, root: Path | None = None) -> tuple[dict, list[tuple[str, str]], dict, str | None]:
    repo = os.environ.get("GITHUB_REPOSITORY") or json.loads(gh("repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
    run = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
    logs, log_errors = [], []
    try:
        jobs = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")).get("jobs", [])
    except subprocess.CalledProcessError as err:
        detail = (err.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        jobs = []
        log_errors.append(f"the jobs of the run could not be listed: {detail[-1] if detail else 'gh api failed'}")
    failed = [j for j in jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled")]
    for j in failed:
        try:
            logs.append((j.get("name") or str(j["id"]), job_log(repo, j["id"])))
        except subprocess.CalledProcessError as err:
            detail = (err.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            reason = detail[-1] if detail else "gh api failed"
            if re.search(r"HTTP (404|410)", reason):
                reason = "the log is no longer available (GitHub deleted it after the retention period, or it was removed)"
            log_errors.append(f"{j.get('name')}: {reason}")
    if failed and not any(text.strip() for _, text in logs) and not log_errors:
        log_errors.append("the log download returned no content")
    config = parse_config((root / CONFIG_PATH).read_text(errors="replace")
                          if root and (root / CONFIG_PATH).is_file() else None)
    rule = environment_rule(config)
    environments = environments_of(repo, run, rule, jobs)
    target = environments_in(environments, failed)
    basis = change_base(repo, run, rule, target, rule["from"] != "none" and takes_inputs(root, run))
    info = {
        "id": run.get("id"), "number": run.get("run_number"), "url": run.get("html_url"),
        "workflow": run.get("name"), "event": run.get("event"), "branch": run.get("head_branch"),
        "headSha": run.get("head_sha"), "conclusion": run.get("conclusion"),
        "pullRequest": basis.get("pullRequest"),
        "environments": target,
        "environmentsFrom": None if not target else rule["from"],
        "failedJobs": [{"name": j.get("name"),
                        **({"environment": environments_in(environments, [j])[0]}
                           if environments_in(environments, [j]) else {}),
                        "failedSteps": [s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"]}
                       for j in failed],
        "logLines": sum(len(text.splitlines()) for _, text in logs),
        "logErrors": log_errors,
    }
    return info, logs, basis, run.get("head_sha")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run-id", help="GitHub Actions run to read the failed job logs from")
    src.add_argument("--log-file", help="deployment log as a file")
    src.add_argument("--result-json", help="output of `sf project deploy ... --json` as a file")
    ap.add_argument("--repo-dir", default=".", help="checkout of the Salesforce project (default: .)")
    ap.add_argument("--no-repo", action="store_true", help="skip the repository correlation")
    ap.add_argument("--base", help="git ref the change is compared against")
    ap.add_argument("--head", help="git ref of the deployed commit")
    ap.add_argument("--catalog", help="alternative error catalog (JSON)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    run_info, base, head, basis = None, args.base, args.head, None
    root = None if args.no_repo else Path(args.repo_dir).resolve()
    if args.run_id:
        run_info, logs, found, run_head = load_run(args.run_id, root)
        if not base:
            base, basis = found.get("base"), found
        head = head or run_head
        parsed = parse_jobs(logs) if logs else parse_text("")
    elif args.result_json:
        parsed = parse_result_json(json.loads(Path(args.result_json).read_text()))
        parsed["source"] = "result-json"
    else:
        parsed = parse_text(Path(args.log_file).read_text(errors="replace"))

    catalog = load_catalog(Path(args.catalog) if args.catalog else None)
    context = enrich(parsed, root, base, head, catalog, basis)
    context = {"run": run_info, **context}
    by_job = {j["name"]: j.get("environment") for j in (run_info or {}).get("failedJobs", [])}
    for job in context.get("jobs") or []:
        if by_job.get(job.get("name")):
            job["environment"] = by_job[job["name"]]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(context, indent=2, ensure_ascii=False) + "\n")
    t = context["totals"]
    found = t["componentFailures"] + t["testFailures"] + t["coverageWarnings"] + t["otherErrors"]
    for problem in (run_info or {}).get("logErrors", []):
        print(f"::warning::Deploy Doctor could not read a job log: {problem}")
    print(f"deploy-context: {t['componentFailures']} component failure(s), {t['testFailures']} test failure(s), "
          f"{t['coverageWarnings']} coverage warning(s), {t['otherErrors']} other error line(s) -> {out}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"findings={found}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
