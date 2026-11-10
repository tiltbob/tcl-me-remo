#!/usr/bin/env python3
"""Decide whether this commit is to be published as a GitHub release, and write its notes.

The release workflow (.github/workflows/release.yml) calls this, so that what it decides can
be tested without running GitHub Actions.  A release is published

  * for the version in package.json, as tag "v<version>",
  * from main (the workflow gets here once CI has passed for a push to it) or from a pushed
    tag of exactly that name,
  * only if no published release of that tag exists yet.

So a push to main that does not change the version publishes nothing, and nobody has to push
a tag: creating the release creates the tag.  Whether CI passed is not decided here: that
has to be settled before anything of the commit is checked out, in the workflow itself.

    release_plan.py plan --ref refs/heads/main --commit <sha> --releases releases.json --tags tags.txt
    release_plan.py notes --zip out/tcl-me-remo.zip --repo owner/name [--expect-sha256 <hex>]

"plan" prints key=value lines (also appended to the file named by $GITHUB_OUTPUT, if set):
publish, version, tag, prerelease, latest, reason.  It exits 1 for a state somebody has to
look at (a tag that does not match the version, a draft release of the same tag).

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_zip  # noqa: E402 - the neighbouring script: the archive's name and the version checks

ROOT = build_zip.ROOT
# The release asset.  Decky takes the last part of the URL a plugin is installed from, without
# ".zip", for the plugin's name (see build_zip.py), so this must be "<plugin.json name>.zip".
ASSET_NAME = f"{build_zip.FOLDER}.zip"
MAIN_REF = "refs/heads/main"
TAG_PREFIX = "refs/tags/"
# MAJOR.MINOR.PATCH, optionally "-something" for a pre-release.  Also what keeps anything odd
# out of the tag name the workflow hands to a shell.
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z][0-9A-Za-z.-]*)?")
SHA256 = re.compile(r"[0-9A-Fa-f]{64}")


class PlanError(Exception):
    """Nothing can be decided: the message says what has to be put right."""


@dataclass(frozen=True)
class Plan:
    publish: bool
    version: str
    tag: str
    prerelease: bool
    latest: bool
    reason: str
    # True for a skip that somebody may have to follow up; the run shows it as a warning.
    warn: bool = False

    def lines(self) -> list[str]:
        def flag(value: bool) -> str:
            return "true" if value else "false"

        return [
            f"publish={flag(self.publish)}",
            f"version={self.version}",
            f"tag={self.tag}",
            f"prerelease={flag(self.prerelease)}",
            f"latest={flag(self.latest)}",
            f"reason={self.reason}",
        ]


def download_url(repo: str) -> str:
    """The address that always serves the newest release's archive (GitHub: "latest" skips
    drafts and pre-releases)."""
    return f"https://github.com/{repo}/releases/latest/download/{ASSET_NAME}"


def version_key(text: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) of "1.2.3", "v1.2.3" or "1.2.3-rc.1"; None for anything else."""
    match = VERSION.fullmatch(text.removeprefix("v"))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def parse_releases(text: str) -> list[dict[str, Any]]:
    """The releases in what "gh api --paginate repos/OWNER/REPO/releases" printed.

    That is one JSON array per page, one after the other.  Anything else is an error: an
    answer that cannot be read must not be taken for "no release yet".
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    position = 0
    text = text.strip()
    if not text:
        raise PlanError("the list of releases is empty (not even '[]'): the request for it failed")
    while position < len(text):
        try:
            page, end = decoder.raw_decode(text, position)
        except ValueError as exc:
            raise PlanError(f"the list of releases is not JSON: {exc}") from None
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise PlanError("the list of releases is not a list of releases (an error message from the API?)")
        found.extend(page)
        position = end
        while position < len(text) and text[position].isspace():
            position += 1
    return found


def parse_tag_commit(text: str, tag: str) -> str:
    """The commit a tag points at, from "git ls-remote origin refs/tags/T 'refs/tags/T^{}'".

    "" if the tag does not exist.  An annotated tag is listed twice, and the line ending in
    "^{}" names the commit.
    """
    direct = peeled = ""
    for line in text.splitlines():
        sha, _, name = line.strip().partition("\t")
        if name == f"{TAG_PREFIX}{tag}^{{}}":
            peeled = sha
        elif name == f"{TAG_PREFIX}{tag}":
            direct = sha
    return peeled or direct


def decide(ref: str, version: str, commit: str, releases: list[dict[str, Any]], tag_commit: str = "") -> Plan:
    """What to do for a run on ``ref`` at ``commit``, with ``version`` in package.json there."""
    if not VERSION.fullmatch(version):
        raise PlanError(f"package.json version {version!r} is not MAJOR.MINOR.PATCH (with an optional -suffix)")
    tag = f"v{version}"
    prerelease = "-" in version

    def skip(reason: str, warn: bool = False) -> Plan:
        return Plan(False, version, tag, prerelease, False, reason, warn)

    if ref.startswith(TAG_PREFIX):
        pushed = ref[len(TAG_PREFIX) :]
        if pushed != tag:
            raise PlanError(f"tag {pushed} does not match the version in package.json ({version}): expected {tag}")
    elif ref != MAIN_REF:
        return skip(f"{ref} is neither main nor a version tag; only those publish")

    same_tag = [release for release in releases if release.get("tag_name") == tag]
    if any(not release.get("draft") for release in same_tag):
        return skip(f"release {tag} exists already; nothing to publish until the version changes")
    if same_tag:
        raise PlanError(f"a draft release for {tag} exists: publish it or delete it, then run this again")
    if tag_commit and tag_commit != commit:
        # The tag names another commit.  What is released as v<version> must be built from
        # the commit the tag names, and that is a run on the tag itself.
        # A tag without a release is usually what is left of a release somebody deleted: the
        # tag stays, and since the workflow's own token made it, no run for it ever existed.
        return skip(
            f"tag {tag} exists at {tag_commit[:12]}, not at this commit ({commit[:12]}), and has no release;"
            " to publish, start this workflow by hand on that tag, delete the tag, or raise the version",
            warn=True,
        )

    # Decided here and passed on explicitly: GitHub would otherwise mark every new release
    # as the latest one, also one for an older version.
    newest = max(
        (
            key
            for release in releases
            if not release.get("draft") and not release.get("prerelease")
            for key in [version_key(str(release.get("tag_name", "")))]
            if key is not None
        ),
        default=None,
    )
    own = version_key(version)
    latest = not prerelease and (newest is None or (own is not None and own > newest))
    if prerelease:
        kind = "a pre-release"
    else:
        kind = "the latest release" if latest else "a release (an older version: not the latest)"
    return Plan(True, version, tag, prerelease, latest, f"publishing {tag} as {kind}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def notes(zip_path: Path, repo: str, version: str, expect_sha256: str | None = None) -> str:
    """The text put in front of GitHub's generated release notes.

    ``expect_sha256`` is the checksum of the archive that passed the checks; None for "there
    is nothing to compare with".  Anything else that is not a SHA-256 (an empty text is what
    a workflow hands over when the step that was to compute it did not) is an error, not
    "nothing to compare with".
    """
    if zip_path.name != ASSET_NAME:
        raise PlanError(f"the archive must be called {ASSET_NAME}, not {zip_path.name}: Decky goes by the name")
    if expect_sha256 is not None and not SHA256.fullmatch(expect_sha256.strip()):
        raise PlanError(
            f"the SHA-256 of the archive that passed the checks was not handed over ({expect_sha256!r} is not one):"
            " without it this archive cannot be compared, so nothing is published"
        )
    try:
        digest = sha256_of(zip_path)
    except OSError as exc:
        raise PlanError(f"cannot read {zip_path}: {exc}") from None
    if expect_sha256 is not None and digest != expect_sha256.strip().lower():
        raise PlanError(
            f"{zip_path} has SHA-256 {digest}, but the archive that passed the checks had {expect_sha256}:"
            " the two builds differ, so nothing is published"
        )
    address = download_url(repo).removeprefix("https://")
    return "\n".join(
        [
            f"tcl-me-remo {version}, a Decky Loader plugin.",
            "",
            "**Install or update** in Game Mode: Decky settings > General > *Developer mode*, then"
            " *Developer* > *Install Plugin from URL*. Type this address exactly and choose *Install*:",
            "",
            "```",
            address,
            "```",
            "",
            "It always fetches the newest release, so the same address updates an installed plugin."
            " Settings and the pairing with the TV are kept.",
            "",
            f"SHA-256 of `{ASSET_NAME}`: `{digest}`",
            "",
        ]
    )


def _write_outputs(lines: list[str]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="decide whether to publish; prints key=value lines")
    p.add_argument("--ref", required=True, help="the ref that was pushed (refs/heads/main, refs/tags/v1.2.3)")
    p.add_argument("--commit", required=True, help="the commit that is checked out")
    p.add_argument("--releases", required=True, type=Path, help="file with the repository's releases (API JSON)")
    p.add_argument("--tags", type=Path, help="file with the output of git ls-remote for the version's tag")
    p = sub.add_parser("notes", help="print the release notes, with the archive's SHA-256")
    p.add_argument("--zip", required=True, type=Path, dest="zip_path")
    p.add_argument("--repo", required=True, help="OWNER/NAME on GitHub")
    p.add_argument("--expect-sha256", default=None, help="fail unless the archive has this SHA-256")
    p = sub.add_parser("sha256", help="print the SHA-256 of a file")
    p.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "sha256":
            print(sha256_of(args.path))
            return 0
        # Refuses a checkout whose package.json, plugin.json and service disagree.
        version = build_zip.plugin_version(root)
        if args.command == "notes":
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
                raise PlanError(f"{args.repo!r} is not OWNER/NAME")
            sys.stdout.write(notes(args.zip_path, args.repo, version, args.expect_sha256))
            return 0
        releases = parse_releases(args.releases.read_text(encoding="utf-8"))
        tag_commit = parse_tag_commit(args.tags.read_text(encoding="utf-8"), f"v{version}") if args.tags else ""
        plan = decide(args.ref, version, args.commit, releases, tag_commit)
    except (PlanError, build_zip.BuildError, OSError) as exc:
        # "::error::" puts the line on the summary page of a GitHub Actions run.  On stderr,
        # because the notes go to stdout.
        print(f"::error::release_plan: {exc}", file=sys.stderr)
        return 1
    lines = plan.lines()
    _write_outputs(lines)
    print("\n".join(lines))
    print(f"::{'warning' if plan.warn else 'notice'}::{plan.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
