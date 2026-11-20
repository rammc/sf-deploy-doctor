# Contributing

Thank you for helping. The most useful contributions are small and come with evidence from a real deployment.

## An error the doctor does not know

1. Add one entry to `doctor/catalog.json`: `id`, `category`, `pattern` (a case-insensitive regular expression), `refs` (capture groups that hold a component name), `title`, `advice`.
2. Add the message to `test_known_messages` in `tests/test_deploy_context.py`, exactly as the org returned it. Remove usernames, org ids and record ids.
3. Open a pull request and say where the message came from (which command, which kind of metadata).

The catalog only holds messages an org actually returned. Please do not add patterns from memory.

## A false alarm of the pre-flight

Open an issue with the smallest diff that triggers it: the changed lines and the files they refer to. A fix always comes with a test in `tests/test_preflight_context.py` that fails before and passes after.

## Working on the code

```bash
python3 -m unittest discover -s tests     # all tests, no dependencies
```

- The context builders and the setup script run on Python 3.9+ with the standard library only. Please keep it that way; it is why the workflows need no install step.
- Facts are collected by code, judgement is left to the agent. A rule belongs in a context builder when it can be decided from the log or the diff alone. Everything that needs weighing belongs in the prompt.
- The workflows stay read-only. No change may give the agent org access, write access to the repository or the ability to re-run or approve anything.
- After changing a workflow under `workflows/`, compile it in a test repository and let it run once before you open the pull request.

## Releases

`scripts/bump_version.py X.Y.Z` writes the version to `VERSION`, the workflows and the README. Add a section to `CHANGELOG.md`, merge, then push the tag `vX.Y.Z`. The release workflow publishes it.
