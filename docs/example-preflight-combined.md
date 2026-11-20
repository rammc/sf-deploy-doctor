# Example pre-flight comment: a change with many risks

Written by the Deploy Doctor pre-flight on a pull request in a private test repository (engine: Claude). The change deletes a class and a field that are still in use, changes a field type, makes one field required and another unique, adds a validation rule, modifies an active flow, edits a profile, adds an Apex class without a test and refers to a managed package field. No org was involved. The text is unedited.

---

## Verdict: Likely to fail

Two components are deleted from the source while still referenced elsewhere, and a test class inserts `Doctor_Visit__c` records that satisfy neither the new required field nor the new validation rule. Those three things alone are enough to break compilation and test execution. There are also data-risk items (a field type change and a field becoming unique) that need an org check before this goes out.

`components: 10 (2 added, 6 modified, 2 deleted)` · `signals: 3 high, 6 medium, 3 low`

### Likely to fail

- **Deleted class still referenced.** `force-app/main/default/classes/DoctorLegacyHelper.cls` is deleted, but `DoctorSegmentService.cls:3` still calls `DoctorLegacyHelper.normalise(...)`. This will not compile.
- **Deleted field still referenced.** `Account.Doctor_Legacy_Code__c` is deleted, but it is still used in `force-app/main/default/classes/DoctorSegmentService.cls:3`, `force-app/main/default/classes/DoctorSegmentServiceTest.cls:5`, and `force-app/main/default/permissionsets/Doctor_Base_Access.permissionset-meta.xml:11`. Remove or update all three in the same change, or the deletion (and the class above) will fail validation.
- **New required field and new validation rule break an existing test.** `force-app/main/default/objects/Doctor_Visit__c/fields/Reason__c.field-meta.xml` makes `Reason__c` required, and `force-app/main/default/objects/Doctor_Visit__c/validationRules/Account_Is_Required.validationRule-meta.xml` requires `Account__c`. `force-app/main/default/classes/DoctorVisitServiceTest.cls:12` inserts two `Doctor_Visit__c` records with no fields set at all, and `DoctorVisitService.cls:3` (exercised by the `logsVisit` test) inserts a visit with only `Account__c` set, never `Reason__c`. Both will fail at insert once this field and rule are live, and a failing test blocks the deployment.
- **Field type change on `Account.Doctor_Segment__c`** (`Picklist` → `Text`, in `force-app/main/default/objects/Account/fields/Doctor_Segment__c.field-meta.xml`). The source shows no other metadata referencing picklist values of this field, so nothing here blocks on its own, but this kind of change is commonly rejected if a validation rule, flow or record type outside this diff still depends on the old picklist values — that can only be confirmed in the org.

### Risks after deployment

- **Field becomes unique.** `Account.Doctor_Region__c` gains `<unique>true</unique>` (`force-app/main/default/objects/Account/fields/Doctor_Region__c.field-meta.xml:8`). If the org already holds duplicate values, the deployment itself fails; this cannot be confirmed from the source.
- **Active flow modified.** `force-app/main/default/flows/Doctor_Account_Intake.flow-meta.xml` is `Active`. The change goes live as a new active version (or fails to deploy if the org does not deploy flows as active), and any paused interviews keep running on the old version.
- **New Apex class without a test.** `force-app/main/default/classes/DoctorRegionExporter.cls` is not referenced by any test class in this change.
- **Deleted components stay in the org.** Deleting `force-app/main/default/classes/DoctorLegacyHelper.cls` and `force-app/main/default/objects/Account/fields/Doctor_Legacy_Code__c.field-meta.xml` from source does not remove them from the org; a destructive-changes manifest is needed, and removing the field deletes its stored data.
- **Managed package reference.** `Doctor_Base_Access.permissionset-meta.xml:16` references `Account.docpkg__Score__c`, which carries a namespace prefix and so comes from a managed package.

### Check before merging

- Will this change remove or update the tests and permission-set entry that still reference `Doctor_Legacy_Code__c` and `DoctorLegacyHelper`?
- Does the target org currently hold duplicate values in `Account.Doctor_Region__c`?
- Does any validation rule, flow or record type outside this diff depend on the current picklist values of `Account.Doctor_Segment__c`?
- Is "Deploy processes and flows as active" enabled for the target org, and have the flow's own tests been run against this version?
- Does this deployment include a destructive-changes manifest for `DoctorLegacyHelper` and `Account.Doctor_Legacy_Code__c`, and has the data in `Doctor_Legacy_Code__c` been migrated or confirmed unneeded?
- Is the package that provides `Account.docpkg__Score__c` installed in every target org?

### Checked and fine

- The profile change in `force-app/main/default/profiles/Doctor Standard.profile-meta.xml` only grants access to `DoctorSegmentService`, which is already part of this deployment; nothing in the file points at components outside this change.
