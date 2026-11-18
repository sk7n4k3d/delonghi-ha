"""Packaging consistency guards (audit 2026-11-18, #16).

Issue #52 shipped a release whose zip layout disagreed with what HACS
expected; the release workflow validates tag↔manifest but nothing tied
manifest.json to the CHANGELOG or hacs.json to release.yaml. These tests
close that gap: a version bump without a changelog entry, or a renamed zip
in only one of the two places, fails the test suite (and therefore CI)
instead of the user's install.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COFFEE_MANIFEST = ROOT / "custom_components" / "delonghi_coffee" / "manifest.json"
DAEDALUS_MANIFEST = ROOT / "custom_components" / "delonghi_daedalus" / "manifest.json"
CHANGELOG = ROOT / "CHANGELOG.md"
HACS = ROOT / "hacs.json"
RELEASE_WF = ROOT / ".github" / "workflows" / "release.yaml"


def _version(path: Path) -> str:
    return json.loads(path.read_text())["version"]


def _changelog_versions() -> list[str]:
    text = CHANGELOG.read_text()
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.MULTILINE)


def test_coffee_manifest_matches_first_changelog_entry() -> None:
    versions = _changelog_versions()
    assert versions, "CHANGELOG.md has no released version sections"
    coffee = [v for v in versions if v.startswith("1.")]
    assert coffee[0] == _version(COFFEE_MANIFEST), (
        f"manifest says {_version(COFFEE_MANIFEST)} but the newest 1.x CHANGELOG entry is {coffee[0]} — "
        "add the CHANGELOG section in the same commit as the bump"
    )


def test_daedalus_manifest_matches_first_daedalus_changelog_entry() -> None:
    versions = _changelog_versions()
    daedalus = [v for v in versions if v.startswith("0.")]
    assert daedalus, "CHANGELOG.md has no daedalus (0.x) sections"
    assert daedalus[0] == _version(DAEDALUS_MANIFEST), (
        f"daedalus manifest says {_version(DAEDALUS_MANIFEST)} but the newest 0.x CHANGELOG entry is {daedalus[0]}"
    )


def test_hacs_zip_name_matches_release_workflow() -> None:
    hacs = json.loads(HACS.read_text())
    assert hacs.get("zip_release") is True, "hacs.json must keep zip_release: true"
    filename = hacs["filename"]
    workflow = RELEASE_WF.read_text()
    assert re.search(rf"files:\s*{re.escape(filename)}\s*$", workflow, flags=re.MULTILINE), (
        f"hacs.json filename '{filename}' is not the file published by release.yaml — HACS would 404 on every install"
    )
    # the zip must actually be built under that name
    assert filename in workflow, "release.yaml never references the hacs.json zip name"
