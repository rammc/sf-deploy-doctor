# Security

## What the workflows can do

- They have no credentials for Salesforce and never call an org.
- They run with read permissions in your repository. Their only outputs are one comment or one issue per run, enforced by gh-aw safe outputs. The agent can write files on the runner, which is discarded after the job; it cannot write to the repository.
- Log output and file contents are passed to the agent as data. Usernames, e-mail addresses and record ids are removed from the context before the agent sees it. Record ids are recognised by their shape, which can miss an unusual one.
- The agent runs at the provider of the AI engine you configured. The context file and the source files the agent opens are sent there. Nothing is sent to the maintainers of this project.
- The context builders use the Python standard library only and make no network requests other than reading the job log of the failed run through the GitHub CLI.
- The setup script reads `.github/workflows` and `sfdx-project.json`, changes two workflow files and makes no network requests.

Installed workflows check out the context builders at the tag of their release. A tag is not moved after it has been published.

## Reporting a vulnerability

Please do not open a public issue. Use [private vulnerability reporting](https://github.com/rammc/sf-deploy-doctor/security/advisories/new) on GitHub and describe what you found and how to reproduce it.

Things worth reporting: a way to make the agent act on text from a log or a source file, a way to leak a secret, usernames or record ids into a report, or a way to make the workflows write to a repository or reach an org.
