# Changelog

Each release is a tag. The workflows of a release check out the context builders at the same tag, so an installed workflow keeps running the scripts it was written for until you update it.

## 0.6.0

Two more points from an outside review, both confirmed in the code.

- **The target org is part of the comparison.** The base of a push or a manual run was the last successful run of the same workflow on the branch, wherever that run had deployed to. With one workflow for UAT and production, a failed production run was compared with the last UAT run, and `changedInThisRun: false` was wrong for production. The doctor now reads the GitHub environment of each job (new permission `deployments: read`) and only accepts an earlier run that deployed to the same environments. `run.environments` and `changeRange.environments` say what was found. A manual run of a workflow with inputs that names no environment has an unknown range instead of a guessed one.
- **A new missing name stays medium in a project with package dependencies.** Until now every missing reference was low as soon as `sfdx-project.json` listed a dependency, so a typo in a new field name no longer woke the agent. Now the signal is low only if the base branch already uses the name. Over the 470 changes of twelve public repositories the agent runs 83 times instead of 55.
- **Optional configuration file** `.github/deploy-doctor.json`: how to tell the target org when the jobs have no GitHub environment (`doctor.environment`), the threshold of the pre-flight (`preflight.minSeverity`) and names that are deliberately not in the repository (`preflight.knownReferences`). The pre-flight reads it from the base branch, so a pull request cannot silence itself. Nothing changes without the file.
- The pre-flight workflow no longer passes `--min-severity`; the default is still medium. If you edited that line, `gh aw update` shows a conflict there: keep your value or move it to the configuration file.
- Not solved: the key that decides which older report is closed still hangs on the workflow, because gh-aw does not let it carry a value computed at run time. With one workflow for several orgs, set `close-older-issues: false`.

## 0.5.2

- A manually started diagnosis closed the open report of any other manual diagnosis, because all of them shared one key. The key is now the id of the diagnosed run: a report only replaces an older report about the same run. Runs triggered by a failed deployment were not affected.

## 0.5.1

Found in the first live run with two orgs: the context listed a compile error for both jobs, and the agent still reported it for one org only and explained the difference with an assumption about the other org.

- Every finding of a run with several failed jobs now states `inAllFailedJobs` and `notInJobs`, and the prompt tells the agent to copy the job names and forbids conclusions about an org that reported the same error.
- The README no longer calls the shell allowlist a boundary. In that run the engine ran `find` although the list does not contain it; engines run commands they classify as read-only.

## 0.5.0

Deployments beyond the pull request. A review pointed out six weaknesses, all confirmed in the code. Most of them only show on a pipeline that deploys release branches to several orgs, which the earlier versions were never measured on.

- **What a run changed** is no longer guessed from the last commit. A pull request is compared with its base, a push or manual run with the commit of the last successful run of the same workflow on that branch. Without either, `changedInThisRun` is unknown, and the agent is told not to conclude anything about org drift from it. The context says what it compared in `changeRange`.
- **Several failed jobs**, for example one per org, are read one by one. Every finding lists the jobs it occurred in, every failed step names its job, and the limit of six steps applies per job. Steps left out are counted in `failedStepsOmitted`.
- **An older report is only closed by a newer one about the same deploy workflow** (`close-older-key`), not by any report.
- **Pre-flight threshold:** the agent runs from medium signals up (`--min-severity`). A missing reference that the base branch already uses is low, and so are Apex without a test that had none before the change and a change to a flow that was already active.
- **Fewer false alarms in the pre-flight**, found by running it over the history of twelve public repositories: a deleted component that is only named in a changelog, a type declaration or a configuration file is no longer "still referenced"; a missing reference in a project that depends on other packages (`sfdx-project.json`) is low, because unlocked packages have no namespace and their components look local; custom labels in `.labels` files are read. Over 470 changes the agent would have run 197 times before and runs 55 times now.
- **Record ids** are recognised by their shape instead of a short list of prefixes. Case and task ids were not removed before.
- The catalog no longer takes `Maintenance_Plan__c` or any other name with "maintenance" in it for a platform outage.
- `find` and `sed` are gone from the agent's shell commands. The README now says what read-only covers and that source code and log excerpts are sent to the provider of the AI engine.

Updating: `changedInThisRun` used to be `false` where it is now `null` for runs without a base. If you changed `close-older-issues` or the `bash:` list in your copy, `gh aw update` may ask you to resolve a conflict there.

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
