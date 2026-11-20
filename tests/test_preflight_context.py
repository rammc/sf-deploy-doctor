"""Tests for doctor/preflight_context.py. Run with: python3 -m unittest discover -s tests"""
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
        self.assertEqual(ctx["totals"], {"high": 0, "medium": 3, "low": 1})

    def test_component_of(self):
        self.assertEqual(pf.component_of(f"{D}/classes/Score.cls-meta.xml"), {"type": "ApexClass", "name": "Score"})
        self.assertEqual(pf.component_of(f"{D}/objects/Account/fields/Code__c.field-meta.xml"),
                         {"type": "CustomField", "name": "Account.Code__c"})
        self.assertEqual(pf.component_of(f"{D}/objects/Thing__c/Thing__c.object-meta.xml"),
                         {"type": "CustomObject", "name": "Thing__c"})
        self.assertEqual(pf.component_of(f"{D}/lwc/card/card.js"), {"type": "LightningComponentBundle", "name": "card"})
        self.assertIsNone(pf.component_of("README.md"))


if __name__ == "__main__":
    unittest.main()
