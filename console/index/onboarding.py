"""§9.8 onboarding cost (console-policy.md §2.6, §9.8). Target 0.

For every component that entered the registry in the trailing N days, the
number of edits to `console/` and to the console's configuration shape
(`config.example.yaml`) that were required to make it appear. This is §2.6's
"onboarding a component is writing one file" test, run as a standing number
rather than asserted once at review time.

**Mirrors the discover-then-compare SHAPE of
`nous-ergon-ops/scripts/authority_surface.py`** (per the issue's own gotcha):
discover the population from an artifact that already carries the fact
(here, git history of the registry directory) rather than a hand-maintained
list, then compare against what changed. It does NOT mirror that script's
harness — `authority_surface.py` clones the whole fleet and is time-dependent,
which is exactly the shape to avoid copying; this needs only the one repo's
own git log.

**Uncomputable, honestly, when there is nothing to discover from**: no git
history for the registry path (not a repo, or the path was never committed),
or no registry configured at all. The real fleet registry does not exist yet
(alpha-engine-config-I6115) — this module works against whatever registry
config.py hands it, fixture included, and says so when it cannot.

**Known edge case, measured against this repo's own example registry**: a
row added in the SAME commit that scaffolded the console package itself (this
repo's genesis commit, `c903602`) reports every file that bootstrapping
commit touched as its "onboarding cost" — correct as a same-commit measure,
uninformative as a signal, because nothing was onboarded onto an EXISTING
surface. This is a one-time artifact of a repo's first commit, not a defect
in the measure; it resolves itself once 90 days pass the genesis commit, and
does not recur for any component added afterward (§9.8's real target case).
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

#: What counts as "an edit to the console" for this number (§2.6's test).
#: The registry directory itself is deliberately NOT in this list — adding a
#: registry row is the one required edit, not a cost being measured.
EDIT_PATHS: tuple[str, ...] = ("console", "config.example.yaml")

_GIT_TIMEOUT_SECONDS = 10


def _git(repo_root: str, args: list[str]) -> str | None:
    """Run one git command; None on any failure (no git, not a repo, …).

    An adapter declares what it cannot supply rather than raising (§2.3) —
    this is that rule applied to a git subprocess instead of an HTTP call.
    """
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _component_id(fpath: Path, id_field: str) -> str | None:
    try:
        with open(fpath) as fh:
            row = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None
    cid = row.get(id_field)
    return str(cid) if cid else None


def discover_recent_additions_detail(
    repo_root: str,
    registry_paths: list[str],
    id_field: str = "component_id",
    since_days: int = 90,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """``discover_recent_additions`` plus the two census counts that decide
    whether an empty result is a finding or a fact.

    ``{"additions": …, "rows_seen": int, "rows_with_history": int}``, or
    ``None`` when git history is unavailable at all.

    **Why the counts exist.** An empty ``additions`` has two indistinguishable
    causes, and only one of them is "nothing was onboarded recently". The
    other is that the registry rows are not in this repository's history at
    all — the fleet's own deployment is exactly that shape: the console runs
    from a checkout whose ``registry.d/`` is delivered out of band and is
    untracked (``git status --porcelain`` → ``?? registry.d/``), so all 291
    rows produce no ``git log`` line and §9.8 reported
    ``{computable: true, count: 0, of: 0}`` — an aggregate over an empty
    population rendered indistinguishably from the target being met. That is
    the shape `Index.broken_registries` already guards §9.1 and §9.6 against
    (`alpha-engine-config-I7126`); this is the same guard on §9.8.
    """
    if _git(repo_root, ["rev-parse", "--is-inside-work-tree"]) is None:
        return None
    # A shallow clone (`actions/checkout`'s default, and any `git clone
    # --depth`) truncates history at the fetch boundary: `git log --follow`
    # on a file added before that boundary finds nothing and would silently
    # misattribute the addition to whatever commit happens to be the shallow
    # tip. Degrading honestly here is what makes `.github/workflows/test.yml`
    # needing `fetch-depth: 0` a real requirement rather than an optional
    # nicety — this repo's own CI runs this exact code against its own
    # history (config.example.yaml's registry) on every PR.
    shallow = _git(repo_root, ["rev-parse", "--is-shallow-repository"])
    if shallow is not None and shallow.strip() == "true":
        return None
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=since_days)
    additions: dict[str, dict[str, str]] = {}
    rows_seen = 0
    rows_with_history = 0
    for reg_path in registry_paths:
        p = Path(reg_path)
        if not p.is_absolute():
            p = Path(repo_root) / reg_path
        if not p.is_dir():
            continue
        for fname in sorted(os.listdir(p)):
            if not fname.endswith((".yaml", ".yml")):
                continue
            fpath = p / fname
            cid = _component_id(fpath, id_field)
            if not cid:
                continue
            rows_seen += 1
            log = _git(repo_root, [
                "log", "--diff-filter=A", "--follow", "--format=%H%x09%aI", "--",
                str(fpath),
            ])
            lines = [l for l in (log or "").strip().splitlines() if l]
            if not lines:
                continue  # never committed — nothing to discover from
            rows_with_history += 1
            # `git log` is newest-first; with --follow the LAST line is the
            # original addition of the file's earliest-known name.
            commit, _, added_at = lines[-1].partition("\t")
            try:
                ts = datetime.fromisoformat(added_at)
            except ValueError:
                continue
            if ts >= cutoff:
                additions[cid] = {"added_at": added_at, "commit": commit}
    return {
        "additions": additions,
        "rows_seen": rows_seen,
        "rows_with_history": rows_with_history,
    }


def discover_recent_additions(
    repo_root: str,
    registry_paths: list[str],
    id_field: str = "component_id",
    since_days: int = 90,
    now: datetime | None = None,
) -> dict[str, dict[str, str]] | None:
    """component_id → ``{"added_at": iso, "commit": sha}``, for rows added
    within the trailing ``since_days``. ``None`` when git history is
    unavailable at all (distinct from an empty dict, which means "available,
    and nothing was added recently" — a distinction
    ``discover_recent_additions_detail`` is needed to make safely)."""
    detail = discover_recent_additions_detail(
        repo_root, registry_paths, id_field, since_days, now
    )
    return None if detail is None else detail["additions"]


def count_edits_in_commit(
    repo_root: str, commit: str, edit_paths: tuple[str, ...] = EDIT_PATHS
) -> int | None:
    """Files under ``edit_paths`` touched by the ONE commit that added the
    registry row (`discover_recent_additions`'s ``commit``).

    Deliberately scoped to that single commit rather than "anything touching
    `console/` since the addition date" — the naive open-ended version was
    measured against this repo's own history to over-count by two orders of
    magnitude, attributing unrelated LATER feature work (every subsequent PR)
    to a component that needed none of it. `console-policy.md` §9.8 asks what
    onboarding COST, not what happened afterward.

    This assumes an onboarding PR lands as one commit on the default branch —
    true for this fleet, which squash-merges (`pull-request-policy.md`). A
    genuine follow-up fix landed as a SEPARATE later commit is undercounted
    rather than a normal repo's ongoing development being overcounted — the
    safer direction when the target is 0 and the number is meant to catch a
    real cost, not manufacture false ones.
    """
    out = _git(repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", commit])
    if out is None:
        return None
    changed = [line for line in out.strip().splitlines() if line]
    edit_dirs = tuple(p.rstrip("/") + "/" for p in edit_paths if not p.endswith((".py", ".yaml", ".yml")))
    edit_files = tuple(p for p in edit_paths if p.endswith((".py", ".yaml", ".yml")))
    count = 0
    for path in changed:
        if path in edit_files or any(path.startswith(d) for d in edit_dirs):
            count += 1
    return count


def compute_onboarding_cost(
    repo_root: str,
    registry_paths: list[str],
    id_field: str = "component_id",
    since_days: int = 90,
    now: datetime | None = None,
) -> dict[str, Any]:
    """§9.8's published number.

    ``computable: False`` (never the reserved N/A-NOT-IMPL token — that is
    §11's carve-out and this number is not exempt from needing a reason) in
    each of the three ways there is nothing to discover from: no git history
    at repo_root at all, no registry rows under the configured path(s), and
    rows present but none of them git-tracked. The third is the one the
    fleet's own deployment hits, and it previously fell through to
    ``{count: 0, of: 0, computable: true}`` — §5.3's forbidden aggregate over
    an empty population, reading exactly like the target being met.
    """
    detail = discover_recent_additions_detail(repo_root, registry_paths,
                                              id_field, since_days, now)
    if detail is None:
        return {
            "count": None, "of": 0, "computable": False,
            "reason": (
                "no git history available for the configured registry "
                "path(s) — §9.8 needs registry-row addition dates, which "
                "only a git-tracked registry can supply"
            ),
        }
    if detail["rows_seen"] == 0:
        return {
            "count": None, "of": 0, "computable": False,
            "reason": (
                "no registry rows found under the configured registry "
                f"path(s) {sorted(registry_paths)!r} — §9.8's population is "
                "the components that entered the registry, and there is no "
                "registry here to have entered"
            ),
        }
    if detail["rows_with_history"] == 0:
        return {
            "count": None, "of": 0, "computable": False,
            "reason": (
                f"{detail['rows_seen']} registry row(s) were read under "
                f"{sorted(registry_paths)!r}, but none is tracked in the git "
                f"history at {repo_root!r} — the rows are delivered out of "
                "band, so no addition date is discoverable and §9.8 has no "
                "population to measure. Point `repo_root` at the repository "
                "the registry is committed to, or accept the number as "
                "uncomputable here"
            ),
        }
    additions = detail["additions"]
    components = []
    total_edits = 0
    for cid, info in sorted(additions.items()):
        edits = count_edits_in_commit(repo_root, info["commit"])
        edits = edits or 0
        total_edits += edits
        components.append({
            "component_id": cid, "added_at": info["added_at"],
            "commit": info["commit"], "edits": edits,
        })
    return {
        "count": total_edits, "of": len(components), "computable": True,
        "since_days": since_days, "components": components,
    }
