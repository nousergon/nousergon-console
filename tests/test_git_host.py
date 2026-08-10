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


# ------------------------------------------------------------- pull requests
# nousergon-console#59: merged PRs (47_Merged_PRs, Artifact) and open PRs
# joining the Decision Queue (49_Decision_Queue's own declared source names
# "GitHub Issues/PRs", not issues alone).

FIXTURE_PRS = [
    {"number": 66, "title": "feat(index): compute the nine numbers",
     "state": "MERGED", "labels": [], "updatedAt": "2026-08-10T15:48:23Z",
     "url": "https://example/pull/66", "mergedAt": "2026-08-10T15:48:23Z"},
    {"number": 70, "title": "wip: still cooking", "state": "OPEN",
     "labels": [{"name": "gate:decision"}], "updatedAt": "2026-08-10T16:00:00Z",
     "url": "https://example/pull/70", "mergedAt": None},
    {"number": 55, "title": "abandoned approach", "state": "CLOSED",
     "labels": [], "updatedAt": "2026-08-01T00:00:00Z",
     "url": "https://example/pull/55", "mergedAt": None},
]


def _pr_lister(org, repo):
    return FIXTURE_PRS


def test_prs_not_fetched_by_default():
    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"]},
        lister=_lister, pr_lister=_pr_lister,
    )
    assert not any(e.id.startswith("some-repo-PR") for e in result.entities)
    assert not any(e.id == "example-org/some-repo#66" for e in result.entities)


def test_merged_pr_becomes_an_artifact():
    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"], "include_prs": True},
        lister=_lister, pr_lister=_pr_lister,
    )
    artifact = next(e for e in result.entities if e.id == "example-org/some-repo#66")
    assert artifact.kind is Kind.ARTIFACT
    assert artifact.state == "merged"


def test_open_pr_becomes_a_decision_in_its_own_namespace():
    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"], "include_prs": True},
        lister=_lister, pr_lister=_pr_lister,
    )
    decision = next(e for e in result.entities if e.id == "some-repo-PR70")
    assert decision.kind is Kind.DECISION
    assert decision.state == "open"
    assert decision.detail["is_pull_request"] is True


def test_closed_unmerged_pr_is_not_emitted():
    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"], "include_prs": True},
        lister=_lister, pr_lister=_pr_lister,
    )
    assert not any("55" in e.id for e in result.entities)


def test_pr_and_issue_numbers_do_not_collide():
    """A repo's issue #12 and PR #12 must render as two distinct entities —
    `<repo>-I<N>` vs `<repo>-PR<N>` are different identifiers on purpose."""
    def lister(org, repo):
        return [{"number": 66, "title": "an issue, not the PR above", "state": "OPEN",
                  "labels": [], "updatedAt": "2026-08-10T00:00:00Z", "url": "https://example/66"}]

    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"], "include_prs": True},
        lister=lister, pr_lister=_pr_lister,
    )
    ids = {e.id for e in result.entities}
    assert "some-repo-I66" in ids
    assert "example-org/some-repo#66" in ids  # the merged PR, same number, different namespace


def test_pr_lister_failure_does_not_drop_already_read_issues():
    def boom(org, repo):
        raise RuntimeError("PR search unreachable")

    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"], "include_prs": True},
        lister=_lister, pr_lister=boom,
    )
    assert any(e.id == "some-repo-I327" for e in result.entities)


def test_no_consumed_by_edges_declared_structural_leaf():
    """nousergon-console#52: git-host is a documented structural leaf for
    `consumed-by` — a Decision/Incident is a terminal record a human rules
    on, and the host API this adapter reads (number, title, tracker state,
    labels, timestamps) carries no consumer identifier to declare. This is
    the explicit "no consumer relation" record §6/§3.3 asks for, so a future
    audit does not re-flag the adapter as an oversight."""
    result = git_host.fetch(
        {"org": "example-org", "repos": ["some-repo"], "incident_label": "incident"},
        lister=_lister,
    )
    assert result.entities  # sanity: the fixture does produce entities
    assert result.edges == ()
