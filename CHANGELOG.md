# Changelog

Each release is a tag. The workflows of a release check out the context builders at the same tag, so an installed workflow keeps running the scripts it was written for until you update it.

## 0.1.0

First tagged release.

- **Deploy Doctor:** diagnosis of failed deployments and validations from the job log. 35 error patterns in seven categories, including Lightning web components.
- **Pre-flight:** assessment of a pull request before deployment. Signals for missing references, deleted components still in use, field type changes, required and unique fields, validation rules, Apex without a test, active flows, profiles, managed package references, imports of Lightning web components and Aura components, and fields used by UI bundles (React, Angular).
- Both context builders run on Python 3.9+ without dependencies and can be used without the agent.
