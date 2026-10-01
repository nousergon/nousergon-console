"""``docs/contract.md`` is the console's single normative contract, and every
section names the code and tests that enforce it. Those names are only worth
anything while they resolve: a renamed module or test file would leave the
contract citing an enforcer that no longer exists, and nothing else notices.

So every relative link in the contract must resolve to a file in this repo,
and every in-page or cross-doc anchor must match a heading in its target.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "contract.md"

_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to '-'."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {
        _slug(line.lstrip("#"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    }


def _relative_links() -> list[str]:
    links = _LINK.findall(CONTRACT.read_text(encoding="utf-8"))
    return [link for link in links if not re.match(r"^[a-z]+:", link)]


def test_contract_has_relative_links():
    assert _relative_links(), "contract.md cites no enforcing code or tests"


def test_every_relative_link_resolves():
    missing = []
    for link in _relative_links():
        target, _, _ = link.partition("#")
        if target and not (CONTRACT.parent / target).resolve().exists():
            missing.append(link)
    assert not missing, f"contract.md links to files that do not exist: {missing}"


def test_every_anchor_matches_a_heading():
    broken = []
    for link in _relative_links():
        target, sep, anchor = link.partition("#")
        if not sep:
            continue
        path = (CONTRACT.parent / target).resolve() if target else CONTRACT
        if anchor not in _anchors(path):
            broken.append(link)
    assert not broken, f"contract.md anchors match no heading: {broken}"
