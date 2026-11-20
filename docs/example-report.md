# Example report

Written by Deploy Doctor on a pull request in a test repository (engine: Claude). The validation ran check-only against a sandbox; the change contained four deliberately broken references. The text is the agent's own; only the formatting of API names was normalised to code and the link to the private run was removed. One number in it is wrong because of a bug in the context builder at that time, since fixed: the log repeats the progress line, and the first one (0 of 6) was taken instead of the last (2 of 6).

---

## Summary

This was a check-only validation of branch `feat/account-score` that failed before deploying anything (0 of 6 components deployed). All four component failures are the same kind of problem: metadata added or changed on this branch refers to custom fields, a custom object, and an Apex class that do not exist anywhere in this repository. This is not an org-drift or environment issue; it is incomplete metadata on this branch, and the author of the change needs to act.

**Totals:** 4 component failures (4 missing-dependency), 0 test failures, 0 coverage warnings, 0 other errors. Run: #2 (private test repository)

## Causes

**1. New field `Account.Doctor_Lookup__c` points to an object that does not exist — `Doctor_Missing_Object__c`**

The lookup field added in this run has `<referenceTo>Doctor_Missing_Object__c</referenceTo>`, but no object with that name exists anywhere in the repository (`force-app/main/default/objects/Account/fields/Doctor_Lookup__c.field-meta.xml:3`). The org correctly rejects it: `referenceTo value of 'Doctor_Missing_Object__c' does not resolve to a valid sObject type`. Confidence: high, since the field itself is new in this run and the target object is entirely absent from source. Next step: add the `Doctor_Missing_Object__c` object (and any required fields) to the deployment, or point the lookup at the correct, already-existing object if the name is a placeholder/typo.

**2. New class `DoctorBrokenService` queries and reads a field that does not exist — `Account.Doctor_Missing_Field__c`**

`DoctorBrokenService.cls` (new in this run) runs `SELECT Id, Doctor_Missing_Field__c FROM Account` (line 3) and then reads `a.Doctor_Missing_Field__c` (line 4). No field with that API name exists in the repository under `objects/Account/fields`. The two reported errors (`No such column 'Doctor_Missing_Field__c' on entity 'Account'` and `Variable does not exist: Doctor_Missing_Field__c`) are a single root cause: the second is a direct consequence of the first. Confidence: high. Next step: add the `Account.Doctor_Missing_Field__c` field to the deployment if it is meant to exist, or correct the field name in `DoctorBrokenService.cls` if it was meant to reference an existing field (for example the newly added `Doctor_Lookup__c`).

**3. Permission set `Doctor_Access` grants access to a field that does not exist — `Account.Doctor_Other_Missing__c`**

The permission set (new/changed in this run) contains `<fieldPermissions><field>Account.Doctor_Other_Missing__c</field>...`, but this field is not present in the repository under any object. Confidence: high. Next step: remove this `fieldPermissions` entry if the field is not meant to be part of this change, or add the field to the deployment if it is.

A related, not-yet-surfaced problem in the same file: `Doctor_Access.permissionset-meta.xml` also grants class access to `<apexClass>DoctorNoSuchClass</apexClass>`, and no Apex class by that name exists in the repository either. This did not appear as a reported error in this run (the validation may have stopped reporting component errors before reaching it), but it will very likely fail the same way once the field-permission issue above is fixed. Recommend fixing it in the same pass: remove the `DoctorNoSuchClass` class access or add the intended class to the deployment.

## Follow-up errors

<details>
<summary>Errors that are direct consequences of cause 2 (collapse once the missing field is resolved)</summary>

- `DoctorBrokenService`: `Variable does not exist: Doctor_Missing_Field__c` (line 4) — disappears once `Account.Doctor_Missing_Field__c` exists or the class is corrected.

</details>

## Not explained

None — all four reported component failures are accounted for above.

