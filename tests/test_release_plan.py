"""What the release workflow decides (scripts/release_plan.py), and the names it must not change.

GitHub Actions cannot be run here, so everything in the workflow that is a decision lives in
that script and is tested as plain functions.
"""

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
MAIN = "refs/heads/main"
COMMIT = "1111111111111111111111111111111111111111"
OTHER = "2222222222222222222222222222222222222222"


@pytest.fixture(scope="module")
def plan():
    spec = importlib.util.spec_from_file_location("release_plan", REPO / "scripts" / "release_plan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_plan"] = module  # a dataclass looks its own module up while it is defined
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("release_plan", None)


def release(tag, *, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


# --------------------------------------------------------------------------- the decision


def test_first_push_to_main_publishes_the_version(plan):
    decided = plan.decide(MAIN, "0.1.0", COMMIT, [])
    assert decided.publish and decided.tag == "v0.1.0" and decided.version == "0.1.0"
    assert decided.latest and not decided.prerelease


def test_a_push_that_leaves_the_version_alone_publishes_nothing(plan):
    decided = plan.decide(MAIN, "0.1.0", COMMIT, [release("v0.1.0")])
    assert not decided.publish
    assert "v0.1.0 exists already" in decided.reason


def test_a_new_version_on_main_publishes_again(plan):
    decided = plan.decide(MAIN, "0.2.0", COMMIT, [release("v0.1.0")])
    assert decided.publish and decided.tag == "v0.2.0" and decided.latest


def test_a_pushed_tag_publishes_when_it_names_the_version(plan):
    decided = plan.decide("refs/tags/v0.1.0", "0.1.0", COMMIT, [], COMMIT)
    assert decided.publish and decided.tag == "v0.1.0"


def test_a_pushed_tag_that_does_not_name_the_version_is_an_error(plan):
    with pytest.raises(plan.PlanError, match="tag v0.2.0 does not match the version in package.json \\(0.1.0\\)"):
        plan.decide("refs/tags/v0.2.0", "0.1.0", COMMIT, [])


def test_tag_and_main_cannot_both_publish(plan):
    # The two runs are queued one behind the other (the workflow's concurrency group); the
    # second one finds the first one's release, whichever of them it is.
    first = plan.decide(MAIN, "0.1.0", COMMIT, [], COMMIT)
    assert first.publish
    after = [release(first.tag)]
    assert not plan.decide("refs/tags/v0.1.0", "0.1.0", COMMIT, after, COMMIT).publish
    assert not plan.decide(MAIN, "0.1.0", COMMIT, after, COMMIT).publish


def test_main_uses_a_tag_somebody_pushed_to_the_same_commit(plan):
    assert plan.decide(MAIN, "0.1.0", COMMIT, [], COMMIT).publish


def test_main_does_not_publish_under_a_tag_that_names_another_commit(plan):
    # Publishing here would attach an archive built from this commit to a tag that names another.
    decided = plan.decide(MAIN, "0.1.0", COMMIT, [], OTHER)
    assert not decided.publish and decided.warn
    assert "tag v0.1.0 exists at 222222222222" in decided.reason
    # Such a tag is mostly what a deleted release left behind, and then no run for it exists
    # that could be run again: the message names what does work.
    assert "start this workflow by hand on that tag, delete the tag, or raise the version" in decided.reason
    assert "run it again" not in decided.reason
    # ... the first of which: a run on the tag, at the commit the tag names.
    assert plan.decide("refs/tags/v0.1.0", "0.1.0", OTHER, [], OTHER).publish


def test_a_draft_of_the_same_tag_needs_a_person(plan):
    with pytest.raises(plan.PlanError, match="a draft release for v0.1.0 exists"):
        plan.decide(MAIN, "0.1.0", COMMIT, [release("v0.1.0", draft=True)])
    # ... but a published one beside it settles the matter.
    assert not plan.decide(MAIN, "0.1.0", COMMIT, [release("v0.1.0", draft=True), release("v0.1.0")]).publish


def test_other_branches_publish_nothing(plan):
    decided = plan.decide("refs/heads/some-branch", "0.1.0", COMMIT, [])
    assert not decided.publish and "only those publish" in decided.reason


def test_a_suffixed_version_is_a_prerelease_and_never_the_latest(plan):
    # "latest" is what the address people install from serves; GitHub skips pre-releases there.
    decided = plan.decide(MAIN, "0.2.0-rc.1", COMMIT, [release("v0.1.0")])
    assert decided.publish and decided.prerelease and not decided.latest
    assert decided.tag == "v0.2.0-rc.1"


def test_an_older_version_does_not_become_the_latest(plan):
    decided = plan.decide("refs/tags/v0.1.1", "0.1.1", COMMIT, [release("v0.2.0"), release("v0.1.0")], COMMIT)
    assert decided.publish and not decided.latest


def test_prereleases_drafts_and_foreign_tags_do_not_count_as_newer(plan):
    existing = [release("v0.9.0-rc.1", prerelease=True), release("v0.8.0", draft=True), release("nightly")]
    assert plan.decide(MAIN, "0.2.0", COMMIT, existing).latest


NOT_VERSIONS = ["", "1.2", "1.2.3.4", "v1.2.3", "1.2.3 ", "1.2.3-", "1.2.3;rm", "1.2.3-a b", "$(id)"]


@pytest.mark.parametrize("version", NOT_VERSIONS)
def test_a_version_that_is_not_a_version_is_refused(plan, version):
    with pytest.raises(plan.PlanError, match="is not MAJOR.MINOR.PATCH"):
        plan.decide(MAIN, version, COMMIT, [])


def test_outputs_are_single_lines(plan):
    for decided in (
        plan.decide(MAIN, "0.1.0", COMMIT, []),
        plan.decide(MAIN, "0.1.0", COMMIT, [], OTHER),
        plan.decide(MAIN, "0.1.0", COMMIT, [release("v0.1.0")]),
    ):
        lines = decided.lines()
        keys = [line.split("=", 1)[0] for line in lines]
        assert keys == ["publish", "version", "tag", "prerelease", "latest", "reason"]
        assert all("\n" not in line and "\r" not in line for line in lines)
        assert lines[0] in ("publish=true", "publish=false")


# --------------------------------------------------------------------------- reading GitHub's answers


def test_release_list_of_several_pages(plan):
    # "gh api --paginate" prints one array per page, back to back.
    text = json.dumps([release("v0.2.0")]) + json.dumps([release("v0.1.0")]) + "\n" + json.dumps([])
    assert [item["tag_name"] for item in plan.parse_releases(text)] == ["v0.2.0", "v0.1.0"]
    assert plan.parse_releases("[]\n") == []


@pytest.mark.parametrize("text", ["", "  \n", '{"message": "Not Found"}', "[1, 2]", "[] trailing", "<html>"])
def test_an_unreadable_release_list_is_not_taken_for_no_releases(plan, text):
    with pytest.raises(plan.PlanError, match="list of releases"):
        plan.parse_releases(text)


def test_tag_commit_from_ls_remote(plan):
    listing = (
        f"{'a' * 40}\trefs/tags/v0.1.0\n"
        f"{'b' * 40}\trefs/tags/v0.1.00\n"
        f"{'c' * 40}\trefs/tags/v0.2.0\n"  # an annotated tag: the tag object ...
        f"{'d' * 40}\trefs/tags/v0.2.0^{{}}\n"  # ... and the commit it names
    )
    assert plan.parse_tag_commit(listing, "v0.1.0") == "a" * 40
    assert plan.parse_tag_commit(listing, "v0.2.0") == "d" * 40
    assert plan.parse_tag_commit(listing, "v0.3.0") == ""
    assert plan.parse_tag_commit("", "v0.1.0") == ""


# --------------------------------------------------------------------------- the notes


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "tcl-me-remo.zip"
    path.write_bytes(b"not really a zip")
    return path


def test_notes_carry_the_checksum_and_the_address(plan, archive):
    digest = hashlib.sha256(b"not really a zip").hexdigest()
    text = plan.notes(archive, "someone/tcl-me-remo", "0.1.0", expect_sha256=digest)
    assert f"SHA-256 of `tcl-me-remo.zip`: `{digest}`" in text
    assert "\ngithub.com/someone/tcl-me-remo/releases/latest/download/tcl-me-remo.zip\n" in text
    assert "Install Plugin from URL" in text and "tcl-me-remo 0.1.0" in text


def test_notes_refuse_an_archive_that_is_not_the_checked_one(plan, archive):
    with pytest.raises(plan.PlanError, match="the two builds differ"):
        plan.notes(archive, "someone/tcl-me-remo", "0.1.0", expect_sha256="0" * 64)


@pytest.mark.parametrize("handed_over", ["", " ", "deadbeef", "0" * 63, "0" * 65, "g" * 64])
def test_notes_refuse_a_checksum_that_is_none(plan, archive, handed_over):
    # An empty value is what the publish job receives when the step that computes the
    # checksum failed without failing, or was renamed: that must not read as "nothing to compare".
    with pytest.raises(plan.PlanError, match="was not handed over"):
        plan.notes(archive, "someone/tcl-me-remo", "0.1.0", expect_sha256=handed_over)


def test_notes_compare_whatever_the_case_of_the_checksum(plan, archive):
    digest = hashlib.sha256(b"not really a zip").hexdigest()
    assert digest in plan.notes(archive, "someone/tcl-me-remo", "0.1.0", expect_sha256=f" {digest.upper()}\n")
    assert digest in plan.notes(archive, "someone/tcl-me-remo", "0.1.0")  # no comparison asked for


def test_notes_refuse_another_file_name(plan, tmp_path):
    other = tmp_path / "tcl-me-remo-v0.1.0.zip"
    other.write_bytes(b"x")
    with pytest.raises(plan.PlanError, match="must be called tcl-me-remo.zip"):
        plan.notes(other, "someone/tcl-me-remo", "0.1.0")


# --------------------------------------------------------------------------- the command


def run_plan(plan, tmp_path, monkeypatch, capsys, *, ref=MAIN, releases="[]", tags=""):
    (tmp_path / "releases.json").write_text(releases, encoding="utf-8")
    (tmp_path / "tags.txt").write_text(tags, encoding="utf-8")
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    argv = ["plan", "--ref", ref, "--commit", COMMIT]
    argv += ["--releases", str(tmp_path / "releases.json"), "--tags", str(tmp_path / "tags.txt")]
    code = plan.main(argv)
    written = output.read_text(encoding="utf-8") if output.exists() else ""
    return code, dict(line.split("=", 1) for line in written.splitlines()), capsys.readouterr()


def test_command_publishes_the_checkouts_version(plan, tmp_path, monkeypatch, capsys):
    version = json.loads((REPO / "package.json").read_text(encoding="utf-8"))["version"]
    code, outputs, printed = run_plan(plan, tmp_path, monkeypatch, capsys)
    assert code == 0
    assert outputs["publish"] == "true" and outputs["tag"] == f"v{version}" and outputs["version"] == version
    assert f"::notice::publishing v{version}" in printed.out

    code, outputs, _ = run_plan(plan, tmp_path, monkeypatch, capsys, releases=json.dumps([release(f"v{version}")]))
    assert code == 0 and outputs["publish"] == "false"

    code, outputs, printed = run_plan(plan, tmp_path, monkeypatch, capsys, tags=f"{OTHER}\trefs/tags/v{version}\n")
    assert code == 0 and outputs["publish"] == "false"
    assert f"::warning::tag v{version} exists at" in printed.out

    code, outputs, _ = run_plan(plan, tmp_path, monkeypatch, capsys, tags=f"{COMMIT}\trefs/tags/v{version}\n")
    assert code == 0 and outputs["publish"] == "true"


def test_command_fails_loudly_and_writes_no_outputs(plan, tmp_path, monkeypatch, capsys):
    code, outputs, printed = run_plan(plan, tmp_path, monkeypatch, capsys, ref="refs/tags/v99.0.0")
    assert code == 1 and outputs == {}
    assert "::error::release_plan: tag v99.0.0 does not match" in printed.err
    assert printed.out == ""

    code, outputs, printed = run_plan(plan, tmp_path, monkeypatch, capsys, releases='{"message": "Bad credentials"}')
    assert code == 1 and outputs == {}
    assert "list of releases" in printed.err


def test_command_writes_notes_for_the_built_archive(plan, tmp_path, capsys):
    import zipfile

    spec = importlib.util.spec_from_file_location("build_zip_for_notes", REPO / "scripts" / "build_zip.py")
    build_zip = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_zip)
    target = build_zip.build(REPO, tmp_path / "out")
    assert zipfile.is_zipfile(target)
    capsys.readouterr()

    assert plan.main(["sha256", str(target)]) == 0
    digest = capsys.readouterr().out.strip()
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()

    assert plan.main(["notes", "--zip", str(target), "--repo", "tiltbob/tcl-me-remo", "--expect-sha256", digest]) == 0
    assert digest in capsys.readouterr().out
    assert plan.main(["notes", "--zip", str(target), "--repo", "not a repo", "--expect-sha256", digest]) == 1
    capsys.readouterr()
    # The option given without a checksum: an error, and no notes.
    assert plan.main(["notes", "--zip", str(target), "--repo", "tiltbob/tcl-me-remo", "--expect-sha256", ""]) == 1
    printed = capsys.readouterr()
    assert printed.out == "" and "was not handed over" in printed.err
    assert plan.main(["notes", "--zip", str(target), "--repo", "tiltbob/tcl-me-remo"]) == 0


