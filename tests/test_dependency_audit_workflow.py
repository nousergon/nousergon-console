from pathlib import Path

import yaml


def test_dependency_audit_is_blocking_and_runs_after_install():
    path = Path(__file__).resolve().parent.parent / ".github/workflows/test.yml"
    workflow = yaml.safe_load(path.read_text())
    job = workflow["jobs"]["pytest"]
    steps = job["steps"]
    install_index = next(i for i, step in enumerate(steps) if step.get("name") == "Install")
    audit_index = next(
        i for i, step in enumerate(steps) if step.get("name") == "Audit dependencies"
    )
    audit = steps[audit_index]

    assert audit_index > install_index
    assert audit["run"].splitlines() == [
        "python -m pip install pip-audit",
        "python -m pip_audit",
    ]
    for scope in (job, audit):
        assert not scope.get("continue-on-error", False)
        assert "if" not in scope
