"""Tests for doctor/deploy_context.py. Run with: python3 -m unittest discover -s tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "doctor"))
import deploy_context as dc  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
CATALOG = dc.load_catalog()


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def write(repo, rel, text="x"):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class ParseHumanLog(unittest.TestCase):
    def test_component_failures_with_wrapped_cells(self):
        parsed = dc.parse_log((FIXTURES / "compile-human.log").read_text())
        self.assertEqual(parsed["deployment"]["status"], "Failed")
        self.assertTrue(parsed["deployment"]["dryRun"])
        self.assertEqual((parsed["deployment"]["componentsDeployed"], parsed["deployment"]["componentsTotal"]), (2, 6))
        names = [(f["type"], f["name"]) for f in parsed["componentFailures"]]
        self.assertEqual(names, [
            ("CustomField", "Account.Doctor_Lookup__c"),
            ("ApexClass", "DoctorBrokenService"),
            ("ApexClass", "DoctorBrokenService"),
            ("PermissionSet", "Doctor_Access"),
        ])
        soql = parsed["componentFailures"][1]
        self.assertIn("No such column 'Doctor_Missing_Field__c' on entity 'Account'", soql["problem"])
        self.assertEqual((soql["line"], soql["column"]), (3, 21))

    def test_plain_table_of_a_ci_job_log(self):
        """Without a terminal the CLI prints a position-aligned table, not a box."""
        parsed = dc.parse_log((FIXTURES / "compile-ci-job.log").read_text())
        found = [(f["type"], f["name"], f["line"], f["column"]) for f in parsed["componentFailures"]]
        self.assertEqual(found, [
            ("CustomField", "Account.Doctor_Lookup__c", 3, 13),
            ("ApexClass", "DoctorBrokenService", 3, 21),
            ("ApexClass", "DoctorBrokenService", 4, 18),
            ("PermissionSet", "Doctor_Access", None, None),
        ])
        self.assertIn("No such column 'Doctor_Missing_Field__c' on entity 'Account'", parsed["componentFailures"][1]["problem"])
        self.assertTrue(parsed["componentFailures"][0]["problem"].endswith("valid sObject type"))
        enriched = dc.enrich(parsed, None, None, None, CATALOG)
        self.assertEqual(enriched["totals"]["byCategory"], {"missing-dependency": 4})

    def test_test_failures_and_coverage(self):
        parsed = dc.parse_log((FIXTURES / "tests-human.log").read_text())
        self.assertEqual([t["test"] for t in parsed["testFailures"]],
                         ["DoctorOkServiceTest.insertsAccount", "DoctorOkServiceTest.twiceDoubles"])
        self.assertIn("REQUIRED_FIELD_MISSING", parsed["testFailures"][0]["message"])
        self.assertIn("line 4", parsed["testFailures"][0]["stackTrace"])
        self.assertEqual(parsed["coverageWarnings"][0]["name"], "DoctorOkService")
        self.assertIn("is 50%, at least 75%", parsed["coverageWarnings"][0]["message"])

    def test_github_log_prefixes_are_stripped(self):
        raw = (FIXTURES / "compile-human.log").read_text().splitlines()
        as_job_log = "\n".join(f"2026-11-20T12:52:21.1234567Z {line}" for line in raw)
        as_gh_view = "\n".join(f"deploy\tDeploy Metadata\t2026-11-20T12:52:21.1234567Z {line}" for line in raw)
        for text in (as_job_log, as_gh_view):
            self.assertEqual(len(dc.parse_log(text)["componentFailures"]), 4)

    def test_unstructured_failure_keeps_error_lines(self):
        parsed = dc.parse_log("Installing CLI\nError (1): expired access/refresh token\n##[error]Process completed with exit code 1.")
        self.assertEqual(len(parsed["otherErrors"]), 2)
        enriched = dc.enrich(parsed, None, None, None, CATALOG)
        self.assertEqual(enriched["otherErrors"][0]["patternId"], "auth")
        self.assertEqual(enriched["otherErrors"][0]["category"], "environment")

    def test_named_cli_errors_and_log_tail(self):
        log = "\n".join([
            "Log in to Salesforce org using JSON web token (JWT)...",
            "Error (JwtGrantError): We encountered a JSON web token error: MissingClientIdError::Client ID is required",
            "##[error]Process completed with exit code 1.",
            "Post job cleanup.",
        ])
        enriched = dc.enrich(dc.parse_log(log), None, None, None, CATALOG)
        self.assertEqual(enriched["otherErrors"][0]["patternId"], "auth")
        self.assertEqual(len(enriched["logTail"]), 2)
        self.assertNotIn("Post job cleanup.", enriched["logTail"])


class ParseResultJson(unittest.TestCase):
    def test_same_findings_as_the_human_log(self):
        for name in ("compile", "tests"):
            from_json = dc.parse_result_json(json.loads((FIXTURES / f"{name}.json").read_text()))
            from_log = dc.parse_log((FIXTURES / f"{name}-human.log").read_text())
            self.assertEqual(len(from_json["componentFailures"]), len(from_log["componentFailures"]), name)
            self.assertEqual(sorted(t["test"] for t in from_json["testFailures"]),
                             sorted(t["test"] for t in from_log["testFailures"]), name)
            self.assertEqual(len(from_json["coverageWarnings"]), len(from_log["coverageWarnings"]), name)

    def test_result_embedded_in_a_log_is_found(self):
        text = "some output\n" + (FIXTURES / "compile.json").read_text() + "\nmore output\n"
        self.assertEqual(dc.find_result_json(text)["result"]["status"], "Failed")
        self.assertIsNone(dc.find_result_json("no json here\n{\n \"a\": 1\n}\n"))


class Classification(unittest.TestCase):
    def test_every_pattern_compiles_and_has_a_known_category(self):
        import re
        for p in CATALOG["patterns"]:
            re.compile(p["pattern"])
            self.assertIn(p["category"], CATALOG["categories"], p["id"])
            self.assertTrue(p["title"] and p["advice"], p["id"])
        ids = [p["id"] for p in CATALOG["patterns"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_known_messages(self):
        cases = {
            "No such column 'Foo__c' on entity 'Account'.": "missing-field-soql",
            "In field: apexClass - no ApexClass named Foo found": "missing-named-component",
            "Method does not exist or incorrect signature: void run() from the type Foo": "apex-signature",
            "System.AssertException: Assertion Failed: Expected: 1, Actual: 2": "test-assertion",
            "System.DmlException: Update failed. UNABLE_TO_LOCK_ROW, unable to obtain exclusive access": "test-row-lock",
            "System.LimitException: Too many SOQL queries: 101": "test-governor-limit",
            "Average test coverage across all Apex Classes and Triggers is 71%, at least 75% test coverage is required.": "coverage-org",
            "Unknown user permission: ViewRoles": "unknown-user-permission",
            "insufficient access rights on cross-reference id: 0Q0000000000001": "insufficient-access",
            "The version of the flow you're updating is active and can't be overwritten.": "active-flow",
            "socket hang up": "network-or-platform",
        }
        for text, expected in cases.items():
            self.assertEqual(dc.classify(text, CATALOG)["patternId"], expected, text)
        self.assertEqual(dc.classify("something nobody has seen before", CATALOG)["category"], "unknown")

    def test_usernames_and_ids_are_redacted(self):
        text = dc.redact("deploy as jane.doe@example.com failed on 005XX0000012345AAA")
        self.assertNotIn("jane.doe", text)
        self.assertNotIn("005XX0000012345AAA", text)


class RepositoryCorrelation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        base = "force-app/main/default"
        write(self.repo, f"{base}/classes/DoctorBrokenService.cls")
        write(self.repo, f"{base}/classes/DoctorBrokenService.cls-meta.xml")
        write(self.repo, f"{base}/objects/Account/fields/Doctor_Lookup__c.field-meta.xml")
        write(self.repo, f"{base}/objects/Account/fields/Doctor_Other_Missing__c.field-meta.xml")
        write(self.repo, f"{base}/objects/Contact/fields/Doctor_Missing_Field__c.field-meta.xml")
        write(self.repo, f"{base}/permissionsets/Doctor_Access.permissionset-meta.xml")
        write(self.repo, f"{base}/lwc/doctorCard/doctorCard.js")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        write(self.repo, f"{base}/classes/DoctorBrokenService.cls", "changed")
        git(self.repo, "commit", "-q", "-am", "change the class")

    def tearDown(self):
        self.tmp.cleanup()

    def context(self):
        parsed = dc.parse_log((FIXTURES / "compile-human.log").read_text())
        return dc.enrich(parsed, self.repo, None, "HEAD", CATALOG)

    def test_source_files_and_change_flags(self):
        failures = {(f["name"], f["patternId"]): f for f in self.context()["componentFailures"]}
        cls = failures[("DoctorBrokenService", "missing-field-soql")]
        self.assertTrue(cls["sourceFile"].endswith("classes/DoctorBrokenService.cls"))
        self.assertTrue(cls["changedInThisRun"])
        field = failures[("Account.Doctor_Lookup__c", "missing-sobject")]
        self.assertTrue(field["sourceFile"].endswith("Account/fields/Doctor_Lookup__c.field-meta.xml"))
        self.assertFalse(field["changedInThisRun"])

    def test_missing_reference_is_looked_up_in_the_source(self):
        failures = {(f["name"], f["patternId"]): f for f in self.context()["componentFailures"]}
        in_repo = failures[("Doctor_Access", "missing-named-component")]["reference"]
        self.assertEqual(in_repo["name"], "Account.Doctor_Other_Missing__c")
        self.assertTrue(in_repo["inRepository"])
        self.assertFalse(in_repo["changedInThisRun"])
        # The field exists on Contact only, so it does not count for Account.
        not_in_repo = failures[("DoctorBrokenService", "missing-field-soql")]["reference"]
        self.assertEqual(not_in_repo["name"], "Account.Doctor_Missing_Field__c")
        self.assertFalse(not_in_repo["inRepository"])

    def test_bundle_and_totals(self):
        files = dc.index_repo(self.repo)
        self.assertTrue(dc.find_source(files, "LightningComponentBundle", "doctorCard").endswith("lwc/doctorCard/"))
        ctx = self.context()
        self.assertEqual(ctx["totals"]["componentFailures"], 4)
        self.assertEqual(ctx["totals"]["byCategory"], {"missing-dependency": 4})
        self.assertEqual(ctx["changedFiles"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
