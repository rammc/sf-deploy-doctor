"""Tests for doctor/preflight_context.py. Run with: python3 -m unittest discover -s tests"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "doctor"))
import preflight_context as pf  # noqa: E402

D = "force-app/main/default"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def write(repo, rel, text="x\n"):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def field(ftype="Text", extra=""):
    return f"<CustomField>\n<fullName>X</fullName>\n<type>{ftype}</type>\n{extra}</CustomField>\n"


class Preflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo, f"{D}/classes/Score.cls", "public class Score {\n  public static Integer one() { return 1; }\n}\n")
        write(self.repo, f"{D}/classes/ScoreTest.cls", "@IsTest\nprivate class ScoreTest {\n  @IsTest static void t() { Score.one(); }\n}\n")
        write(self.repo, f"{D}/classes/Legacy.cls", "public class Legacy {}\n")
        write(self.repo, f"{D}/classes/UsesLegacy.cls", "public class UsesLegacy {\n  Legacy l;\n}\n")
        write(self.repo, f"{D}/objects/Account/fields/Code__c.field-meta.xml", field("Picklist"))
        write(self.repo, f"{D}/objects/Account/fields/Known__c.field-meta.xml", field())
        write(self.repo, f"{D}/objects/Account/fields/Old__c.field-meta.xml", field())
        write(self.repo, f"{D}/permissionsets/Access.permissionset-meta.xml", "<PermissionSet>\n</PermissionSet>\n")
        write(self.repo, f"{D}/flows/Intake.flow-meta.xml", "<Flow>\n<status>Active</status>\n<label>a</label>\n</Flow>\n")
        write(self.repo, "README.md", "docs\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "switch", "-q", "-c", "change")

    def tearDown(self):
        self.tmp.cleanup()

    def run_preflight(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        # the working tree must not matter: go back to main before analysing
        git(self.repo, "switch", "-q", "main")
        return pf.build(self.repo, "main", "change")

    def rules(self, ctx):
        return {(s["rule"], s["component"]) for s in ctx["signals"]}

    def test_clean_change_has_no_signals(self):
        write(self.repo, f"{D}/classes/Score.cls", "public class Score {\n  public static Integer one() { return 2; }\n}\n")
        write(self.repo, "README.md", "more docs\n")
        ctx = self.run_preflight()
        self.assertEqual(ctx["signals"], [])
        self.assertEqual(ctx["summary"]["components"], 1)
        self.assertEqual(ctx["components"][0]["tests"][0]["file"], f"{D}/classes/ScoreTest.cls")

    def test_references_that_are_not_in_the_source(self):
        write(self.repo, f"{D}/classes/Score.cls", "\n".join([
            "public class Score {",
            "  /* Missing_In_Comment__c does not count */",
            "  // Nor_This__c",
            "  String s = 'Nor_A_String__c';",
            "  public static Object one() { return [SELECT Known__c, Gone__c, Leads__r.Name FROM Account]; }",
            "}", ""]))
        write(self.repo, f"{D}/permissionsets/Access.permissionset-meta.xml", "\n".join([
            "<PermissionSet>",
            "<field>Account.Known__c</field>",
            "<field>Account.Gone__c</field>",
            "<apexClass>NoSuchClass</apexClass>",
            "<apexClass>Score</apexClass>",
            "<field>pkg__Thing__c.pkg__Field__c</field>",
            "</PermissionSet>", ""]))
        ctx = self.run_preflight()
        missing = {s["evidence"]: s for s in ctx["signals"] if s["rule"] == "reference-not-in-source"}
        self.assertEqual(set(missing), {"Gone__c", "<field>Account.Gone__c</field>", "<apexClass>NoSuchClass</apexClass>"})
        self.assertEqual(missing["Gone__c"]["line"], 5)
        self.assertEqual(missing["Gone__c"]["severity"], "high")
        self.assertIn(("package-reference", "PermissionSet:Access"), self.rules(ctx))

    def test_same_missing_name_in_several_files_is_one_signal(self):
        for n in (1, 2, 3):
            write(self.repo, f"{D}/customMetadata/Cfg.R{n}.md-meta.xml", "<CustomMetadata>\n<field>Path__c</field>\n</CustomMetadata>\n")
        ctx = self.run_preflight()
        missing = [s for s in ctx["signals"] if s["rule"] == "reference-not-in-source"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["occurrences"], 3)
        self.assertEqual(len(missing[0]["alsoIn"]), 2)

    def test_deleted_component_that_is_still_used(self):
        (self.repo / f"{D}/classes/Legacy.cls").unlink()
        (self.repo / f"{D}/objects/Account/fields/Old__c.field-meta.xml").unlink()
        ctx = self.run_preflight()
        self.assertIn(("deleted-still-referenced", "ApexClass:Legacy"), self.rules(ctx))
        self.assertNotIn(("deleted-still-referenced", "CustomField:Account.Old__c"), self.rules(ctx))
        severity = {s["component"]: s["severity"] for s in ctx["signals"] if s["rule"] == "destructive-change"}
        self.assertEqual(severity, {"ApexClass:Legacy": "low", "CustomField:Account.Old__c": "medium"})
        self.assertEqual(ctx["summary"]["deleted"], 2)

    def test_deleted_and_added_class_with_identical_meta_files_are_not_a_rename(self):
        meta = "<ApexClass><apiVersion>67.0</apiVersion></ApexClass>\n"
        write(self.repo, f"{D}/classes/Legacy.cls-meta.xml", meta)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "meta")
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "merge", "-q", "change")
        git(self.repo, "switch", "-q", "change")
        (self.repo / f"{D}/classes/Legacy.cls").unlink()
        (self.repo / f"{D}/classes/Legacy.cls-meta.xml").unlink()
        write(self.repo, f"{D}/classes/Exporter.cls", "public class Exporter {}\n")
        write(self.repo, f"{D}/classes/Exporter.cls-meta.xml", meta)
        ctx = self.run_preflight()
        changes = {(c["name"], c["change"]) for c in ctx["components"]}
        self.assertEqual(changes, {("Legacy", "deleted"), ("Exporter", "added")})
        self.assertEqual(sum(s["rule"] == "apex-without-test" for s in ctx["signals"]), 1)
        self.assertTrue(next(c for c in ctx["components"] if c["name"] == "Exporter")["file"].endswith("Exporter.cls"))

    def test_field_changes(self):
        write(self.repo, f"{D}/objects/Account/fields/Code__c.field-meta.xml", field("Text", "<unique>true</unique>\n"))
        write(self.repo, f"{D}/objects/Account/fields/Known__c.field-meta.xml", field("Text", "<required>true</required>\n"))
        ctx = self.run_preflight()
        rules = self.rules(ctx)
        self.assertIn(("field-type-change", "CustomField:Account.Code__c"), rules)
        self.assertIn(("field-becomes-unique", "CustomField:Account.Code__c"), rules)
        self.assertIn(("new-required-field", "CustomField:Account.Known__c"), rules)
        change = next(s for s in ctx["signals"] if s["rule"] == "field-type-change")
        self.assertEqual(change["evidence"], "Picklist -> Text")
        self.assertEqual(ctx["signals"][0]["severity"], "high")  # sorted by severity

    def test_apex_without_test_validation_rule_profile_and_flow(self):
        write(self.repo, f"{D}/classes/Lonely.cls", "public class Lonely {}\n")
        write(self.repo, f"{D}/classes/Shape.cls", "public interface Shape {}\n")
        write(self.repo, f"{D}/objects/Account/validationRules/Need_Code.validationRule-meta.xml",
              "<ValidationRule>\n<active>true</active>\n<errorConditionFormula>ISBLANK(Name)</errorConditionFormula>\n<errorMessage>Name it</errorMessage>\n</ValidationRule>\n")
        write(self.repo, f"{D}/profiles/Admin.profile-meta.xml", "<Profile>\n</Profile>\n")
        write(self.repo, f"{D}/flows/Intake.flow-meta.xml", "<Flow>\n<status>Active</status>\n<label>b</label>\n</Flow>\n")
        ctx = self.run_preflight()
        rules = self.rules(ctx)
        self.assertIn(("apex-without-test", "ApexClass:Lonely"), rules)
        self.assertNotIn(("apex-without-test", "ApexClass:Shape"), rules)
        self.assertIn(("validation-rule-active", "ValidationRule:Account.Need_Code"), rules)
        self.assertIn(("profile-change", "Profile:Admin"), rules)
        self.assertIn(("active-flow-change", "Flow:Intake"), rules)
        self.assertEqual(ctx["totals"], {"high": 0, "medium": 2, "low": 2})

    def test_a_flow_that_the_change_switches_on_is_medium(self):
        git(self.repo, "switch", "-q", "main")
        write(self.repo, f"{D}/flows/Draft.flow-meta.xml", "<Flow>\n<status>Draft</status>\n</Flow>\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "draft flow")
        git(self.repo, "switch", "-q", "-c", "activate")
        write(self.repo, f"{D}/flows/Draft.flow-meta.xml", "<Flow>\n<status>Active</status>\n</Flow>\n")
        write(self.repo, f"{D}/flows/Intake.flow-meta.xml", "<Flow>\n<status>Active</status>\n<label>c</label>\n</Flow>\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        found = {s["component"]: (s["severity"], s["title"]) for s in pf.build(self.repo, "main", "activate")["signals"]}
        self.assertEqual(found, {"Flow:Draft": ("medium", "A flow is activated"),
                                 "Flow:Intake": ("low", "An active flow is modified")})

    def test_a_name_the_base_branch_already_uses_is_a_low_signal(self):
        """An org that is only partly in version control: the field was never in the repository."""
        git(self.repo, "switch", "-q", "main")
        write(self.repo, f"{D}/classes/Old.cls", "public class Old {\n  Object o = [SELECT Grown__c FROM Account];\n}\n")
        write(self.repo, f"{D}/classes/OldTest.cls", "@IsTest\nprivate class OldTest {\n  @IsTest static void t() { new Old(); }\n}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "grown org")
        git(self.repo, "switch", "-q", "-c", "change2")
        write(self.repo, f"{D}/classes/Score.cls", "public class Score {\n  Object a = [SELECT Grown__c, Brand_New__c FROM Account];\n}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        ctx = pf.build(self.repo, "main", "change2")
        found = {s["why"].split("`")[1]: s for s in ctx["signals"] if s["rule"] == "reference-not-in-source"}
        self.assertEqual(found["Brand_New__c"]["severity"], "high")
        self.assertNotIn("usedBefore", found["Brand_New__c"])
        self.assertEqual(found["Grown__c"]["severity"], "low")
        self.assertEqual(found["Grown__c"]["usedBefore"], {"files": 1, "example": f"{D}/classes/Old.cls"})
        self.assertEqual(ctx["totals"], {"high": 1, "medium": 0, "low": 1})

    def test_a_project_with_package_dependencies_gets_medium_signals_for_new_missing_names(self):
        """Unlocked packages have no namespace, so their fields look like fields of this project."""
        write(self.repo, "sfdx-project.json", '{"packageDirectories": [{"path": "force-app", "dependencies": '
                                              '[{"package": "shared-base", "versionNumber": "1.0.0.LATEST"}]}]}')
        write(self.repo, f"{D}/classes/Score.cls", "public class Score {\n  Object a = [SELECT From_Package__c FROM Account];\n}\n")
        ctx = self.run_preflight()
        signal = next(s for s in ctx["signals"] if s["rule"] == "reference-not-in-source")
        self.assertEqual((signal["severity"], signal["dependencies"]), ("medium", ["shared-base"]))

    def test_a_package_name_the_base_branch_already_uses_stays_low(self):
        git(self.repo, "switch", "-q", "main")
        write(self.repo, "sfdx-project.json", '{"packageDirectories": [{"path": "force-app", "dependencies": '
                                              '[{"package": "shared-base", "versionNumber": "1.0.0.LATEST"}]}]}')
        write(self.repo, f"{D}/classes/Old.cls", "public class Old {\n  Object a = [SELECT From_Package__c FROM Account];\n}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base uses the package field")
        git(self.repo, "switch", "-q", "-c", "change2")
        write(self.repo, f"{D}/classes/Score.cls", "public class Score {\n  Object a = [SELECT From_Package__c FROM Account];\n}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        ctx = pf.build(self.repo, "main", "change2")
        signal = next(s for s in ctx["signals"] if s["rule"] == "reference-not-in-source")
        self.assertEqual(signal["severity"], "low")
        self.assertIn("usedBefore", signal)
        self.assertNotIn("dependencies", signal)

    def test_a_deleted_class_named_in_a_changelog_is_not_still_referenced(self):
        git(self.repo, "switch", "-q", "main")
        write(self.repo, "CHANGELOG.md", "Legacy was added\n")
        write(self.repo, ".sfdx/tsconfig.sfdx.json", '{"Legacy": 1}\n')
        write(self.repo, f"{D}/lwc/typings/legacy.d.ts", "declare module Legacy {}\n")
        (self.repo / f"{D}/classes/UsesLegacy.cls").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "docs")
        git(self.repo, "switch", "-q", "-c", "remove")
        (self.repo / f"{D}/classes/Legacy.cls").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        rules = {s["rule"] for s in pf.build(self.repo, "main", "remove")["signals"]}
        self.assertEqual(rules, {"destructive-change"})

    def test_untested_code_that_is_not_new_is_a_low_signal(self):
        write(self.repo, f"{D}/classes/Legacy.cls", "public class Legacy { Integer i; }\n")
        write(self.repo, f"{D}/classes/Fresh.cls", "public class Fresh {}\n")
        ctx = self.run_preflight()
        found = {s["component"]: s["severity"] for s in ctx["signals"] if s["rule"] == "apex-without-test"}
        self.assertEqual(found, {"ApexClass:Legacy": "low", "ApexClass:Fresh": "medium"})

    def test_gate_counts_from_the_threshold_up(self):
        write(self.repo, f"{D}/classes/Legacy.cls", "public class Legacy { Integer i; }\n")
        write(self.repo, f"{D}/profiles/Admin.profile-meta.xml", "<Profile>\n</Profile>\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        script = str(ROOT / "doctor" / "preflight_context.py")
        counts = {}
        for level in (None, "low", "high"):
            out, github = Path(self.tmp.name) / "pf.json", Path(self.tmp.name) / f"out-{level}"
            subprocess.run([sys.executable, script, "--repo-dir", str(self.repo), "--base", "main", "--head", "change",
                            "--out", str(out)] + (["--min-severity", level] if level else []),
                           check=True, capture_output=True, env={"GITHUB_OUTPUT": str(github), "PATH": __import__("os").environ["PATH"]})
            counts[level] = dict(l.split("=") for l in github.read_text().splitlines())
        self.assertEqual(counts[None]["signals"], "0")       # two low signals do not wake the agent
        self.assertEqual(counts[None]["signals_total"], "2")
        self.assertEqual(counts["low"]["signals"], "2")
        self.assertEqual(counts["high"]["signals"], "0")

    def config_on_main(self, text):
        git(self.repo, "switch", "-q", "main")
        write(self.repo, ".github/deploy-doctor.json", text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "config")
        git(self.repo, "switch", "-q", "-c", "change2")

    def test_known_references_from_the_base_branch_are_low(self):
        self.config_on_main('{"preflight": {"knownReferences": ["Account.Legacy_*", "Hand_Made__c"]}}')
        write(self.repo, f"{D}/classes/Score.cls",
              "public class Score {\n  Object a = [SELECT Legacy_Code__c, Hand_Made__c, Typo__c FROM Account];\n}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        ctx = pf.build(self.repo, "main", "change2")
        found = {s["why"].split("`")[1].rpartition(".")[2]: s for s in ctx["signals"] if s["rule"] == "reference-not-in-source"}
        self.assertEqual({k: v["severity"] for k, v in found.items()},
                         {"Legacy_Code__c": "low", "Hand_Made__c": "low", "Typo__c": "high"})
        self.assertEqual(found["Legacy_Code__c"]["knownReference"], "Account.Legacy_*")

    def test_a_pull_request_cannot_silence_itself(self):
        write(self.repo, ".github/deploy-doctor.json", '{"preflight": {"knownReferences": ["*"], "minSeverity": "high"}}')
        write(self.repo, f"{D}/classes/Score.cls", "public class Score {\n  Object a = [SELECT Typo__c FROM Account];\n}\n")
        ctx = self.run_preflight()
        signal = next(s for s in ctx["signals"] if s["rule"] == "reference-not-in-source")
        self.assertEqual(signal["severity"], "high")
        self.assertIsNone(ctx["config"]["minSeverity"])

    def test_threshold_from_the_configuration_and_the_flag_wins(self):
        self.config_on_main('{"preflight": {"minSeverity": "low"}}')
        write(self.repo, f"{D}/profiles/Admin.profile-meta.xml", "<Profile>\n</Profile>\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        script, counts = str(ROOT / "doctor" / "preflight_context.py"), {}
        for level in (None, "high"):
            out, github = Path(self.tmp.name) / "pf.json", Path(self.tmp.name) / f"cfg-{level}"
            subprocess.run([sys.executable, script, "--repo-dir", str(self.repo), "--base", "main", "--head", "change2",
                            "--out", str(out)] + (["--min-severity", level] if level else []),
                           check=True, capture_output=True, env={"GITHUB_OUTPUT": str(github), "PATH": __import__("os").environ["PATH"]})
            counts[level] = dict(l.split("=") for l in github.read_text().splitlines())["signals"]
            self.assertNotIn("config", json.loads(out.read_text()))
        self.assertEqual(counts, {None: "1", "high": "0"})

    def test_component_of(self):
        self.assertEqual(pf.component_of(f"{D}/classes/Score.cls-meta.xml"), {"type": "ApexClass", "name": "Score"})
        self.assertEqual(pf.component_of(f"{D}/objects/Account/fields/Code__c.field-meta.xml"),
                         {"type": "CustomField", "name": "Account.Code__c"})
        self.assertEqual(pf.component_of(f"{D}/objects/Thing__c/Thing__c.object-meta.xml"),
                         {"type": "CustomObject", "name": "Thing__c"})
        self.assertEqual(pf.component_of(f"{D}/lwc/card/card.js"), {"type": "LightningComponentBundle", "name": "card"})
        self.assertIsNone(pf.component_of("README.md"))


LWC = f"{D}/lwc"


def bundle(repo, name, js="", html=""):
    write(repo, f"{LWC}/{name}/{name}.js",
          "import { LightningElement } from 'lwc';\n" + js + "export default class X extends LightningElement {}\n")
    write(repo, f"{LWC}/{name}/{name}.html", f"<template>\n{html}</template>\n")
    write(repo, f"{LWC}/{name}/{name}.js-meta.xml", "<LightningComponentBundle/>\n")


class Lightning(unittest.TestCase):
    """Imports of Lightning web components and Aura components."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo, f"{D}/classes/CardController.cls",
              "public with sharing class CardController {\n"
              "  @AuraEnabled(cacheable=true)\n  public static String load(Id recordId) { return 'a'; }\n"
              "  @AuraEnabled\n  @TestVisible\n  public static void save(String v) {}\n"
              "  // @AuraEnabled public static void ghost() {}\n"
              "  public static String internal() { return 'b'; }\n}\n")
        write(self.repo, f"{D}/objects/Account/fields/Known__c.field-meta.xml", field())
        write(self.repo, f"{D}/objects/Account/fields/Partner__c.field-meta.xml", field("Lookup"))
        write(self.repo, f"{D}/objects/Visit__c/Visit__c.object-meta.xml", "<CustomObject/>\n")
        write(self.repo, f"{D}/labels/CustomLabels.labels-meta.xml",
              "<CustomLabels>\n<labels>\n<fullName>Greeting</fullName>\n</labels>\n</CustomLabels>\n")
        write(self.repo, f"{D}/staticresources/logo.resource-meta.xml", "<StaticResource/>\n")
        write(self.repo, f"{D}/messageChannels/Refresh.messageChannel-meta.xml", "<LightningMessageChannel/>\n")
        write(self.repo, f"{LWC}/jsconfig.json", "{}\n")
        bundle(self.repo, "childCard")
        bundle(self.repo, "parentCard", "import load from '@salesforce/apex/CardController.load';\n", "<c-child-card></c-child-card>\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "switch", "-q", "-c", "change")

    def tearDown(self):
        self.tmp.cleanup()

    def run_preflight(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        return pf.build(self.repo, "main", "change")

    def by_rule(self, ctx, rule):
        return [s for s in ctx["signals"] if s["rule"] == rule]

    def test_valid_imports_give_no_signal(self):
        bundle(self.repo, "goodCard",
               "import load from '@salesforce/apex/CardController.load';\n"
               "import save from '@salesforce/apex/CardController.save';\n"
               "import KNOWN from '@salesforce/schema/Account.Known__c';\n"
               "import PARTNER_NAME from '@salesforce/schema/Account.Partner__r.Name';\n"
               "import NAME from '@salesforce/schema/Account.Name';\n"
               "import VISIT from '@salesforce/schema/Visit__c';\n"
               "import greeting from '@salesforce/label/c.Greeting';\n"
               "import LOGO from '@salesforce/resourceUrl/logo';\n"
               "import REFRESH from '@salesforce/messageChannel/Refresh__c';\n"
               "import userId from '@salesforce/user/Id';\n"
               "import { helper } from 'c/childCard';\n"
               "// import gone from '@salesforce/apex/Nowhere.run';\n",
               "<c-child-card></c-child-card>\n<lightning-button></lightning-button>\n")
        ctx = self.run_preflight()
        self.assertEqual(ctx["signals"], [])
        self.assertEqual(ctx["summary"]["byType"], {"LightningComponentBundle": 1})
        self.assertEqual(ctx["summary"]["added"], 1)

    def test_imports_of_things_the_source_lacks(self):
        bundle(self.repo, "badCard",
               "import run from '@salesforce/apex/NoSuchClass.run';\n"
               "import F from '@salesforce/schema/Account.Missing__c';\n"
               "import R from '@salesforce/schema/Account.Gone__r.Name';\n"
               "import O from '@salesforce/schema/Nothing__c.Name';\n"
               "import l from '@salesforce/label/c.NoLabel';\n"
               "import r from '@salesforce/resourceUrl/noRes';\n"
               "import p from '@salesforce/customPermission/NoPerm';\n"
               "import c from '@salesforce/messageChannel/NoChannel__c';\n"
               "import m from 'c/noModule';\n",
               "<c-no-child></c-no-child>\n")
        ctx = self.run_preflight()
        missing = self.by_rule(ctx, "reference-not-in-source")
        names = {s["why"].split("`")[1] for s in missing}
        self.assertEqual(names, {"NoSuchClass", "Account.Missing__c", "Account.Gone__c", "Nothing__c", "NoLabel",
                                 "noRes", "NoPerm", "NoChannel", "noModule", "noChild"})
        self.assertTrue(all(s["severity"] == "high" and s["line"] for s in missing))
        html = next(s for s in missing if "noChild" in s["why"])
        self.assertTrue(html["file"].endswith("badCard.html"))

    def test_apex_method_missing_or_not_aura_enabled(self):
        bundle(self.repo, "methodCard",
               "import a from '@salesforce/apex/CardController.internal';\n"
               "import b from '@salesforce/apex/CardController.nothing';\n"
               "import c from '@salesforce/apex/CardController.ghost';\n"
               "import d from '@salesforce/apex/CardController.LOAD';\n")
        ctx = self.run_preflight()
        found = {s["evidence"].split(".")[-1].rstrip("';"): s for s in self.by_rule(ctx, "apex-method-not-exposed")}
        self.assertEqual(set(found), {"internal", "nothing", "ghost"})
        self.assertIn("without `@AuraEnabled`", found["internal"]["why"])
        self.assertIn("has no method", found["nothing"]["why"])
        self.assertEqual(found["internal"]["severity"], "high")

    def test_removed_aura_enabled_breaks_existing_component(self):
        write(self.repo, f"{D}/classes/CardController.cls",
              "public with sharing class CardController {\n"
              "  public static String load(Id recordId) { return 'a'; }\n"
              "  @AuraEnabled\n  public static void save(String v) {}\n}\n")
        ctx = self.run_preflight()
        found = self.by_rule(ctx, "apex-method-not-exposed")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["component"], "LightningComponentBundle:parentCard")
        self.assertTrue(found[0]["file"].endswith("parentCard.js"))
        self.assertEqual(found[0]["line"], 2)

    def test_namespaced_imports_are_package_references(self):
        bundle(self.repo, "pkgCard",
               "import a from '@salesforce/apex/ns.Remote.run';\n"
               "import l from '@salesforce/label/ns.Hello';\n"
               "import f from '@salesforce/schema/Account.ns__Score__c';\n")
        ctx = self.run_preflight()
        packaged = self.by_rule(ctx, "package-reference")  # merged into one finding
        self.assertEqual([p["occurrences"] for p in packaged], [3])
        self.assertEqual(self.by_rule(ctx, "reference-not-in-source"), [])

    def test_deleting_one_file_does_not_delete_the_bundle(self):
        (self.repo / f"{LWC}/parentCard/parentCard.html").unlink()
        ctx = self.run_preflight()
        self.assertEqual([(c["name"], c["change"]) for c in ctx["components"]], [("parentCard", "modified")])
        self.assertEqual(ctx["signals"], [])

    def test_deleted_bundle_still_used_in_markup(self):
        for f in (self.repo / f"{LWC}/childCard").iterdir():
            f.unlink()
        bundle(self.repo, "childCardList")
        ctx = self.run_preflight()
        found = self.by_rule(ctx, "deleted-still-referenced")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["component"], "LightningComponentBundle:childCard")
        self.assertIn("parentCard.html", found[0]["evidence"])

    def test_files_below_lwc_and_jest_tests_are_ignored(self):
        write(self.repo, f"{LWC}/jsconfig.json", '{"a": 1}\n')
        write(self.repo, f"{LWC}/parentCard/__tests__/parentCard.test.js",
              "import x from '@salesforce/apex/Mocked.run';\n")
        ctx = self.run_preflight()
        self.assertEqual(ctx["signals"], [])
        self.assertEqual(pf.component_of(f"{LWC}/jsconfig.json"), None)

    def test_aura_component(self):
        write(self.repo, f"{D}/aura/panel/panel.cmp",
              '<aura:component controller="CardController">\n<c:childCard/>\n<c:lost/>\n</aura:component>\n')
        write(self.repo, f"{D}/aura/other/other.cmp", '<aura:component controller="NoController"/>\n')
        ctx = self.run_preflight()
        names = {s["why"].split("`")[1] for s in self.by_rule(ctx, "reference-not-in-source")}
        self.assertEqual(names, {"lost", "NoController"})


