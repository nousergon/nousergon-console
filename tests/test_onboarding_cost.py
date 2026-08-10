"""§9.8 onboarding cost (console-policy.md §2.6, §9.8). Target 0.

For every component that entered the registry in the trailing 90 days, the
number of edits to `console/` and to the console's configuration required to
make it appear. Mirrors nous-ergon-ops/scripts/authority_surface.py's
discover-then-compare SHAPE (not its harness, which clones the whole fleet
and is time-dependent — see the issue's own gotcha).

Without a real fleet registry (alpha-engine-config-I6115, tracked
separately) this is exercised against a throwaway git repo built entirely in
the test, so it is deterministic and never depends on nousergon-console's own
history.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from console.index.onboarding import compute_onboarding_cost

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env=env)


def _commit(repo: Path, message: str, when: datetime) -> None:
    stamp = when.isoformat()
    env = {
        "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message, env=env)


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "console").mkdir()
    (root / "console" / "__init__.py").write_text("# seed\n")
    (root / "config.example.yaml").write_text("console: {}\n")
    reg = root / "example" / "registry.d"
    reg.mkdir(parents=True)
    _commit(root, "seed", NOW - timedelta(days=400))
    return root


def _write_component(repo: Path, cid: str, when: datetime) -> None:
    reg = repo / "example" / "registry.d"
    with open(reg / f"{cid}.yaml", "w") as fh:
        yaml.safe_dump({"component_id": cid, "lifecycle": "in-service"}, fh)
    _commit(repo, f"add registry row for {cid}", when)


def test_a_component_added_long_ago_is_outside_the_trailing_window(repo):
    _write_component(repo, "comp-old", NOW - timedelta(days=200))
    result = compute_onboarding_cost(str(repo), ["example/registry.d"], now=NOW)
    assert result["computable"] is True
    assert result["of"] == 0
    assert result["count"] == 0


def test_a_component_added_this_week_with_no_console_edits_costs_zero(repo):
    _write_component(repo, "comp-new", NOW - timedelta(days=3))
    result = compute_onboarding_cost(str(repo), ["example/registry.d"], now=NOW)
    assert result["computable"] is True
    assert result["of"] == 1
    assert result["count"] == 0
    assert result["components"][0]["component_id"] == "comp-new"


def test_a_console_edit_bundled_into_the_onboarding_commit_is_counted(repo):
    """A same-commit console edit is the one signal that is unambiguously
    caused by onboarding — bundle a `console/` change into the SAME commit
    that adds the registry row, as a squash-merged onboarding PR would."""
    reg = repo / "example" / "registry.d"
    with open(reg / "comp-costly.yaml", "w") as fh:
        yaml.safe_dump({"component_id": "comp-costly", "lifecycle": "in-service"}, fh)
    (repo / "console" / "extra.py").write_text("# a change forced by onboarding\n")
    _commit(repo, "onboard comp-costly, and had to touch console/",
           NOW - timedelta(days=2))
    result = compute_onboarding_cost(str(repo), ["example/registry.d"], now=NOW)
    assert result["count"] == 1
    row = result["components"][0]
    assert row["component_id"] == "comp-costly" and row["edits"] == 1


def test_console_development_AFTER_onboarding_is_never_attributed_to_it(repo):
    """The defect the same-commit design replaces: an open-ended "anything
    touching console/ since the addition date" window measured two orders of
    magnitude of unrelated LATER feature work as this component's cost, on
    nousergon-console's own history. A clean onboarding commit followed by
    ordinary, unrelated console development must cost 0."""
    _write_component(repo, "comp-clean", NOW - timedelta(days=3))
    (repo / "console" / "later_feature.py").write_text("# unrelated later work\n")
    _commit(repo, "console: an unrelated feature merged after comp-clean onboarded",
           NOW - timedelta(days=1))
    result = compute_onboarding_cost(str(repo), ["example/registry.d"], now=NOW)
    assert result["count"] == 0
    assert result["components"][0]["edits"] == 0


def test_a_console_edit_unrelated_to_onboarding_before_the_addition_does_not_count(repo):
    (repo / "console" / "unrelated.py").write_text("# unrelated earlier work\n")
    _commit(repo, "console: unrelated change", NOW - timedelta(days=10))
    _write_component(repo, "comp-clean", NOW - timedelta(days=3))
    result = compute_onboarding_cost(str(repo), ["example/registry.d"], now=NOW)
    assert result["count"] == 0


def test_uncomputable_on_a_shallow_clone(repo, tmp_path):
    """`actions/checkout`'s default `fetch-depth: 1` truncates history at the
    fetch boundary — trusting `git log --follow` past it would misattribute
    an addition to whatever commit happens to be the shallow tip. Degrading
    honestly here is what makes `.github/workflows/test.yml` needing
    `fetch-depth: 0` a real requirement."""
    _write_component(repo, "comp-new", NOW - timedelta(days=3))
    shallow = tmp_path / "shallow-clone"
    _git(tmp_path, "clone", "--depth", "1", f"file://{repo}", str(shallow))
    result = compute_onboarding_cost(str(shallow), ["example/registry.d"], now=NOW)
    assert result["computable"] is False
    assert result["count"] is None


def test_uncomputable_when_the_path_has_no_git_history(tmp_path):
    root = tmp_path / "not-a-repo"
    (root / "example" / "registry.d").mkdir(parents=True)
    result = compute_onboarding_cost(str(root), ["example/registry.d"], now=NOW)
    assert result["computable"] is False
    assert result["count"] is None
    assert result["reason"]


def test_uncomputable_with_no_registry_paths_configured(repo):
    result = compute_onboarding_cost(str(repo), [], now=NOW)
    assert result["computable"] is True
    assert result["of"] == 0
    assert result["count"] == 0
