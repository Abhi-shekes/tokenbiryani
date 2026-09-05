#!/usr/bin/env python3
"""Read CHANGELOG.md, so the release notes and the changelog cannot disagree.

A release has three places that state what changed — the git tag, the GitHub
release body, and CHANGELOG.md — and any hand-copying between them eventually
produces a release whose notes describe a different version. So the changelog is
the single source, this script is the only reader, and the release workflow quotes
it rather than restating it.

    python scripts/changelog.py extract 0.1.0     # that section's body, for notes
    python scripts/changelog.py check 0.1.0       # exit 1 if it is missing or empty
    python scripts/changelog.py unreleased        # what is queued for the next one

`check` is what stands between a tag and a release: the tag job refuses to publish
a version the changelog has never heard of, which is the failure mode that produces
an empty GitHub release at 2am.

The parser is deliberately dumb — it finds `## [x.y.z]` headings and slices between
them. Keep a Changelog is a convention about headings, and anything cleverer would
start having opinions about the prose underneath.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

#: `## [1.2.3] - 2026-09-05`, or `## [Unreleased]`. The date is optional because an
#: unreleased section has not got one yet, and required nowhere else.
HEADING = re.compile(r"^##\s+\[(?P<name>[^\]]+)\]\s*(?:-\s*(?P<date>\S+))?\s*$")

CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def sections(text: str) -> List[Tuple[str, Optional[str], str]]:
    """Every `## [name]` section, in file order, as (name, date, body)."""
    found: List[Tuple[str, Optional[str], str]] = []
    name: Optional[str] = None
    date: Optional[str] = None
    body: List[str] = []

    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            if name is not None:
                found.append((name, date, "\n".join(body).strip()))
            name, date, body = match.group("name"), match.group("date"), []
        elif name is not None:
            body.append(line)

    if name is not None:
        found.append((name, date, "\n".join(body).strip()))
    return found


def by_name(text: str) -> Dict[str, Tuple[Optional[str], str]]:
    return {name: (date, body) for name, date, body in sections(text)}


def normalise(version: str) -> str:
    """`v0.1.0` and `0.1.0` are the same release; tags carry the v, headings do not."""
    return version[1:] if version.startswith("v") else version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="print one section's body")
    extract.add_argument("version")

    check = sub.add_parser("check", help="fail unless that section exists and says something")
    check.add_argument("version")

    sub.add_parser("unreleased", help="print the queued section, empty if there is none")

    args = parser.parse_args()
    if not CHANGELOG.exists():
        print(f"no changelog at {CHANGELOG}", file=sys.stderr)
        return 1

    found = by_name(CHANGELOG.read_text())

    if args.command == "unreleased":
        print(found.get("Unreleased", (None, ""))[1])
        return 0

    version = normalise(args.version)
    if version not in found:
        known = ", ".join(name for name in found if name != "Unreleased") or "none"
        print(
            f"CHANGELOG.md has no '## [{version}]' section. Released versions: {known}.\n"
            f"Add one — or rename [Unreleased] to [{version}] and date it — before tagging.",
            file=sys.stderr,
        )
        return 1

    body = found[version][1]
    if args.command == "check":
        if not body:
            print(f"'## [{version}]' exists but is empty.", file=sys.stderr)
            return 1
        date = found[version][0]
        if not date:
            print(
                f"'## [{version}]' has no date. Use '## [{version}] - YYYY-MM-DD'.",
                file=sys.stderr,
            )
            return 1
        print(f"CHANGELOG.md describes {version}, dated {date}.")
        return 0

    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
