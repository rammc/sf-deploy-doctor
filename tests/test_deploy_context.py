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
        # the log repeats the progress line (0/6, 3/6, 2/6); the last one counts
        self.assertEqual((parsed["deployment"]["componentsDeployed"], parsed["deployment"]["componentsTotal"]), (2, 6))

    def test_ci_log_with_failed_tests_but_without_their_details(self):
        """In CI the CLI prints 'Failing: 2' and nothing about which tests failed."""
        parsed = dc.parse_log((FIXTURES / "tests-ci-job.log").read_text())
        self.assertEqual(parsed["deployment"]["tests"], {"passing": 0, "failing": 2, "total": 2})
        self.assertEqual(parsed["testFailures"], [])
        ctx = dc.enrich(parsed, None, None, None, CATALOG)
        self.assertEqual(ctx["totals"]["testFailures"], 2)
        self.assertEqual(ctx["totals"]["testFailuresWithDetails"], 0)
        self.assertEqual(ctx["detailsMissing"][0]["what"], "testFailures")
        self.assertIn("deploy report", ctx["detailsMissing"][0]["fix"])
        self.assertEqual(len(ctx["coverageWarnings"]), 1)

    def test_no_details_missing_when_the_result_is_complete(self):
        for parsed in (dc.parse_log((FIXTURES / "tests-human.log").read_text()),
                       dc.parse_result_json(json.loads((FIXTURES / "tests.json").read_text()))):
            ctx = dc.enrich(parsed, None, None, None, CATALOG)
            self.assertEqual(ctx["detailsMissing"], [])
            self.assertEqual(ctx["totals"]["testFailures"], 2)

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
        parsed = dc.parse_text(text)
        self.assertEqual((parsed["source"], parsed["deployment"]["status"]), ("log-json", "Failed"))
        self.assertTrue(parsed["componentFailures"])
        self.assertEqual(dc.parse_text("no json here\n{\n \"a\": 1\n}\n")["source"], "log")


class RealPipelines(unittest.TestCase):
    """A job that deploys several times and runs the Apex tests as a step of its own.

    The fixture has the shape of a real pipeline log; names and ids are made up.
    """

    def setUp(self):
        self.parsed = dc.parse_text((FIXTURES / "multi-step-ci-job.log").read_text())
        self.ctx = dc.enrich(self.parsed, None, None, None, CATALOG)

    def test_the_failed_deployments_count_not_the_first_one(self):
        dep = self.ctx["deployment"]
        self.assertEqual(dep["status"], "Failed")
        self.assertEqual(dep["resultsInLog"], {"total": 3, "failed": 2})

    def test_component_error_listed_only_under_files(self):
        failures = self.ctx["componentFailures"]
        self.assertEqual([(f["type"], f["name"]) for f in failures], [("EmailTemplate", "Doctor_Templates/Doctor_Reminder")])
        self.assertEqual((failures[0]["patternId"], failures[0]["refs"]), ("missing-folder", {"name": "Doctor_Templates"}))

    def test_deployment_that_fails_without_naming_a_component(self):
        other = self.ctx["otherErrors"]
        self.assertEqual(len(other), 1)
        self.assertEqual((other[0]["patternId"], other[0]["category"]), ("deploy-row-lock", "org-state"))

    def test_apex_tests_that_run_as_their_own_step(self):
        tests = self.ctx["testFailures"]
        self.assertEqual(sorted(t["test"] for t in tests),
                         ["DoctorPriceServiceTest.appliesDiscount", "DoctorPriceServiceTest.rejectsNegativePrice",
                          "DoctorQuoteServiceTest.createsQuoteLine"])
        self.assertEqual({t["patternId"] for t in tests}, {"test-row-lock"})
        self.assertEqual(tests[0]["involved"][0]["name"], "DoctorTestData")
        self.assertEqual(self.ctx["testRuns"], {"runs": 1, "passing": 3, "failing": 3, "total": 6})
        self.assertEqual(self.ctx["totals"]["testFailures"], 3)
        self.assertEqual(self.ctx["detailsMissing"], [])

    def test_every_failed_step_is_kept_as_an_excerpt(self):
        steps = self.ctx["failedSteps"]
        self.assertEqual([s["command"] for s in steps],
                         ["sf project deploy start --source-dir templates --json",
                          "sf project deploy start --source-dir force-app --json",
                          "sf apex run test --result-format json --code-coverage --wait 60",
                          "./ci/report.sh"])
        report = steps[3]
        self.assertEqual(report["error"], "Process completed with exit code 4.")
        # the echoed script and the environment are not output, warnings are counted
        self.assertEqual(report["lastLines"][0], "Collecting coverage for <user>")
        self.assertEqual(report["warnings"], 1)
        self.assertFalse(any("Warning" in l or "shell:" in l for l in report["lastLines"] + report["errorLines"]))
        self.assertEqual([k["patternId"] for k in report["knownPatterns"]], ["apex-test-wait-exceeded"])
        self.assertFalse(any("_raw" in s for s in steps))

    def test_echoed_script_is_not_output(self):
        log = ("2026-11-20T10:00:00.0000000Z ##[group]Run ./check.sh\n"
               "2026-11-20T10:00:00.0000000Z \x1b[36;1mcommand -v jq || echo '::error::jq utility required but absent'\x1b[0m\n"
               "2026-11-20T10:00:00.0000000Z ##[endgroup]\n"
               "2026-11-20T10:00:01.0000000Z scanner: quality gate failed\n"
               "2026-11-20T10:00:01.0000000Z ##[error]Process completed with exit code 2.\n")
        parsed = dc.parse_text(log)
        self.assertFalse(any("jq utility" in e for e in parsed["otherErrors"]))
        self.assertEqual(parsed["failedSteps"][0]["lastLines"], ["scanner: quality gate failed"])

    def test_human_readable_result_of_a_failed_step_is_read_too(self):
        human = (FIXTURES / "compile-ci-job.log").read_text()
        ok = (FIXTURES / "multi-step-ci-job.log").read_text().split("##[group]Run sf project deploy start --source-dir templates")[0]
        parsed = dc.parse_text(ok.rsplit("\n", 1)[0] + "\n" + human)
        self.assertEqual(parsed["source"], "log-json")
        self.assertTrue(parsed["componentFailures"], "the table of the failed step is lost next to JSON of another step")


