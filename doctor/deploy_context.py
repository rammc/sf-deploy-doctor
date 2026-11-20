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
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
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
BUNDLE_DIRS = {"LightningComponentBundle": "lwc", "AuraDefinitionBundle": "aura"}


# ── input: log text ─────────────────────────────────────────────────────────

def clean_lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = ANSI.sub("", raw)
        if line.count("\t") >= 2:  # gh run view --log: job \t step \t message
            line = line.split("\t", 2)[2]
        out.append(TIMESTAMP.sub("", line).rstrip())
    return out


def redact(text: str) -> str:
    """Usernames and record ids from the org do not belong in an issue."""
    text = EMAIL.sub("<user>", text or "")
    return re.sub(r"\b(?:00[5DQ]|001|003|006|0Af|01p|01q|07M|a[0-9A-Za-z]{2})[0-9A-Za-z]{12}(?:[0-9A-Za-z]{3})?\b", "<id>", text)


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
    lines = clean_lines(text)
    out = {"deployment": {}, "componentFailures": [], "testFailures": [], "coverageWarnings": [], "otherErrors": []}
    dep = out["deployment"]
    joined = "\n".join(lines)

    m = re.search(r"^\s*Status:\s*(\w+)", joined, re.M)
    if m:
        dep["status"] = m.group(1)
    m = re.search(r"Deploy ID:\s*(0Af\w+)", joined)
    if m:
        dep["deployId"] = m.group(1)
    dep["dryRun"] = bool(re.search(r"\(dry-run\)|Dry-run complete|deploy validate|Validating Deployment", joined))
    m = re.search(r"Components:\s*(\d+)/(\d+)", joined)
    if m:
        dep["componentsDeployed"], dep["componentsTotal"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Tests:\s*(\d+)/(\d+)", joined)
    if m:
        dep["testsCompleted"], dep["testsTotal"] = int(m.group(1)), int(m.group(2))

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

    # Coverage warnings are printed as oclif warnings and wrap across lines.
    warn = re.sub(r"\n\s*›\s+", " ", joined)
    for m in re.finditer(r"Warning:\s*(?:([\w.]+) - )?((?:Test coverage of selected|Average test coverage across)[^\n]+?required)", warn):
        out["coverageWarnings"].append({"name": m.group(1) or "(org-wide)", "message": re.sub(r"\s+", " ", m.group(2))})

    if not out["componentFailures"] and not out["testFailures"] and not out["coverageWarnings"]:
        # Nothing structured: keep the lines that look like the actual error.
        seen = set()
        for line in lines:
            s = line.strip()
            if re.search(r"(##\[error\]|::error::|^Error(?: \([^)]+\))?:|^ERROR|ERROR running|Deploy failed|\bError: )", s) and s not in seen:
                seen.add(s)
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
    if data.get("message") and not (out["componentFailures"] or out["testFailures"]):
        out["otherErrors"].append(str(data.get("message"))[:MAX_EXCERPT])
    return out


def find_result_json(text: str) -> dict | None:
    """A log may contain the `--json` result of the deploy command."""
    for m in re.finditer(r"^\{\s*$", text, re.M):
        depth, end = 0, None
        for j in range(m.start(), len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end:
            try:
                data = json.loads(text[m.start():end])
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("result"), dict) and "details" in data["result"]:
                return data
    return None


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
    if not head:
        return None
    for spec in ([f"{base}...{head}"] if base else []) + [f"{head}~1..{head}"]:
        try:
            out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", spec],
                                 check=True, capture_output=True, text=True).stdout
            return [l for l in out.splitlines() if l]
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
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


def enrich(parsed: dict, root: Path | None, base: str | None, head: str | None, catalog: dict) -> dict:
    files = index_repo(root) if root else []
    changed = changed_files(root, base, head) if root else None
    changed_set = set(changed or [])

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
        e = redact(e)
        classified_other.append({"text": e, **classify(e, catalog)})
    parsed["otherErrors"] = classified_other
    if "logTail" in parsed:
        parsed["logTail"] = [redact(l)[:300] for l in parsed["logTail"]]

    by_category: dict[str, int] = {}
    for item in parsed["componentFailures"] + parsed["testFailures"] + parsed["coverageWarnings"] + parsed["otherErrors"]:
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
    parsed["totals"] = {
        "componentFailures": len(parsed["componentFailures"]),
        "testFailures": len(parsed["testFailures"]),
        "coverageWarnings": len(parsed["coverageWarnings"]),
        "otherErrors": len(parsed["otherErrors"]),
        "byCategory": by_category,
    }
    parsed["changedFiles"] = None if changed is None else {"count": len(changed), "files": changed[:200]}
    parsed["categories"] = catalog["categories"]
    parsed["legend"] = {
        "changedInThisRun": "true: the file is part of the commits of this run. false: the file exists but was not touched, so the failure likely comes from the org or from another change. null: unknown (no source file found or no git range).",
        "reference.inRepository": "For a missing dependency: whether the referenced component exists in the source at all. true means it exists but did not reach the org with this deployment; false means it is not in the repository.",
        "patternId": "Entry of the error catalog that matched. null means no known pattern; classify from the text and say so.",
    }
    return parsed


# ── GitHub run ──────────────────────────────────────────────────────────────

def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True).stdout.decode("utf-8", "replace")


def load_run(run_id: str) -> tuple[dict, str, str | None, str | None]:
    repo = os.environ.get("GITHUB_REPOSITORY") or json.loads(gh("repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
    run = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
    jobs = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")).get("jobs", [])
    failed = [j for j in jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled")]
    text, log_errors = "", []
    for j in failed:
        try:
            text += gh("api", f"repos/{repo}/actions/jobs/{j['id']}/logs") + "\n"
        except subprocess.CalledProcessError as err:
            detail = (err.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            log_errors.append(f"{j.get('name')}: {detail[-1] if detail else 'gh api failed'}")
    if failed and not text.strip() and not log_errors:
        log_errors.append("the log download returned no content")
    prs = run.get("pull_requests") or []
    base = prs[0]["base"]["sha"] if prs else None
    info = {
        "id": run.get("id"), "number": run.get("run_number"), "url": run.get("html_url"),
        "workflow": run.get("name"), "event": run.get("event"), "branch": run.get("head_branch"),
        "headSha": run.get("head_sha"), "conclusion": run.get("conclusion"),
        "pullRequest": prs[0]["number"] if prs else None,
        "failedJobs": [{"name": j.get("name"),
                        "failedSteps": [s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"]}
                       for j in failed],
        "logLines": len(text.splitlines()),
        "logErrors": log_errors,
    }
    return info, text, base, run.get("head_sha")


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

    run_info, base, head = None, args.base, args.head
    if args.run_id:
        run_info, text, run_base, run_head = load_run(args.run_id)
        base, head = base or run_base, head or run_head
        embedded = find_result_json("\n".join(clean_lines(text)))
        parsed = parse_result_json(embedded) if embedded else parse_log(text)
        parsed["source"] = "log-json" if embedded else "log"
    elif args.result_json:
        parsed = parse_result_json(json.loads(Path(args.result_json).read_text()))
        parsed["source"] = "result-json"
    else:
        text = Path(args.log_file).read_text(errors="replace")
        embedded = find_result_json("\n".join(clean_lines(text)))
        parsed = parse_result_json(embedded) if embedded else parse_log(text)
        parsed["source"] = "log-json" if embedded else "log"

    root = None if args.no_repo else Path(args.repo_dir).resolve()
    catalog = load_catalog(Path(args.catalog) if args.catalog else None)
    context = enrich(parsed, root, base, head, catalog)
    context = {"run": run_info, **context}

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
