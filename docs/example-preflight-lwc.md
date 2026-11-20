# Example pre-flight comment: a Lightning web component with broken imports

Written by the Deploy Doctor pre-flight on a pull request in a private test repository (engine: Claude). The change adds a Lightning web component and its Apex controller. The component imports a method without `@AuraEnabled`, a field and a custom label that are not in the source, and uses a child component that does not exist. No org was involved. The text is unedited.

The validation of the same pull request against a sandbox reported two of these four problems; the other two would have surfaced only after the first were fixed.

---

**Verdict: Likely to fail** — one signal is a confirmed Apex exposure bug that will break the component's deployment on its own, and three more references point to names that are not defined anywhere in the repository but look like they belong to this same feature.

Components: 3 added, 0 modified, 0 deleted (`ApexClass` x2, `LightningComponentBundle` x1). Signals: 4 high, 0 medium, 0 low.

### Likely to fail

1. **Apex method not exposed to Lightning** — `force-app/main/default/lwc/doctorCard/doctorCard.js:3` imports `recalculate` from `DoctorCardController`, but in `force-app/main/default/classes/DoctorCardController.cls:7` the method is declared as plain `public static Integer recalculate(Id recordId)` without `@AuraEnabled`. The deployment of `doctorCard` fails with "Unable to find Apex action method referenced as `DoctorCardController.recalculate`". Fix: add `@AuraEnabled` to `recalculate` (the included test, `DoctorCardControllerTest.cls`, calls `recalculate` directly as Apex, so it will not catch this).

1. **Missing dependencies named after this feature** — three imports in `doctorCard` resolve to nothing in the source, and each is named to match this new feature rather than looking like an established platform reference:
   
   - `force-app/main/default/lwc/doctorCard/doctorCard.html:4` uses `<c-doctor-badge>`, i.e. a `doctorBadge` LWC that is not part of this pull request.
   - `force-app/main/default/lwc/doctorCard/doctorCard.js:4` imports the schema for `Account.Doctor_Score__c`, a field not defined in this change.
   - `force-app/main/default/lwc/doctorCard/doctorCard.js:5` imports the custom label `Doctor_Card_Title`, which is not defined in this change.

All three look like forgotten files for this feature rather than long-standing org artifacts. If `doctorBadge`, `Account.Doctor_Score__c`, and `Doctor_Card_Title` are not already present in the target org, the deployment fails on each missing reference.

### Check before merging

- Does the `doctorBadge` Lightning web component already exist in the target org?
- Does the `Account.Doctor_Score__c` field already exist in the target org, with field-level access for the users of this page?
- Does the custom label `Doctor_Card_Title` already exist in the target org?