class OtherPipelines(unittest.TestCase):
    """Formats found in the failed runs of public repositories, not in the first benchmark."""

    def test_apex_tests_in_the_human_readable_table(self):
        ctx = dc.enrich(dc.parse_text((FIXTURES / "apex-tests-human-ci-job.log").read_text()), None, None, None, CATALOG)
        tests = {t["test"]: t for t in ctx["testFailures"]}
        self.assertEqual(sorted(tests), ["AccountSummaryControllerTest.shouldBuildSummaryForAccount",
                                         "GovernorBudgetTest.pastTheHardThresholdWarnsHarder"])
        first = tests["AccountSummaryControllerTest.shouldBuildSummaryForAccount"]
        self.assertEqual(first["message"], "System.AssertException: Assertion Failed: Account should be resolved")
        self.assertIn("line 174", first["stackTrace"])
        self.assertEqual(first["patternId"], "test-assertion")
        second = tests["GovernorBudgetTest.pastTheHardThresholdWarnsHarder"]
        self.assertEqual(second["patternId"], "test-governor-limit")
        self.assertEqual([i["name"] for i in second["involved"]], ["GovernorBudget"])
        self.assertEqual(ctx["deployment"]["tests"], {"passing": 239, "failing": 2, "total": 241})
        self.assertEqual(ctx["detailsMissing"], [])
        self.assertNotIn("example.com", json.dumps(ctx["failedSteps"]))

    def test_cli_errors_over_several_lines_and_as_json(self):
        ctx = dc.enrich(dc.parse_text((FIXTURES / "cli-errors-ci-job.log").read_text()), None, None, None, CATALOG)
        found = {e["patternId"]: e["text"] for e in ctx["otherErrors"]}
        self.assertIn("Exactly one of the following must be provided", found["cli-usage"])
        self.assertTrue(found["package-install-failed"].startswith("PackageInstallError: Encountered errors"))
        self.assertIn("org-alias-not-authenticated", found)
        self.assertNotIn("auth", found)
        self.assertFalse(any("##[group]" in e["text"] for e in ctx["otherErrors"]))
        self.assertEqual(len(ctx["failedSteps"]), 3)

    def test_status_is_only_read_from_a_deployment(self):
        parsed = dc.parse_log("Status: Downloaded newer image for salesforce/cli:latest-full\n")
        self.assertNotIn("status", parsed["deployment"])


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
            # messages from failed runs of public repositories
            "Variable does not exist: request": "apex-unknown-variable",
            "Variable does not exist: Score__c": "missing-field-variable",
            "Type is not visible: ConnectApi.EinsteinLLM": "apex-type-not-visible",
            "Could not resolve list view column: AssignedUser": "listview-unknown-column",
            "TypeError: webidl.util.markAsUncloneable is not a function": "cli-node-incompatible",
            "Error (2): Nonexistent flag: --debug": "cli-usage",
            "Error (ConversionError): Component conversion failed: \"ENV_ID\" is in sfdx-project.json as a value for \"replaceWithEnv\" property, but it's not set in your environment.": "env-replacement-missing",
            "Error (1): TotalRequests Limit exceeded.": "api-limit",
            "Error (UnexpectedForceIgnore): Metadata xml file /w/lwc/card/card.js-meta.xml is forceignored but is required for /w/lwc/card.": "forceignore-required-file",
            "Error (LibraryError): error parsing /w/My App/objects/Case/fields/Status__c.field-meta.xml due to:": "metadata-xml-invalid",
            "Error (INVALID_INPUT): This class name's value is invalid: CoverageTest. Provide the name of an Apex class that has test methods.": "test-class-not-found",
            "PackageInstallError: Encountered errors installing the package! Installation errors:": "package-install-failed",
            "Error (NamedOrgNotFoundError): No authorization information found for scratch-org.": "org-alias-not-authenticated",
            "Error (RefreshTokenAuthError): Error authenticating with the refresh token due to: expired access/refresh token": "auth",
            # messages from a real pipeline with email templates and a separate test step
            "Cannot find folder:ServiceEmailTemplates": "missing-folder",
            "The emailTemplate OrderEmails/Order_Cancelled can't be found. Use the following format": "missing-email-template",
            "To create email template folders, enable Folders and Enhanced Sharing.": "email-template-folders-disabled",
            "UNABLE_TO_LOCK_ROW: unable to obtain exclusive access to this record": "deploy-row-lock",
            "System.DmlException: Update failed. First exception on row 0; first error: FIELD_INTEGRITY_EXCEPTION, Can't change quote owner. Enter an active user: []": "test-field-integrity",
            "Run with --synchronous or increase --wait timeout to wait for results.": "apex-test-wait-exceeded",
            # Lightning web components: messages as an org returned them on a check-only deployment
            "Unable to find Apex action class referenced as 'NoSuchClassXyz'.": "lwc-missing-apex-class",
            "Unable to find Apex action method referenced as 'DoctorProbe.hidden'.": "lwc-missing-apex-method",
            "No MODULE named markup://c:noSuchChildXyz found : [markup://c:probeChild]": "lwc-missing-module",
            "Invalid reference Account.NoSuchFieldXyz__c of type sobjectField in file probeSchema.js": "lwc-invalid-schema-reference",
            "Invalid reference c.NoSuchLabelXyz of type label in file probeLabel.js": "lwc-invalid-reference",
            "Invalid reference noSuchResXyz of type resourceUrl in file probeResource.js": "lwc-invalid-reference",
            "org.auraframework.throwable.AuraRuntimeException: Unable to retrieve metadata for descriptor: "
            "markup://c:__LightningMessageChannel__NoSuchChannelXyz__c": "lwc-missing-message-channel",
            "[Line: 2, Col: 12] LWC1503: Parsing error: Unexpected token (2:12)": "lwc-compile",
        }
        for text, expected in cases.items():
            self.assertEqual(dc.classify(text, CATALOG)["patternId"], expected, text)
        self.assertEqual(dc.classify("something nobody has seen before", CATALOG)["category"], "unknown")
        method = dc.classify("Unable to find Apex action method referenced as 'DoctorProbe.hidden'.", CATALOG)
        self.assertEqual(method["refs"], {"name": "DoctorProbe", "method": "hidden"})
        channel = dc.classify("descriptor: markup://c:__LightningMessageChannel__Refresh__c", CATALOG)
        self.assertEqual(channel["refs"], {"name": "Refresh"})

    def test_aura_descriptors_do_not_look_like_urls(self):
        text = dc.redact("No MODULE named markup://c:noSuchChildXyz found : [markup://c:probeChild]")
        self.assertEqual(text, "No MODULE named c/noSuchChildXyz found : [c/probeChild]")
        found = dc.classify(text, CATALOG)
        self.assertEqual((found["patternId"], found["refs"]), ("lwc-missing-module", {"name": "noSuchChildXyz"}))

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
        return dc.enrich(parsed, self.repo, "HEAD~1", "HEAD", CATALOG)

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
        self.assertEqual((ctx["changeRange"]["known"], ctx["changeRange"]["basis"]), (True, "given"))

    def test_without_a_base_nothing_is_called_unchanged(self):
        """The last commit alone says nothing about a release branch: no base, no verdict."""
        parsed = dc.parse_log((FIXTURES / "compile-human.log").read_text())
        ctx = dc.enrich(parsed, self.repo, None, "HEAD", CATALOG, {"kind": None, "why": "no earlier run"})
        self.assertEqual({f["changedInThisRun"] for f in ctx["componentFailures"]}, {None})
        self.assertIsNone(ctx["changedFiles"])
        self.assertEqual(ctx["changeRange"], {"known": False, "base": None, "head": "HEAD", "basis": None,
                                              "comparedWith": None, "why": "no earlier run"})

    def test_a_base_git_does_not_know_is_unknown_too(self):
        parsed = dc.parse_log((FIXTURES / "compile-human.log").read_text())
        ctx = dc.enrich(parsed, self.repo, "0" * 40, "HEAD", CATALOG, {"kind": "last-successful-run", "base": "0" * 40})
        self.assertFalse(ctx["changeRange"]["known"])
        self.assertIn("could not compare", ctx["changeRange"]["why"])

    def test_a_release_branch_is_compared_with_the_last_green_run(self):
        base = "force-app/main/default"
        green = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD~1"], check=True,
                               capture_output=True, text=True).stdout.strip()
        for n in range(3):  # more commits after the one that touched the class
            write(self.repo, f"{base}/classes/Other{n}.cls")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", f"commit {n}")
        parsed = dc.parse_log((FIXTURES / "compile-human.log").read_text())
        ctx = dc.enrich(parsed, self.repo, green, "HEAD", CATALOG, {"kind": "last-successful-run", "base": green,
                                                                    "run": {"number": 7, "url": "u"}})
        cls = next(f for f in ctx["componentFailures"] if f["name"] == "DoctorBrokenService")
        self.assertTrue(cls["changedInThisRun"])
        self.assertEqual(ctx["changedFiles"]["count"], 4)
        self.assertEqual(ctx["changeRange"]["run"], {"number": 7, "url": "u"})


