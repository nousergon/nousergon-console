"""git-host adapter tests — recorded fixtures, no live host."""
from __future__ import annotations

import pytest

from console.adapters import git_host
from console.model.envelope import AdapterStatus
from console.model.kinds import Kind, State

FIXTURE_ISSUES = [
    {"number": 327, "title": "Build the console entity index", "state": "OPEN",
     "labels": [{"name": "P1"}, {"name": "complexity:high"}],
     "updatedAt": "2026-07-31T17:53:39Z", "url": "https://example/327"},
    {"number": 12, "title": "weekly-sf rough weekend", "state": "CLOSED",
     "labels": [{"name": "incident"}], "updatedAt": "2026-07-20T00:00:00Z",
     "url": "https://example/12"},
]


def _lister(org, repo):
    return FIXTURE_ISSUES


def test_issues_become_decisions_with_tracker_refs():
    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"], "incident_label": "incident"},
        lister=_lister,
    )
    assert result.status is AdapterStatus.OK
    decision = next(e for e in result.entities if e.kind is Kind.DECISION)
    assert decision.id == "some-repo-I327"
    assert decision.facets["repo"] == "example-org/some-repo"


def test_incident_label_becomes_incident_kind():
    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"], "incident_label": "incident"},
        lister=_lister,
    )
    incident = next(e for e in result.entities if e.kind is Kind.INCIDENT)
    assert incident.id == "some-repo-I12"
    assert incident.state == "resolved"  # closed incident = resolved record


def test_open_decision_carries_the_trackers_own_value():
    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"]}, lister=_lister)
    open_decision = next(e for e in result.entities if e.id == "some-repo-I327")
    # An issue is open or closed — not HEALTHY, and certainly not FAILED.
    # Forcing it into the component vocabulary is what produced the UNKNOWN
    # fall-through §8.3 forbids by name (§5.1 second half).
    assert open_decision.state == "open"


def test_lister_failure_is_failed_state_not_empty():
    def boom(org, repo):
        raise RuntimeError("host unreachable")

    result = git_host.fetch({"org": "o", "repos": ["r"]}, lister=boom)
    assert result.status is AdapterStatus.FAILED


def test_missing_org_is_failed():
    result = git_host.fetch({"repos": ["r"]}, lister=_lister)
    assert result.status is AdapterStatus.FAILED
