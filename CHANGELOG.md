# Changelog

Each release is a tag. The workflows of a release check out the context builders at the same tag, so an installed workflow keeps running the scripts it was written for until you update it.

## 0.4.0

Checked against public repositories. 0.3.0 was built on the failed runs of one pipeline. Run over 118 failed Salesforce steps from 37 public repositories, it missed formats that pipeline never used.

- **Apex tests in the human-readable table** of `sf apex run test` are parsed, with messages and stack traces.
- **CLI errors** that continue over several lines, and the JSON envelope the CLI prints when a command fails (for example a package installation).
- A missing org alias is no longer reported as an authentication problem: it is usually a follow-up of an earlier failed step.
- `Status:` is only read from a deployment (a Docker pull prints the same word).
- A run whose jobs cannot be listed, or whose log has expired, is reported as such instead of ending in a stack trace.
- Twelve catalog patterns from messages in those runs, among them a CLI that does not start on the runner's Node.js version, seen in four repositories.

## 0.3.0

Diagnosis on real pipelines. The first versions were only tried on pipelines built for the test. Run over thirty failed runs of a real one, they explained none.

- **Several deployments in one job:** every result in the log counts, not the first one. A job is no longer taken for successful because its first deployment was.
- **Apex tests as a step of their own:** the result of `sf apex run test --result-format json` is read.
- **Deployments that fail as a whole:** the `errorMessage` of a result (a locked record, a timeout) and errors listed only per file are read.
- **Failed steps the doctor does not understand:** the context keeps an excerpt of every failed step (`failedSteps`), and the agent diagnoses from it instead of reporting nothing.
- Six catalog patterns from real messages: missing folders and email templates, disabled template folders, a record lock during deployment, field integrity errors in tests, a test run that exceeds its wait time.
- The prompt tells the agent to explain every failed step, to treat many identical test failures as one cause and to say when a failure is intermittent.

## 0.2.0

Installation for everyone.

- **One command:** `gh aw add rammc/sf-deploy-doctor@v0.2.0` installs both workflows and a setup script through a package manifest (`aw.yml`).
- **Setup script:** finds the workflows that deploy to Salesforce, the branch patterns of the repository and the package directories, and writes them into the two workflow files. No hand editing.
- README: new quick start, engine table, a way to try the doctor on a run that already failed, troubleshooting.
- `CONTRIBUTING.md`, `SECURITY.md` and issue templates for unknown errors and false alarms.

## 0.1.0

First tagged release.

- **Deploy Doctor:** diagnosis of failed deployments and validations from the job log. 35 error patterns in seven categories, including Lightning web components.
- **Pre-flight:** assessment of a pull request before deployment. Signals for missing references, deleted components still in use, field type changes, required and unique fields, validation rules, Apex without a test, active flows, profiles, managed package references, imports of Lightning web components and Aura components, and fields used by UI bundles (React, Angular).
- Both context builders run on Python 3.9+ without dependencies and can be used without the agent.