class ChangeBase(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answers = {}
        self._gh = dc.gh

        def fake(*args):
            self.calls.append(args[-1])
            for key, value in self.answers.items():
                if key in args[-1]:
                    if isinstance(value, Exception):
                        raise value
                    return json.dumps(value)
            raise subprocess.CalledProcessError(1, "gh")
        dc.gh = fake

    def tearDown(self):
        dc.gh = self._gh

    def test_pull_request(self):
        run = {"head_sha": "h", "event": "pull_request", "pull_requests": [{"number": 4, "base": {"sha": "b"}}]}
        self.assertEqual(dc.change_base("o/r", run), {"kind": "pull-request", "base": "b", "pullRequest": 4})
        self.assertEqual(self.calls, [])

    def test_pull_request_from_a_fork(self):
        self.answers["commits/h/pulls"] = [{"number": 9, "head": {"sha": "h"}, "base": {"sha": "b"}}]
        run = {"head_sha": "h", "event": "pull_request", "pull_requests": []}
        self.assertEqual(dc.change_base("o/r", run), {"kind": "pull-request", "base": "b", "pullRequest": 9})

    def test_push_uses_the_last_successful_run_of_the_branch(self):
        self.answers["workflows/11/runs"] = {"workflow_runs": [
            {"run_number": 31, "head_sha": "later"},   # a re-run that finished after this one
            {"run_number": 28, "head_sha": "h"},       # the same commit says nothing
            {"run_number": 25, "head_sha": "green", "html_url": "u"},
        ]}
        run = {"head_sha": "h", "event": "push", "head_branch": "release/2026.11", "workflow_id": 11,
               "run_number": 30, "pull_requests": []}
        self.assertEqual(dc.change_base("o/r", run), {"kind": "last-successful-run", "base": "green",
                                                      "run": {"number": 25, "url": "u"}})
        self.assertIn("branch=release%2F2026.11", self.calls[0])

    def test_first_run_on_a_branch_has_no_base(self):
        self.answers["workflows/11/runs"] = {"workflow_runs": []}
        run = {"head_sha": "h", "event": "workflow_dispatch", "head_branch": "uat", "workflow_id": 11, "run_number": 1}
        found = dc.change_base("o/r", run)
        self.assertIsNone(found["kind"])
        self.assertNotIn("base", found)


class SeveralJobs(unittest.TestCase):
    """One job per org: every finding says where it occurred."""

    def logs(self):
        compile_log = (FIXTURES / "compile-ci-job.log").read_text()
        tests_log = (FIXTURES / "tests-ci-job.log").read_text()
        return [("deploy (uat)", compile_log), ("deploy (prod)", compile_log), ("deploy (int)", tests_log)]

    def test_findings_carry_their_jobs(self):
        parsed = dc.parse_jobs(self.logs())
        single = dc.parse_text((FIXTURES / "compile-ci-job.log").read_text())
        self.assertEqual(len(parsed["componentFailures"]), len(single["componentFailures"]))
        self.assertEqual({tuple(f["jobs"]) for f in parsed["componentFailures"]}, {("deploy (uat)", "deploy (prod)")})
        self.assertEqual([j["name"] for j in parsed["jobs"]], ["deploy (uat)", "deploy (prod)", "deploy (int)"])
        self.assertEqual({(f["inAllFailedJobs"], tuple(f["notInJobs"])) for f in parsed["componentFailures"]},
                         {(False, ("deploy (int)",))})
        self.assertTrue(all(s["job"] for s in parsed["failedSteps"]))
        self.assertEqual({s["job"] for s in parsed["failedSteps"]}, {"deploy (uat)", "deploy (prod)", "deploy (int)"})

    def test_a_finding_in_every_job_says_so(self):
        log = (FIXTURES / "compile-ci-job.log").read_text()
        ctx = dc.enrich(dc.parse_jobs([("deploy (uat)", log), ("deploy (prod)", log)]), None, None, None, CATALOG)
        self.assertEqual({(f["inAllFailedJobs"], tuple(f["notInJobs"])) for f in ctx["componentFailures"]}, {(True, ())})

    def test_context_of_several_jobs(self):
        ctx = dc.enrich(dc.parse_jobs(self.logs()), None, None, None, CATALOG)
        self.assertEqual(ctx["source"], "log-per-job")
        self.assertTrue(all("jobs" in f and f["patternId"] for f in ctx["componentFailures"]))
        missing = [d for d in ctx["detailsMissing"]]
        self.assertTrue(all(d["job"] == "deploy (int)" for d in missing))
        self.assertEqual(ctx["deployment"]["job"], "deploy (uat)")

    def test_one_job_reads_like_before(self):
        text = (FIXTURES / "multi-step-ci-job.log").read_text()
        self.assertEqual(dc.parse_jobs([("validate", text)]), dc.parse_text(text))

    def test_steps_left_out_are_counted(self):
        step = "##[group]Run sf thing {n}\n##[endgroup]\nsomething failed {n}\n##[error]Process completed with exit code 1.\n"
        text = "".join(step.format(n=n) + "ok\nok\n" for n in range(9))
        parsed = dc.parse_text(text)
        self.assertEqual((len(parsed["failedSteps"]), parsed["failedStepsOmitted"]), (dc.MAX_FAILED_STEPS, 3))


class Redaction(unittest.TestCase):
    def test_record_ids_of_any_object(self):
        for record_id in ("5005g00000AbCdE", "00TAb00001xyzABAAA", "a0BAb00001xyzAB", "00D5g000004abcdEAA",
                          "0Af5g00000Xyz12CAA", "m0Xab0000000123"):
            self.assertEqual(dc.redact(f"record {record_id} is locked"), "record <id> is locked", record_id)

    def test_names_that_only_look_like_ids_stay(self):
        for word in ("AccountScoreSvc", "e2eTestRunner01", "k8sDeployStep12", "202611201234567", "abcdef012345678",
                     "Account_Score__c", "DoctorBrokenService"):
            self.assertEqual(dc.redact(word), word)

    def test_maintenance_is_not_a_field_service_object(self):
        for text in ("Weird text about Maintenance_Plan__c", "Something odd on MaintenancePlan layout",
                     "Maintenance Plan: bad value for restricted picklist"):
            self.assertNotEqual(dc.classify(text, CATALOG)["patternId"], "network-or-platform", text)
        for text in ("The org is under maintenance", "Service unavailable during scheduled maintenance"):
            self.assertEqual(dc.classify(text, CATALOG)["patternId"], "network-or-platform", text)


if __name__ == "__main__":
    unittest.main()
