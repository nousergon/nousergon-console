"""The coverage gate's *scope* is asserted here, not only its number.

repository-baseline-policy.md §4.2 C5: the way a coverage gate stops being
honest is by narrowing what it measures rather than by lowering the number —
which reads as an improvement in every report. Measured on symposion, removing
one flag moved the reported figure from 34.76% to 92.36% with no new test
code.

So these tests assert what a passing suite cannot otherwise notice:

* the measured source is the WHOLE ``console`` package (C1), never a path or
  submodule narrower than that;
* the floor is enforced by a non-zero exit (C2) and is a ratchet that may be
  raised and never lowered (C3);
* no coverage config carries an ``omit`` that would shrink the denominator
  without a test noticing the change in words;
* every source module under ``console/`` is inside the measured package —
  nothing is invisible to the gate by living outside it;
* nothing in CI passes a ``--cov-fail-under`` that could shadow (or
  undercut) the pyproject.toml gate this test protects.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_ROOT = REPO_ROOT / "console"

#: The floor may be RAISED here as coverage improves. Lowering it is a policy
#: amendment (repository-baseline-policy.md §4.2 C3), not a code change.
MINIMUM_FLOOR = 87


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_coverage_source_is_the_whole_package() -> None:
    """C1 — ``source`` names the package root, so unimported modules still count."""
    sources = _pyproject()["tool"]["coverage"]["run"]["source"]
    assert sources == ["console"], (
        f"coverage source must be exactly the console package, got {sources!r}. "
        "Narrowing it to a submodule or a path measures the tested subset and "
        "reports it as the repository."
    )


def test_coverage_floor_is_enforced_and_never_lowered() -> None:
    """C2 + C3 — the gate exits non-zero below a floor that only ratchets up."""
    fail_under = _pyproject()["tool"]["coverage"]["report"]["fail_under"]
    assert isinstance(fail_under, int), (
        f"fail_under must be a single integer floor, got {fail_under!r}"
    )
    assert fail_under >= MINIMUM_FLOOR, (
        f"coverage floor {fail_under} is below the ratchet {MINIMUM_FLOOR}. "
        "A floor is raised as coverage improves and never lowered to make a "
        "change pass (repository-baseline-policy.md §4.2 C3)."
    )


def test_no_coverage_omit_shrinks_the_denominator() -> None:
    """A shrunk denominator is a narrowing that neither flag alone would reveal."""
    run_config = _pyproject()["tool"]["coverage"]["run"]
    omit = run_config.get("omit")
    assert not omit, (
        f"[tool.coverage.run] gained an `omit` list: {omit!r}. Omitting a path "
        "removes it from the denominator, raising the reported figure without "
        "adding a test — every module under console/ is currently measured, "
        "and adding an omit here is a scope decision that needs review, not a "
        "drive-by coverage bump."
    )


def test_every_source_module_is_inside_the_measured_package() -> None:
    """No source file lives outside what ``source = ["console"]`` measures."""
    stray = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.rglob("*.py")
        if PACKAGE_ROOT not in p.parents
        and p != PACKAGE_ROOT
        and "tests" not in p.parts
        and ".venv" not in p.parts
        and not p.parts[0].startswith(".")
    )
    assert not stray, (
        f"source modules outside the measured package are invisible to the "
        f"coverage gate: {stray}"
    )


def test_no_cov_fail_under_flag_shadows_the_pyproject_gate() -> None:
    """A CI-passed --cov-fail-under could silently override pyproject.toml's."""
    for workflow in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for match in re.findall(r"--cov-fail-under=(\d+)", text):
            assert int(match) >= MINIMUM_FLOOR, (
                f"{workflow.name} passes --cov-fail-under={match} directly, "
                "bypassing the pyproject.toml ratchet this test protects."
            )