# --------------------------------------------------------------------------- names that belong together
#
# Decky takes the last part of the address a plugin is installed from, without ".zip", for the
# plugin's name, and only treats the install as an update of the installed plugin when that
# equals the "name" in plugin.json.  So the archive, the release asset, the address in the
# README and the one the panel shows all have to end in "<name>.zip".  Rename one, rename all.

ADDRESS = re.compile(r"github\.com/[\w.-]+/[\w.-]+/releases/latest/download/([^\s`\"'<>)]+)")


def plugin_name():
    return json.loads((REPO / "plugin.json").read_text(encoding="utf-8"))["name"]


def test_asset_name_is_the_plugin_name(plan):
    assert plan.ASSET_NAME == f"{plugin_name()}.zip"
    assert plan.download_url("o/r").rsplit("/", 1)[1] == f"{plugin_name()}.zip"
    assert plan.download_url("o/r") == f"https://github.com/o/r/releases/latest/download/{plugin_name()}.zip"


def test_the_workflow_publishes_exactly_that_asset():
    text = WORKFLOW.read_text(encoding="utf-8")
    archive = f"out/{plugin_name()}.zip"
    # Every archive the workflow names is that one, and it is what "gh release create" uploads.
    named = re.findall(r"[\w-][\w./-]*\.zip", text)
    assert {name.rsplit("/", 1)[-1] for name in named} == {f"{plugin_name()}.zip"}
    assert archive in named
    assert re.search(rf'gh release create "\$TAG" {re.escape(archive)} ', text)
    # Publishing needs nothing but the workflow's own token, and only one job may write.
    assert "secrets." not in text
    assert text.count("contents: write") == 1 and "permissions: {}" in text
    # One queue for every run, so that a pushed tag and a push to main cannot publish at once.
    assert re.search(r"^concurrency:\n  group: release\n  queue: max\n", text, re.MULTILINE)
    assert "cancel-in-progress" not in text
    # The checksum the publish job compares with is computed in a command of its own: inside
    # "echo $(...)" a failure would not fail the step, and an empty value would be handed on.
    assert '$(python3 scripts/release_plan.py sha256' in text
    assert not re.search(r"echo [^\n]*\$\(python3", text)
    assert '--expect-sha256 "$CHECKED_SHA256"' in text


