# Example pre-flight comment

Written by the Deploy Doctor pre-flight on a pull request in a private test repository (engine: Claude), before any validation ran. The change is the same one as in the [example report](example-report.md). The text is unedited.

---

**Verdict: Likely to fail.** All three new components reference names — a field, an object, another field, and an Apex class — that are not defined anywhere in this repository. They all follow this PR's own `Doctor_` / `Doctor` naming, which suggests pieces of the change were left out rather than that these are long-standing org components.

`3 components added (1 ApexClass, 1 CustomField, 1 PermissionSet), 0 modified, 0 deleted` — `4 high, 1 medium, 0 low signals`

### Likely to fail

- `force-app/main/default/classes/DoctorBrokenService.cls:3` queries `Doctor_Missing_Field__c` on `Account`, but no `CustomField` named `Doctor_Missing_Field__c` exists in the source. If it is not already present in the target org, the SOQL compile step fails the deployment.
- `force-app/main/default/objects/Account/fields/Doctor_Lookup__c.field-meta.xml:2` sets `<referenceTo>Doctor_Missing_Object__c</referenceTo>`, but no object by that name is defined in the source. A lookup field cannot deploy to a target object that does not exist.
- `force-app/main/default/permissionsets/Doctor_Access.permissionset-meta.xml:3` grants access to `Account.Doctor_Other_Missing__c`, a third field name distinct from `Doctor_Missing_Field__c` above, and it is also not defined anywhere in the source.
- `force-app/main/default/permissionsets/Doctor_Access.permissionset-meta.xml:4` grants class access to `DoctorNoSuchClass`, which is a different name from the `DoctorBrokenService` class added in this same PR and is not defined anywhere in the source.

Fix: either add the missing `Doctor_Missing_Field__c` field, `Doctor_Missing_Object__c` object and `DoctorNoSuchClass` class (or correct what look like typos against the components actually in this PR), or confirm each one already exists in every target org before deploying.

### Risks after deployment

- No test class in the source refers to `DoctorBrokenService` (`force-app/main/default/classes/DoctorBrokenService.cls`). If this org requires 75% coverage or uses `RunSpecifiedTests`, this class will not count toward coverage on its own, independent of the missing-reference issues above.

### Check before merging

- Does `Account.Doctor_Missing_Field__c` already exist in the target org?
- Does the object `Doctor_Missing_Object__c` already exist in the target org?
- Does `Account.Doctor_Other_Missing__c` already exist in the target org?
- Does the Apex class `DoctorNoSuchClass` already exist in the target org?
- Is there a test class elsewhere (not in this PR) that already covers `DoctorBrokenService`?
