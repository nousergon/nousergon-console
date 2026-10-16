"""Citation links — which plain-text references in a rendered document name an
issue, a pull request or a comment, and where each one lives.

Reports cite work as plain text: ``ops#1709``, ``alpha-engine-config-I12351``,
``nous-ergon-ops-PR1718``, ``nousergon-data#2110 comment 6213967618``. A reader
who wants to check a cited item had to copy the reference and rebuild its URL
by hand. A `citations:` declaration lets the markdown renderer
(`render/markdown.py`) turn each one into a link at RENDER time, so every
document already in the source gets links too, with nothing rewritten where it
is stored.

**Nothing about any particular organisation lives in this repo.** The
declaration names the base URL the repositories live under, the repositories
that may be linked, and short aliases for them. Only a declared repository or
alias is ever linked: a word that merely looks like a reference
(``ruling-#25``, ``#113`` with no repository) stays text, because a link the
console guessed is a link that can point at the wrong thing.

The URL shapes are the ones GitHub serves (``<base>/<repo>/issues/<n>``,
``/pull/<n>``, ``#issuecomment-<c>``); ``/issues/<n>`` redirects to the pull
request when ``<n>`` is one, which is why a bare ``repo#n`` uses it.

A declaration this build cannot use (a non-http(s) base, a repository name that
is not a path segment, an alias naming an undeclared repository or shadowing a
different one) FAILS THE BUILD naming the entry, never renders a page that
silently links nothing or links somewhere else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

_ATTR = "_console_citations"
#: A repository name or alias: one URL path segment, as GitHub allows them.
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_BASE = re.compile(r"^https?://[^\s/?#]+(?:/[^\s?#]*)?$", re.IGNORECASE)


class CitationsConfigError(ValueError):
    """A `citations:` declaration this build cannot evaluate. Raised by
    `parse` during `config.build_index`, so it fails the build by name."""


@dataclass(frozen=True)
class Citations:
    #: Where repositories live; a repository's URL is ``<base_url>/<repo>``.
    base_url: str
    #: Every name a reference may use (repositories and aliases), mapped to
    #: the repository it links to.
    names: tuple[tuple[str, str], ...]

    def repo(self, name: str) -> str | None:
        return dict(self.names).get(name)

    def issue_url(self, repo: str, number: str, comment: str | None = None) -> str:
        url = f"{self.base_url}/{repo}/issues/{number}"
        return f"{url}#issuecomment-{comment}" if comment else url

    def pull_url(self, repo: str, number: str, comment: str | None = None) -> str:
        url = f"{self.base_url}/{repo}/pull/{number}"
        return f"{url}#issuecomment-{comment}" if comment else url


def parse(raw: Any) -> Citations | None:
    """Validate and freeze the `citations:` block. None when undeclared."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise CitationsConfigError(
            f"`citations:` must be a mapping with `base_url:` and `repos:`, "
            f"not {type(raw).__name__}")
    base = str(raw.get("base_url") or "").strip().rstrip("/")
    if not _BASE.match(base):
        raise CitationsConfigError(
            "citations: `base_url` must be an absolute http(s) URL, e.g. "
            "https://github.com/<org>")
    repos = raw.get("repos")
    if not isinstance(repos, list) or not repos:
        raise CitationsConfigError("citations: `repos` must list at least one repository")
    names: dict[str, str] = {}
    for repo in repos:
        repo = str(repo).strip()
        if not _NAME.match(repo):
            raise CitationsConfigError(
                f"citations: repository {repo!r} is not a single path segment")
        names[repo] = repo
    aliases = raw.get("aliases") or {}
    if not isinstance(aliases, Mapping):
        raise CitationsConfigError("citations: `aliases` must map alias → repository")
    for alias, repo in aliases.items():
        alias, repo = str(alias).strip(), str(repo).strip()
        if not _NAME.match(alias):
            raise CitationsConfigError(
                f"citations: alias {alias!r} is not a single path segment")
        if repo not in repos:
            raise CitationsConfigError(
                f"citations: alias {alias!r} names {repo!r}, which `repos` does not declare")
        if names.get(alias, repo) != repo:
            # `data: nousergon-data` while a repository is itself called `data`
            # would make one reference mean two things.
            raise CitationsConfigError(
                f"citations: alias {alias!r} shadows the repository of the same name")
        names[alias] = repo
    return Citations(base_url=base, names=tuple(sorted(names.items())))


def attach(index: Any, citations: Citations | None) -> None:
    """Hang the parsed declaration off a built index — configuration, not
    graph, for the same reason `planner.attach` gives."""
    setattr(index, _ATTR, citations)


def declared(index: Any) -> Citations | None:
    return getattr(index, _ATTR, None)
