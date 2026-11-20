#!/usr/bin/env python3
"""Prepare a release: write the new version to VERSION and to the workflows.

The workflows check out the context builders at the tag of their own release,
so a workflow and the scripts it runs always belong together.

Usage: scripts/bump_version.py 0.2.0
Then: add the changes to CHANGELOG.md, open a pull request, merge it, and push
the tag v0.2.0. The release workflow does the rest.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        print(__doc__)
        return 2
    version = sys.argv[1]
    (ROOT / "VERSION").write_text(version + "\n")
    for path in sorted((ROOT / "workflows").glob("*.md")):
        text = path.read_text()
        new = re.sub(r"(repository: rammc/sf-deploy-doctor\n\s+ref: )\S+", rf"\g<1>v{version}", text)
        path.write_text(new)
        print(f"{path.relative_to(ROOT)}: {len(re.findall(rf'ref: v{re.escape(version)}', new))} reference(s)")
    readme = ROOT / "README.md"
    readme.write_text(re.sub(r"(rammc/sf-deploy-doctor(?:/[\w-]+)?@)v\d+\.\d+\.\d+", rf"\g<1>v{version}",
                             readme.read_text()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