UI = f"{D}/uiBundles/showroom"


class UiBundle(unittest.TestCase):
    """React, Angular and other front-end applications deployed as UI bundles."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo, f"{D}/objects/Asset/fields/Engine__c.field-meta.xml", field())
        write(self.repo, f"{D}/objects/Visit__c/Visit__c.object-meta.xml", "<CustomObject/>\n")
        write(self.repo, f"{UI}/showroom.uibundle-meta.xml", "<UIBundle/>\n")
        write(self.repo, f"{UI}/src/api/inventory.ts", "export const Q = `query { uiapi { query { Asset { edges { node { Id } } } } } }`;\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "switch", "-q", "-c", "change")

    def tearDown(self):
        self.tmp.cleanup()

    def run_preflight(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        git(self.repo, "switch", "-q", "main")
        return pf.build(self.repo, "main", "change")

    def test_bundle_is_one_component(self):
        self.assertEqual(pf.component_of(f"{UI}/src/api/inventory.ts"), {"type": "UIBundle", "name": "showroom"})
        write(self.repo, f"{UI}/src/api/inventory.ts", "export const Q = `query { Asset { Engine__c { value } } }`;\n")
        write(self.repo, f"{UI}/src/pages/Home.tsx", "export const Home = () => <p>Visit__c</p>;\n")
        ctx = self.run_preflight()
        self.assertEqual(ctx["summary"]["byType"], {"UIBundle": 1})
        self.assertEqual(ctx["components"][0]["change"], "modified")
        self.assertEqual(ctx["signals"], [])

    def test_field_the_source_does_not_define(self):
        write(self.repo, f"{UI}/src/api/inventory.ts",
              "export const Q = `query { Asset { Engine__c { value } Colour__c { value } Owner__r { Name { value } } } }`;\n"
              "// Legacy__c was removed\n")
        write(self.repo, f"{UI}/src/app/visit.component.ts", "const fields = ['Colour__c', 'pkg__Score__c'];\n")
        ctx = self.run_preflight()
        found = [s for s in ctx["signals"] if s["rule"] == "ui-reference-not-in-source"]
        self.assertEqual(len(found), 1)
        self.assertIn("`Colour__c`", found[0]["why"])
        self.assertEqual((found[0]["severity"], found[0]["line"]), ("medium", 1))
        self.assertTrue(found[0]["file"].endswith("src/api/inventory.ts"))
        self.assertEqual([s["rule"] for s in ctx["signals"] if s["severity"] == "low"], ["package-reference"])

    def test_tests_build_output_and_dependencies_are_ignored(self):
        for rel in ("src/api/inventory.test.ts", "src/__tests__/a.ts", "dist/assets/app.js",
                    "node_modules/x/index.js", "e2e/flow.spec.ts", "src/types/schema.d.ts", "package.json"):
            write(self.repo, f"{UI}/{rel}", "const f = 'Nowhere__c';\n")
        ctx = self.run_preflight()
        self.assertEqual(ctx["signals"], [])


if __name__ == "__main__":
    unittest.main()
