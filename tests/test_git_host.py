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


# ---------------------------------------------------------------------------
# include_workflow_runs — `alpha-engine-config-I6835`.
#
# 55 of the fleet's registered components are `github-actions` and NONE of them
# emitted anything the console could see: this adapter read issues and PRs and
# never looked at what a repo actually RUNS. Folded in here rather than built as
# a new adapter per docs/adapters.md's boundary test — same source shape (a Git
# host's API), a different resource.
# ---------------------------------------------------------------------------

def _wf(path, name, state="active", last_run=None):
    return {"id": 1, "name": name, "path": path, "state": state, "last_run": last_run}


def _run(conclusion=None, status="completed", updated="2026-08-10T12:00:00Z"):
    return {"conclusion": conclusion, "status": status, "updatedAt": updated,
            "url": "https://host/run/1", "headBranch": "main"}


def _fetch_workflows(workflows, **extra):
    return git_host.fetch(
        {"org": "o", "repos": ["alpha-engine-config"], "include_workflow_runs": True, **extra},
        lister=lambda org, repo: [],
        pr_lister=lambda org, repo: [],
        workflow_lister=lambda org, repo: workflows,
    )


class TestTheIdentifierMatchesTheRegistrysOwnDerivation:
    """The ONE thing that must not drift. `authority_surface.py::_slugify`
    derives a registry component's id from (repo, workflow file stem); if this
    adapter derived a different id, the same workflow would render TWICE — once
    UNREGISTERED from here, once UNREPORTED from the registry row — which is the
    exact defect I6835 was filed for at the check layer."""

    def test_the_slug_is_repo_plus_workflow_file_stem(self):
        result = _fetch_workflows([_wf(".github/workflows/ci-runner-mode-guard.yml", "CI Runner Mode Guard")])
        assert [e.id for e in result.entities] == ["alpha-engine-config-ci-runner-mode-guard"]

    def test_underscores_and_dots_collapse_like_the_discoverers_slugify(self):
        result = _fetch_workflows([_wf(".github/workflows/lib_pin.drift.sweep.yaml", "X")])
        assert [e.id for e in result.entities] == ["alpha-engine-config-lib-pin-drift-sweep"]

    def test_the_slug_comes_from_the_PATH_not_the_display_name(self):
        """A workflow's `name:` is prose and changes freely; its file path is
        what the discoverer sees."""
        result = _fetch_workflows([_wf(".github/workflows/stable-path.yml", "Some Renamed Title")])
        assert [e.id for e in result.entities] == ["alpha-engine-config-stable-path"]

    def test_a_workflow_with_no_path_is_skipped_not_given_a_minted_id(self):
        assert _fetch_workflows([_wf(None, "pathless")]).entities == ()


class TestTheFourStatesStayFourFacts:
    def test_a_successful_run_is_healthy(self):
        result = _fetch_workflows([_wf(".github/workflows/a.yml", "A", last_run=_run("success"))])
        assert result.entities[0].state is State.HEALTHY

    def test_a_failed_run_is_failed(self):
        result = _fetch_workflows([_wf(".github/workflows/a.yml", "A", last_run=_run("failure"))])
        assert result.entities[0].state is State.FAILED

    def test_a_workflow_that_has_NEVER_run_says_exactly_that(self):
        """The most serious finding this adapter can make about a scheduled
        workflow, and the one invisible in any runs-only listing — which is why
        the lister enumerates workflows first and joins runs onto them."""
        e = _fetch_workflows([_wf(".github/workflows/never.yml", "Never")]).entities[0]
        assert e.state is State.NEVER_RAN, (
            "§8.3 has a member for exactly this — UNREPORTED would blur "
            "'never executed' into 'we cannot see it'"
        )
        assert e.provenance.as_of is None, "a never-run workflow must not carry an invented timestamp"

    def test_an_in_flight_run_keeps_the_last_state_and_flags_itself(self):
        """§8.3's thirteen have no in-progress member (`alpha-engine-config-I6358`).
        Inventing a thirteenth breaks the closed vocabulary every consumer
        switches on; rendering UNREPORTED would blink a healthy nightly job to
        'nothing reported' every night while it ran. The fact is carried in
        detail instead of being lost or faked."""
        e = _fetch_workflows([
            _wf(".github/workflows/a.yml", "A", last_run=_run("success", status="in_progress"))
        ]).entities[0]
        assert e.state is State.HEALTHY
        assert e.detail["in_flight"] is True

    def test_an_in_flight_run_with_no_prior_conclusion_is_not_green(self):
        """The first-ever run being in flight must not resolve to HEALTHY off an
        empty conclusion string."""
        e = _fetch_workflows([
            _wf(".github/workflows/a.yml", "A", last_run=_run(None, status="in_progress"))
        ]).entities[0]
        assert e.state is State.DEGRADED

    def test_a_host_disabled_workflow_is_DISABLED_not_failed(self):
        e = _fetch_workflows([
            _wf(".github/workflows/a.yml", "A", state="disabled_manually", last_run=_run("failure"))
        ]).entities[0]
        assert e.state is State.DISABLED

    def test_an_unrecognised_conclusion_is_DEGRADED_never_healthy(self):
        """A conclusion this adapter has never seen is a fact about the host,
        not a healthy workflow. `success` is the only value that may be green."""
        e = _fetch_workflows([
            _wf(".github/workflows/a.yml", "A", last_run=_run("some_new_thing"))
        ]).entities[0]
        assert e.state is State.DEGRADED


class TestItDoesNotDisturbWhatTheAdapterAlreadyDid:
    def test_workflows_are_not_emitted_unless_opted_in(self):
        result = git_host.fetch(
            {"org": "o", "repos": ["r"]},
            lister=lambda org, repo: [],
            pr_lister=lambda org, repo: [],
            workflow_lister=lambda org, repo: [_wf(".github/workflows/a.yml", "A")],
        )
        assert result.entities == ()

    def test_a_workflow_listing_failure_never_drops_the_issues_already_read(self):
        def boom(org, repo):
            raise RuntimeError("host unreachable")

        result = git_host.fetch(
            {"org": "o", "repos": ["r"], "include_workflow_runs": True},
            lister=lambda org, repo: [
                {"number": 1, "title": "t", "state": "OPEN", "labels": [],
                 "updatedAt": "2026-08-10T12:00:00Z", "url": "u"},
            ],
            pr_lister=lambda org, repo: [],
            workflow_lister=boom,
        )
        assert [e.id for e in result.entities] == ["r-I1"]
        assert result.status is AdapterStatus.OK, (
            "entities were read; a partial failure must not discard them"
        )
