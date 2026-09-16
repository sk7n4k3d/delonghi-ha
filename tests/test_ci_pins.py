"""Guard against CI lint version drift.

The Ruff job went red on ``master`` without anyone touching a source file:
``lint.yaml`` invoked ``ruff-action`` with no ``version`` input, and the
action only auto-discovers a version from ``project.dependencies`` /
``dependency-groups`` — neither of which this pyproject declares. It fell
back to ``latest``, floated from 0.15.22 to 0.16.8, and ``ruff format
--check`` then started rejecting files nobody had edited.

These tests assert the two pins stay in sync, so a future bump cannot
reintroduce the failure silently. Reformatting files fixes the symptom;
the version float was the cause.

Parsing is deliberately stdlib-only. PyYAML is not in ``requirements_test.txt``
and a guard test is not a good reason to add a dependency.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
LINT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "lint.yaml"

RUFF_ACTION = "astral-sh/ruff-action@"


def _required_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    ruff_table = data.get("tool", {}).get("ruff")
    assert isinstance(ruff_table, dict), "pyproject.toml has no [tool.ruff] section"
    version = ruff_table.get("required-version")
    assert isinstance(version, str) and version, (
        "pyproject.toml [tool.ruff] is missing required-version — a mismatched ruff would then run instead of refusing."
    )
    return version


def _ruff_steps() -> list[list[str]]:
    """Return the `with:` body lines of every ruff-action step.

    A step starts at a line containing ``- uses:`` and runs until the next
    ``- uses:``. Only ruff-action steps are returned; their ``with:`` block
    is captured by indentation so a ``version:`` belonging to a different
    action cannot be mistaken for theirs.
    """
    lines = LINT_WORKFLOW.read_text(encoding="utf-8").splitlines()

    steps: list[list[str]] = []
    current: list[str] | None = None

    for line in lines:
        stripped = line.strip()
        is_new_step = stripped.startswith("- uses:") or stripped.startswith("- name:")
        if is_new_step:
            if current is not None:
                steps.append(current)
            current = [line] if RUFF_ACTION in line else None
        elif current is not None:
            current.append(line)
    if current is not None:
        steps.append(current)

    return steps


def _version_inputs(step: list[str]) -> list[str]:
    """Extract values of `version:` keys in a step's body."""
    found: list[str] = []
    for line in step[1:]:
        stripped = line.strip()
        if not stripped.startswith("version:"):
            continue
        value = stripped.split(":", 1)[1].strip().strip("\"'")
        if value:
            found.append(value)
    return found


def test_pyproject_pins_ruff_version() -> None:
    """`required-version` must exist so a mismatched ruff refuses to run."""
    assert _required_version(), "pyproject.toml is missing [tool.ruff] required-version"


def test_lint_workflow_has_ruff_steps() -> None:
    """Sanity: if the action is swapped out, this guard must be revisited."""
    assert _ruff_steps(), "lint.yaml no longer uses astral-sh/ruff-action — update this guard"


def test_every_ruff_step_pins_version() -> None:
    """Each ruff-action step must pass an explicit `version` input.

    Without it the action installs `latest`, which is precisely the bug
    this file exists to prevent.
    """
    for step in _ruff_steps():
        label = step[0].strip()
        assert _version_inputs(step), (
            f"ruff-action step {label!r} has no `version:` input. "
            "It will install `latest` and can turn lint red on untouched files."
        )


def test_pins_agree() -> None:
    """Workflow pin and `required-version` must be the same string.

    A local ruff that disagrees with CI produces different formatting,
    which is how "passes on my machine" becomes a red job.
    """
    required = _required_version()
    installed = {v for step in _ruff_steps() for v in _version_inputs(step)}
    assert installed == {required}, (
        f"ruff version mismatch — workflow installs {sorted(installed)} "
        f"but pyproject requires {required!r}. Bump both together."
    )


@pytest.mark.parametrize("path", ["pyproject.toml", ".github/workflows/lint.yaml"])
def test_pinned_files_exist(path: str) -> None:
    """Fail loudly rather than skip if a pinned file is moved or renamed."""
    assert (REPO_ROOT / path).is_file(), f"{path} is missing — update tests/test_ci_pins.py"