def code_of(text):
    """A workflow without its comments (none of the values here contains a "#")."""
    lines = [line.split("#", 1)[0].rstrip() for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines) + "\n"


def test_the_workflow_publishes_from_main_only_after_ci_has_passed():
    text = code_of(WORKFLOW.read_text(encoding="utf-8"))
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_name = re.search(r"^name: (.+)$", ci, re.MULTILINE).group(1).strip()
    # It follows the CI workflow, which is found by its name: rename one, rename both.
    trigger = re.search(r"^on:\n((?:  .*\n|\n)+)", text, re.MULTILINE).group(1)
    assert f"  workflow_run:\n    workflows: [{ci_name}]\n    types: [completed]\n    branches: [main]\n" in trigger
    # ... and CI runs for a push to main, or there would be nothing to follow.
    assert re.search(r"^on:\n  push:\n    branches: \[main\]\n", ci, re.MULTILINE)
    # A push to main alone publishes nothing; tags and a manual run remain.
    assert re.search(r"^  push:\n((?:    .*\n)+)", trigger, re.MULTILINE).group(1) == '    tags: ["v*"]\n'
    assert "  workflow_dispatch:" in trigger
    # Which CI runs may go on is decided before anything is checked out: only a passed one,
    # for a push (not a pull request from a fork's branch called "main") to this repository.
    jobs = text.split("\njobs:\n", 1)[1]
    found = re.search(r"^  (\w+):\n(?:    .*\n)*?    if: >-\n((?:      .*\n)+)", jobs, re.MULTILINE)
    first_job, condition = found.groups()
    assert first_job == "check" and jobs.startswith("  check:")
    condition = " ".join(condition.split())
    for part in (
        "github.event_name != 'workflow_run' ||",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert part in condition, part
    assert "||" not in condition.replace("github.event_name != 'workflow_run' ||", "")  # no other way in
    # The other job publishes only what the first one decided on.
    gate = r"^  publish:\n(?:    .*\n)*?    needs: check\n    if: needs\.check\.outputs\.publish == 'true'\n"
    assert re.search(gate, jobs, re.MULTILINE)
    # Both jobs work on the commit CI passed for, not on whatever main is by then, and the
    # plan is told "main" (GitHub hands a run that follows another the default branch).
    assert text.count("uses: actions/checkout@") == 2
    assert text.count("          ref: ${{ github.event.workflow_run.head_sha }}\n") == 2
    assert "PLAN_REF: ${{ github.event_name == 'workflow_run' && 'refs/heads/main' || github.ref }}" in text
    assert text.count('--ref "$PLAN_REF"') == 2 and "$GITHUB_REF" not in text


def test_the_workflow_publishes_only_in_the_repository_the_address_names():
    # A fork with workflows on would otherwise publish its own releases of an archive whose
    # README and panel point here.
    text = code_of(WORKFLOW.read_text(encoding="utf-8"))
    guard = re.search(r"github\.repository == '([^']+)'", text)
    assert guard, "the release workflow no longer names the repository it publishes in"
    panel = re.search(r'UPDATE_ADDRESS = "([^"]+)"', (REPO / "src" / "text.ts").read_text(encoding="utf-8")).group(1)
    assert panel.startswith(f"github.com/{guard.group(1)}/releases/")


@pytest.mark.parametrize(
    "path",
    ["README.md", "src/text.ts", "dist/index.js", "docs/hardware-testing.md", ".github/workflows/release.yml"],
)
def test_every_address_people_are_given_ends_in_the_plugin_name(path):
    text = (REPO / path).read_text(encoding="utf-8")
    found = ADDRESS.findall(text)
    assert found, f"{path} does not give the address the plugin is installed from"
    assert set(found) == {f"{plugin_name()}.zip"}


def test_no_document_gives_another_address():
    # Wherever the address turns up, it is the same one.
    for path in [REPO / "README.md", REPO / "CLAUDE.md", *sorted((REPO / "docs").glob("*.md"))]:
        for name in ADDRESS.findall(path.read_text(encoding="utf-8")):
            assert name == f"{plugin_name()}.zip", path


def test_the_panel_and_the_readme_give_the_same_address():
    panel = re.search(r'UPDATE_ADDRESS = "([^"]+)"', (REPO / "src" / "text.ts").read_text(encoding="utf-8"))
    assert panel, "src/text.ts no longer has UPDATE_ADDRESS"
    assert panel.group(1).endswith(f"/releases/latest/download/{plugin_name()}.zip")
    assert not panel.group(1).startswith("http")  # Decky adds "https://"; less to type
    # ... on a line of its own, to be read off the screen and typed.
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert re.search(rf"^\s*{re.escape(panel.group(1))}$", readme, re.MULTILINE)
