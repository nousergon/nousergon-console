"""Your actions — "what is waiting on me to do, and have I done it?"

An ACTION is an open issue carrying a declared label (`actions.labels`, for
example the tracker's operator-gate label) in one declared repository. The
page lists them with one checkbox each. Ticking the box writes the declared
``done_label`` and a one-line comment to the ISSUE, which is where the action
already lives; unticking removes the label. Somebody (in our fleet, Claude)
then checks the work and closes the issue with a note.

**Why this is the console's one write, and why it persists nothing.** The
console is a read-only index (README, `docs/stack-decision.md`): every figure
is a projection of a fact durable somewhere else. A tick is not a figure the
console holds — it is a fact about the issue, so it is written to the issue
and read back from it like every other fact. Nothing about it is kept on the
box: no file, no table, no cache that survives a rebuild. The only thing the
handler keeps is the list it already read, updated in memory so the page that
answers the POST shows the tick before the next rebuild re-reads it.

The write is deliberately narrow, and each bound is checked here rather than
trusted to the caller:

- only an issue that is IN the list just read can be ticked — a number the
  page never showed is refused, so the endpoint cannot label arbitrary issues;
- only the declared ``done_label`` is added or removed, never another;
- the token comes from ``write_token_command`` (a short-lived, one-repository
  credential minted per write), and the console stores none.

The rendering lives in `render/html.py::actions_page`; the route is
`/actions` (`server/router.py`), served in both representations (§3.8).
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass

SOURCE_NAME = "your-actions"
ACTIONS_QUESTION = "What is waiting on me to do, and which have I already done?"
_ATTR = "_console_actions"
_LABEL = re.compile(r"^[^\s,][^,]{0,49}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_TIMEOUT_SECONDS = 20
_TOKEN_TIMEOUT_SECONDS = 60
#: The summary shown under an action's title: its body's first paragraph.
SUMMARY_CHARS = 280
#: The most actions one listing reads (one page of the host's API per label).
MAX_ACTIONS = 100


class ActionsConfigError(ValueError):
    """An `actions:` block this build cannot honour, named by key."""


@dataclass(frozen=True)
class Declaration:
    org: str
    repo: str
    labels: tuple[str, ...]
    done_label: str
    token_command: tuple[str, ...]
    write_token_command: tuple[str, ...] = ()
    title: str = "Your actions"
    done_text: str = "Done — waiting for a check that it deployed and looks good."
    api_url: str = "https://api.github.com"

    @property
    def writable(self) -> bool:
        return bool(self.write_token_command)


@dataclass(frozen=True)
class Action:
    number: int
    ref: str
    title: str
    url: str
    labels: tuple[str, ...]
    done: bool
    summary: str
    created_at: str | None
    updated_at: str | None
    #: When it is due (``YYYY-MM-DD``) and where that date came from, or None.
    due: str | None = None
    due_source: str | None = None
    #: Any bound on WHEN it may happen ("after the 16:30 ET close"), or None.
    when: str | None = None
    #: The PR this action is about (merge it), when the issue names one.
    pr_url: str | None = None
    pr_ref: str | None = None
    #: The one command to run, when the issue carries one.
    command: str | None = None
    #: The linked PR's state as the tracker reports it: ``open``, ``merged``
    #: or ``closed``; None when there is no PR or it could not be read.
    pr_state: str | None = None
    #: The issue itself is closed (the archive), and when.
    closed: bool = False
    closed_at: str | None = None

    @property
    def actionable(self) -> bool:
        """Waiting on the reader: open, not ticked, and not about a PR that
        has already merged or closed (Brian 2026-11-05: "actionable only")."""
        return not (self.done or self.closed or self.pr_state in ("merged", "closed"))


@dataclass
class Listing:
    """What the last build read. ``actions`` is replaced (never mutated in
    place) by `mark`, so a request rendering it sees one coherent list."""

    declaration: Declaration
    actions: tuple[Action, ...] = ()
    read_at: str | None = None
    error: str | None = None
    #: Recently closed actions, newest first: the done archive. A failed read
    #: of it says so (``archive_error``) and never fails the open list.
    archive: tuple[Action, ...] = ()
    archive_error: str | None = None

    @property
    def todo(self) -> tuple[Action, ...]:
        return tuple(a for a in self.actions if a.actionable)

    @property
    def done(self) -> tuple[Action, ...]:
        """Everything not actionable: ticked or PR merged/closed (still open
        issues, waiting for a check), then the closed archive."""
        return tuple(a for a in self.actions if not a.actionable) + self.archive


# ------------------------------------------------------------------ parse ---

def _command(raw: Any, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, list) or not all(isinstance(a, (str, int)) for a in raw):
        raise ActionsConfigError(f"actions.{key} must be a list of strings")
    return tuple(str(a) for a in raw)


def parse(raw: Any) -> Declaration | None:
    """The `actions:` block, or None when there is none. Raises
    `ActionsConfigError` naming the key for anything it cannot honour."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ActionsConfigError("actions: must be a mapping")
    known = {"org", "repo", "labels", "done_label", "token_command",
             "write_token_command", "title", "done_text", "api_url"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ActionsConfigError(f"actions: unknown key(s) {', '.join(unknown)}")
    org, repo = raw.get("org"), raw.get("repo")
    for key, value in (("org", org), ("repo", repo)):
        if not isinstance(value, str) or not _NAME.match(value):
            raise ActionsConfigError(f"actions.{key} must be a plain name")
    labels = raw.get("labels")
    if isinstance(labels, str):
        labels = [labels]
    if (not isinstance(labels, list) or not labels
            or not all(isinstance(lab, str) and _LABEL.match(lab) for lab in labels)):
        raise ActionsConfigError("actions.labels must be a non-empty list of label names")
    done_label = raw.get("done_label")
    if not isinstance(done_label, str) or not _LABEL.match(done_label):
        raise ActionsConfigError("actions.done_label must be a label name")
    if done_label in labels:
        raise ActionsConfigError("actions.done_label must differ from every listed label")
    token_command = _command(raw.get("token_command"), "token_command")
    if not token_command:
        raise ActionsConfigError("actions.token_command is required")
    out = Declaration(
        org=org, repo=repo, labels=tuple(labels), done_label=done_label,
        token_command=token_command,
        write_token_command=_command(raw.get("write_token_command"), "write_token_command"),
    )
    for key in ("title", "done_text", "api_url"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ActionsConfigError(f"actions.{key} must be text")
            out = replace(out, **{key: raw[key].strip()})
    return out


# ------------------------------------------------------------------- host ---

#: (method, url, token, body-or-None) -> parsed JSON. Injectable for tests.
Request = Callable[[str, str, str, Any], Any]


def _http(method: str, url: str, token: str, body: Any) -> Any:
    from ..render.json import dumps  # the one wire encoder (test_wire_encoder)

    data = None if body is None else dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nousergon-console",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from None
    return json.loads(text) if text.strip() else None


def _mint(command: tuple[str, ...], decl: Declaration, repo: str | None = None) -> str:
    """The first stdout line of the configured token command. Its stdout is a
    credential, so a failure is described from stderr and the exit code only."""
    argv = [a.replace("{org}", decl.org).replace("{repo}", repo or decl.repo) for a in command]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, check=True,
                             timeout=_TOKEN_TIMEOUT_SECONDS)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[:1]
        raise RuntimeError(f"token command exit {exc.returncode}"
                           + (f": {detail[0][:200]}" if detail else "")) from None
    except Exception as exc:  # noqa: BLE001 - becomes the reason
        raise RuntimeError(f"token command failed: {type(exc).__name__}") from None
    token = next((ln.strip() for ln in out.stdout.splitlines() if ln.strip()), "")
    if not token:
        raise RuntimeError("token command printed no token")
    return token


_MD_NOISE = re.compile(r"[*_`>#]+")


def summary(body: str | None) -> str:
    """The body's first prose paragraph, plain, cut at a word boundary."""
    for para in re.split(r"\n\s*\n", str(body or "")):
        text = para.strip()
        if not text or text.startswith(("#", "|", "<!--", "```", "---")):
            continue
        text = " ".join(_MD_NOISE.sub("", text).split())
        if len(text) > SUMMARY_CHARS:
            text = text[:SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
        return text
    return ""


#: A body line declaring a due date or a timing bound. Read from the issue the
#: action lives in, so whoever files the ask writes them once, next to it:
#:   ``Due: 2026-11-06``                  — the date it should be done by
#:   ``When: after the 16:30 ET close``   — any bound on when it may happen
#: The tracker's existing date lines count too: a `default-ok` issue's
#: ``Default: <option> after YYYY-MM-DD`` (the date it auto-applies) and a
#: ``Re-exam: YYYY-MM-DD``; failing all of those, the issue's milestone date.
_DUE_LINE = re.compile(r"^\s*(?:[*_-]\s*)*(?:due|deadline|merge by)\s*[*_]*\s*:\s*[*_]*\s*"
                       r"(?P<date>\d{4}-\d{2}-\d{2})", re.IGNORECASE | re.MULTILINE)
_DEFAULT_LINE = re.compile(r"^\s*(?:[*_-]\s*)*default\s*[*_]*\s*:.*?\bafter\s+"
                           r"(?P<date>\d{4}-\d{2}-\d{2})", re.IGNORECASE | re.MULTILINE)
_REEXAM_LINE = re.compile(r"^\s*(?:[*_-]\s*)*re-?exam\s*[*_]*\s*:\s*[*_]*\s*"
                          r"(?P<date>\d{4}-\d{2}-\d{2})", re.IGNORECASE | re.MULTILINE)
_WHEN_LINE = re.compile(r"^\s*(?:[*_-]\s*)*(?:when|window|merge window|timing)\s*[*_]*\s*:"
                        r"\s*[*_]*\s*(?P<text>\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)
WHEN_CHARS = 120


def timing(body: str | None, milestone: Any = None) -> tuple[str | None, str | None, str | None]:
    """``(due, due_source, when)`` declared in an issue body, or Nones."""
    text = str(body or "")
    due = source = None
    for pattern, name in ((_DUE_LINE, "due"), (_DEFAULT_LINE, "default applies"),
                          (_REEXAM_LINE, "re-exam")):
        m = pattern.search(text)
        if m:
            due, source = m.group("date"), name
            break
    if due is None and isinstance(milestone, Mapping) and milestone.get("due_on"):
        due, source = str(milestone["due_on"])[:10], "milestone"
    when = None
    m = _WHEN_LINE.search(text)
    if m:
        when = " ".join(_MD_NOISE.sub("", m.group("text")).split())[:WHEN_CHARS] or None
    return due, source, when


_PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
_FENCE = re.compile(r"```[\w-]*\n(.+?)\n```", re.DOTALL)
_CODE_LINE = re.compile(r"^\s*`([^`\n]{4,})`\s*$", re.MULTILINE)


def pull_request(body: str | None) -> tuple[str | None, str | None]:
    """(url, "repo#N") of the first PR the body links, or (None, None).
    Brian 2026-11-04: the page shows PRs and commands, not the issues behind them."""
    m = _PR_URL.search(str(body or ""))
    if not m:
        return None, None
    return m.group(0), f"{m.group(2)}#{m.group(3)}"


def command(body: str | None) -> str | None:
    """The first fenced block, else the first line that is wholly `inline code`."""
    text = str(body or "")
    m = _FENCE.search(text) or _CODE_LINE.search(text)
    return m.group(1).strip() if m else None


def _action(decl: Declaration, issue: Mapping[str, Any]) -> Action:
    labels = tuple(sorted(str(lab.get("name", "")) for lab in issue.get("labels", [])
                          if isinstance(lab, Mapping)))
    number = int(issue["number"])
    closed = str(issue.get("state") or "open") == "closed"
    return Action(
        number=number, ref=f"{decl.repo}-I{number}",
        title=str(issue.get("title", "")), url=str(issue.get("html_url", "")),
        labels=labels, done=decl.done_label in labels,
        summary=summary(issue.get("body")),
        created_at=issue.get("created_at"), updated_at=issue.get("updated_at"),
        **dict(zip(("due", "due_source", "when"),
                   timing(issue.get("body"), issue.get("milestone")))),
        **dict(zip(("pr_url", "pr_ref"), pull_request(issue.get("body")))),
        command=command(issue.get("body")),
        closed=closed, closed_at=issue.get("closed_at") if closed else None,
    )


#: The most closed actions the archive shows.
ARCHIVE_SIZE = 30


def _pr_states(decl: Declaration, actions: list[Action], call: Request,
               token: str | None) -> dict[str, str]:
    """``{pr_url: open|merged|closed}`` for every linked PR in the declared
    org the tracker answers for. A PR is an issue to the tracker's API, so the
    issues-read token the page already mints (one per repository) is enough.
    A PR that cannot be read is simply absent: its action stays on the to-do
    list rather than being archived on a guess."""
    base = decl.api_url.rstrip("/")
    tokens: dict[str, str | None] = {}
    out: dict[str, str] = {}
    for a in actions:
        if not a.pr_url or a.pr_url in out:
            continue
        m = _PR_URL.match(a.pr_url)
        if not m or m.group(1) != decl.org:
            continue
        repo, number = m.group(2), m.group(3)
        if repo not in tokens:
            try:
                tokens[repo] = (token if token and repo == decl.repo
                                else _mint(decl.token_command, decl, repo))
            except Exception:  # noqa: BLE001 - unreadable: stays to do
                tokens[repo] = None
        if not tokens[repo]:
            continue
        try:
            issue = call("GET", f"{base}/repos/{decl.org}/{repo}/issues/{number}",
                         tokens[repo], None)
        except Exception:  # noqa: BLE001 - unreadable: stays to do
            continue
        if not isinstance(issue, Mapping):
            continue
        merged = (issue.get("pull_request") or {}).get("merged_at")
        out[a.pr_url] = ("merged" if merged else
                         "closed" if issue.get("state") == "closed" else "open")
    return out


def read(decl: Declaration, now: str, request: Request | None = None,
         token: str | None = None) -> tuple[Listing, AdapterResult]:
    """List every open issue carrying any declared label. A failed read is a
    FAILED source with its reason — never an empty list that reads as
    "nothing to do"."""
    call = request or _http
    base = decl.api_url.rstrip("/")
    try:
        tok = token or _mint(decl.token_command, decl)
        seen: dict[int, Action] = {}
        for label in decl.labels:
            q = urllib.parse.urlencode({"state": "open", "labels": label,
                                        "per_page": MAX_ACTIONS})
            rows = call("GET", f"{base}/repos/{decl.org}/{decl.repo}/issues?{q}", tok, None)
            for issue in rows or []:
                if "pull_request" in issue:
                    continue
                act = _action(decl, issue)
                seen.setdefault(act.number, act)
    except Exception as exc:  # noqa: BLE001 - the reason is the source's
        reason = str(exc)[:240] or type(exc).__name__
        return (Listing(declaration=decl, read_at=now, error=reason),
                AdapterResult(name=SOURCE_NAME, status=AdapterStatus.FAILED,
                              claim_class=ClaimClass.OBSERVATION, fetched_at=now,
                              unavailable=(reason,)))
    states = _pr_states(decl, list(seen.values()), call, tok)
    seen = {n: replace(a, pr_state=states.get(a.pr_url or "")) for n, a in seen.items()}
    # Actionable first; within that, the soonest due date first, undated
    # after, oldest first among equals.
    actions = tuple(sorted(seen.values(), key=lambda a: (
        not a.actionable, a.due is None, a.due or "", a.created_at or "", a.number)))
    archive, archive_error = _archive(decl, call, tok, base)
    return (Listing(declaration=decl, actions=actions, read_at=now,
                    archive=archive, archive_error=archive_error),
            AdapterResult(name=SOURCE_NAME, status=AdapterStatus.OK,
                          claim_class=ClaimClass.OBSERVATION, fetched_at=now))


def _archive(decl: Declaration, call: Request, tok: str,
             base: str) -> tuple[tuple[Action, ...], str | None]:
    """The most recently closed actions, newest first."""
    seen: dict[int, Action] = {}
    try:
        for label in decl.labels:
            q = urllib.parse.urlencode({"state": "closed", "labels": label, "sort": "updated",
                                        "direction": "desc", "per_page": ARCHIVE_SIZE})
            for issue in call("GET", f"{base}/repos/{decl.org}/{decl.repo}/issues?{q}",
                              tok, None) or []:
                if "pull_request" in issue:
                    continue
                act = _action(decl, issue)
                if act.closed:
                    seen.setdefault(act.number, act)
    except Exception as exc:  # noqa: BLE001 - the archive's own reason
        return (), str(exc)[:240] or type(exc).__name__
    ordered = sorted(seen.values(), key=lambda a: a.closed_at or "", reverse=True)
    return tuple(ordered[:ARCHIVE_SIZE]), None


def attach(index: Any, listing: Listing | None) -> None:
    setattr(index, _ATTR, listing)


def listing(index: Any) -> Listing | None:
    return getattr(index, _ATTR, None)


# ------------------------------------------------------------------ write ---

class TickRefused(Exception):
    """A tick the handler will not send, with the reason shown to the reader.
    ``status`` is the HTTP status the refusal is answered with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def comment_text(decl: Declaration, done: bool, actor: str | None) -> str:
    who = actor or "the console's reader"
    if done:
        return (f"Marked done on the console by {who}. "
                f"Next: check it deployed and looks good, then close this with a note.")
    return f"Unticked on the console by {who}: not done yet."


def tick(index: Any, number: int, done: bool, actor: str | None,
         request: Request | None = None, token: str | None = None) -> Action:
    """Add (``done``) or remove the done label on one LISTED action, and say
    so on the issue. Returns the action as it now stands."""
    found = listing(index)
    if found is None:
        raise TickRefused("no actions are declared on this console", 404)
    decl = found.declaration
    if not decl.writable:
        raise TickRefused("this console has no write_token_command; tick on the issue instead", 403)
    current = next((a for a in found.actions if a.number == number), None)
    if current is None:
        raise TickRefused(f"#{number} is not on the actions list", 404)
    call = request or _http
    base = f"{decl.api_url.rstrip('/')}/repos/{decl.org}/{decl.repo}/issues/{number}"
    try:
        tok = token or _mint(decl.write_token_command, decl)
        if done:
            call("POST", f"{base}/labels", tok, {"labels": [decl.done_label]})
        else:
            try:
                call("DELETE", f"{base}/labels/{urllib.parse.quote(decl.done_label, safe='')}",
                     tok, None)
            except RuntimeError as exc:
                if not str(exc).startswith("HTTP 404"):  # already absent is the goal
                    raise
        call("POST", f"{base}/comments", tok, {"body": comment_text(decl, done, actor)})
    except TickRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - shown to the reader, nothing changed here
        raise TickRefused(f"the tracker refused the write: {str(exc)[:200]}", 502) from None
    return mark(index, number, done)


def mark(index: Any, number: int, done: bool) -> Action:
    """Reflect a tick the tracker accepted in the list already read, so the
    page answering the POST shows it. The next rebuild re-reads the tracker
    and replaces this list whole; nothing here outlives that."""
    found = listing(index)
    assert found is not None
    labels_done = found.declaration.done_label
    updated: list[Action] = []
    result: Action | None = None
    for a in found.actions:
        if a.number == number:
            labels = set(a.labels) - {labels_done} | ({labels_done} if done else set())
            a = replace(a, done=done, labels=tuple(sorted(labels)))
            result = a
        updated.append(a)
    found.actions = tuple(updated)
    assert result is not None
    return result


def as_json(found: Listing | None) -> dict[str, Any]:
    if found is None:
        return {"declared": False, "actions": []}
    decl = found.declaration
    return {
        "declared": True, "title": decl.title, "repo": f"{decl.org}/{decl.repo}",
        "labels": list(decl.labels), "done_label": decl.done_label,
        "writable": decl.writable, "read_at": found.read_at, "error": found.error,
        "open": len(found.todo),
        "done": len(found.done),
        "actions": [_action_json(a) for a in found.actions],
        "archive": [_action_json(a) for a in found.archive],
        "archive_error": found.archive_error,
    }


def _action_json(a: Action) -> dict[str, Any]:
    return {"ref": a.ref, "number": a.number, "title": a.title, "url": a.url,
            "done": a.done, "actionable": a.actionable, "summary": a.summary,
            "labels": list(a.labels), "due": a.due, "due_source": a.due_source,
            "when": a.when, "pr_url": a.pr_url, "pr_ref": a.pr_ref,
            "pr_state": a.pr_state, "command": a.command, "closed": a.closed,
            "closed_at": a.closed_at, "created_at": a.created_at,
            "updated_at": a.updated_at}
