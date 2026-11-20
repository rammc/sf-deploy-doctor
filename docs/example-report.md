# Example report

Written by Deploy Doctor as a comment on a pull request in a private test repository (engine: Claude). The validation ran check-only against a sandbox; the change contained deliberately broken references. The text is unedited apart from the removed link to the private run.

---

## Deploy Doctor report

### Summary

This validation (check-only, dry run) failed because the deployment references three components that do not exist anywhere in the repository: the custom object `Doctor_Missing_Object__c`, and two custom fields, `Account.Doctor_Missing_Field__c` and `Account.Doctor_Other_Missing__c`. All four reported failures are missing-dependency errors, and all of them trace back to files changed in this run, so the change itself is the cause, not the target org. Fix this by either adding the missing object/field metadata to the deployment or correcting the names if they were meant to reference something else.

`componentFailures: 4` · `testFailures: 0` · `coverageWarnings: 0` · `otherErrors: 0` · `2/6` components deployed — run #2

### Causes

**1. `Doctor_Lookup__c` points to an object that was never added to the repository**

`force-app/main/default/objects/Account/fields/Doctor_Lookup__c.field-meta.xml:3` defines a lookup field with `<referenceTo>Doctor_Missing_Object__c</referenceTo>`, but there is no `Doctor_Missing_Object__c` object metadata anywhere in `force-app`. This file was changed in this run. Confidence: high.
Next step: add the `Doctor_Missing_Object__c` object (and its fields) to the deployment, or, if the lookup was meant to target an existing object, correct `referenceTo` in `Doctor_Lookup__c.field-meta.xml`.

**2. `DoctorBrokenService` queries and reads a field that does not exist on `Account`**

`force-app/main/default/classes/DoctorBrokenService.cls:3` runs `SELECT Id, Doctor_Missing_Field__c FROM Account`, and line 4 reads `a.Doctor_Missing_Field__c`. There is no `Doctor_Missing_Field__c` field under `force-app/main/default/objects/Account/fields/` (only `Doctor_Lookup__c` exists there), so this field is missing from the repository entirely, not merely excluded from this deployment. This class was changed in this run. Confidence: high.
Next step: add the `Account.Doctor_Missing_Field__c` field metadata to the deployment, or update `DoctorBrokenService.cls` to use the correct field name.

**3. `Doctor_Access` permission set grants access to a field that does not exist**

`force-app/main/default/permissionsets/Doctor_Access.permissionset-meta.xml:3` grants `editable`/`readable` on `Account.Doctor_Other_Missing__c`, a field that is not present anywhere in `force-app`. This is a different field name from cause 2, so it is a separate root cause, not a follow-up. This permission set was changed in this run. Confidence: high.
Next step: add the `Account.Doctor_Other_Missing__c` field to the deployment, or remove the `fieldPermissions` entry from `Doctor_Access.permissionset-meta.xml` if the field reference is stale.

### Follow-up errors

- The `Variable does not exist: Doctor_Missing_Field__c` Apex compile error on `DoctorBrokenService.cls:4` is a direct follow-up of cause 2 (the SOQL failure on line 3) and will disappear once that field is added or the query is corrected.

### Not explained

`Doctor_Access.permissionset-meta.xml` also lists `<classAccesses><apexClass>DoctorNoSuchClass</apexClass></classAccesses>` (line 4), referencing an Apex class that is not present in `force-app/main/default/classes`. This was not reported among the four failures in this run's log, possibly because validation stopped reporting further errors for this component after the field-permission error above. Worth checking once cause 3 is fixed, since it looks like it would fail the same way.

