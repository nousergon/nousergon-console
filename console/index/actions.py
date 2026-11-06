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

**The label is the record; the comment is its note.** A tick is confirmed
when the tracker accepts the label write, and only then. The comment that
says who ticked is posted AFTER it — on the server, off the request thread
(`in_background`), one at a time in the order the ticks were accepted, so a
tick and a quick untick never land their notes out of order. A comment that
still fails after one retry is logged and never un-confirms the label: the
label is already on the issue, and telling the reader "not saved" about a
write that happened is the wrong answer (it was, before 2026-11-05, when the
reader waited on both writes and a page reload for every tick).

The rendering lives in `render/html.py::actions_page`; the route is
`/actions` (`server/router.py`), served in both representations (§3.8).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from ..model.envelope import AdapterResult, AdapterStatus, ClaimClass

_LOG = logging.getLogger(__name__)

SOURCE_NAME = "your-actions"
ACTIONS_QUESTION = "What is waiting on me to do, and which have I already done?"
_ATTR = "_console_actions"
_LABEL = re.compile(r"^[^\s,][^,]{0,49}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_TIMEOUT_SECONDS = 20
_TOKEN_TIMEOUT_SECONDS = 60
#: The summary shown under an action's title: its body's first paragraph.
SUMMARY_CHARS = 280
#: The host API's page size for one listing read.
MAX_ACTIONS = 100
#: The most pages one label's read follows. A read that still finds a full
#: page after this many says so (`Listing.truncated`) rather than dropping the
#: rest silently (I13239, codex e978e70a0e90aa2c).
MAX_PAGES = 10


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
    #: The ask IS merging its PR (a merge-ask issue), so the PR merging or
    #: closing ends it. Any other PR an issue names is context: its ask (a
    #: command, a decision) is still owed after that PR merges.
    merge_ask: bool = False
    #: Every PR the issue links, in order, as (url, "repo#N", state-or-None).
    prs: tuple[tuple[str, str, str | None], ...] = ()
    #: Every command the issue gives to run, in order (`commands`).
    commands: tuple[str, ...] = ()
    #: The prose line before each command (where and why it runs), parallel to `commands`.
    command_labels: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        """Waiting on the reader: open, not ticked, and not about a PR that
        has already merged or closed (Brian 2026-11-05: "actionable only")."""
        return not (self.done or self.closed
                    or (self.merge_ask and self.pr_state in ("merged", "closed")))


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
    #: True when some label had more open issues than `MAX_PAGES` pages hold.
    truncated: bool = False

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
#: Emphasis only. A timing bound is copied, not summarised: `>` and `#` are
#: its content ("> 16:30 ET", "after PR #12") (I13239, codex 942684eb6a5de678).
_WHEN_NOISE = re.compile(r"[*_`]+")


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
        when = " ".join(_WHEN_NOISE.sub("", m.group("text")).split())
        if len(when) > WHEN_CHARS:
            # Cut visibly: a bound silently shortened reads as the whole bound.
            when = when[:WHEN_CHARS].rsplit(" ", 1)[0] + "…"
        when = when or None
    return due, source, when


_PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
# `\r?\n`: an issue body edited in a browser arrives with CRLF line endings
# (I13239, codex ee3c591006774d77).
_FENCE = re.compile(r"```[\w-]*\r?\n(.+?)\r?\n```", re.DOTALL)
_CODE_LINE = re.compile(r"^\s*`([^`\n]{4,})`\s*$", re.MULTILINE)


def pull_request(body: str | None) -> tuple[str | None, str | None]:
    """(url, "repo#N") of the first PR the body links, or (None, None).
    Brian 2026-11-04: the page shows PRs and commands, not the issues behind them."""
    m = _PR_URL.search(str(body or ""))
    if not m:
        return None, None
    return m.group(0), f"{m.group(2)}#{m.group(3)}"


#: Which sign-in a profile's browser prompt wants (Brian 2026-10-27: say which login).
LOGIN_LABELS = {"ne-admin": "PROD login (user cipher813)"}

_PROFILE = re.compile(r"--profile[ =]([\w.-]+)")


def complete(cmd: str) -> str:
    """The command as Brian pastes it, from a fresh terminal (Brian 2026-11-06: "all
    commands … should include all commands, including the login commands and the
    directory navigation commands"). Each AWS profile it uses is signed in first,
    only when its session has expired; a command that never changes directory starts
    from home. A command that already does either is left as written."""
    cmd = cmd.strip()
    pre: list[str] = []
    if not re.search(r"(^|&&|;|\n)\s*cd\s", cmd) and not re.search(r"(^|&&|;|\n|\|)\s*git\s", cmd):
        pre.append("cd ~")
    for prof in dict.fromkeys(_PROFILE.findall(cmd)):
        if not re.search(rf"aws\s+sso\s+login\b[^\n&;]*--profile[ =]{re.escape(prof)}\b", cmd):
            pre.append(f"(aws sts get-caller-identity --profile {prof} >/dev/null 2>&1 "
                       f"|| aws sso login --profile {prof})")
    if not pre:
        return cmd
    if "\n" in cmd:
        return "\n".join(pre) + "\n" + cmd
    return " && ".join([*pre, cmd])


def logins(cmd: str) -> tuple[str, ...]:
    """Which sign-ins a command may open in the browser, named for Brian."""
    return tuple(LOGIN_LABELS.get(p, f"AWS login for profile {p}")
                 for p in dict.fromkeys(_PROFILE.findall(cmd)))


def command(body: str | None) -> str | None:
    """The first fenced block, else the first line that is wholly `inline code`."""
    text = str(body or "")
    m = _FENCE.search(text) or _CODE_LINE.search(text)
    return m.group(1).strip() if m else None


#: The most PRs one row lists.
MAX_PRS = 6

#: A line that starts a shell command. A fenced block is a command only when
#: every line of it is one (or continues one): an issue also fences evidence
#: (`LastModified ...`, JSON, logs), which must never be offered to paste.
_CMD_START = re.compile(
    r"^(?:[A-Z_][A-Z0-9_]*=\S*\s+)*(?:sudo\s+)?(?:aws|gcloud|gsutil|gh|git|bash|sh|zsh|cd|cp|mv|"
    r"open|launchctl|python3?|pip3?|curl|tofu|terraform|kubectl|docker|make|npm|npx|"
    r"ssh|scp|export|source|\.|chmod|mkdir|ls|cat|echo|jq|brew|uv|claude|codex)\b")


def _is_command_block(block: str) -> bool:
    lines = [ln.rstrip() for ln in block.strip().splitlines() if ln.strip()
             and not ln.lstrip().startswith("#")]
    if not lines:
        return False
    cont = False
    for ln in lines:
        if not cont and not _CMD_START.match(ln.strip()):
            return False
        cont = ln.endswith(("\\", "&&", "|", "||"))
    return True


def commands(body: str | None) -> tuple[str, ...]:
    """Every command block in the body, in order: fenced blocks that are wholly
    commands, and lines that are wholly `inline code` naming one."""
    return tuple(cmd for _, cmd in command_steps(body))


_LABEL_MAX = 240


def _label(gap: str) -> str:
    """The last line of prose before a command: where and why it runs (Brian
    2026-11-06: a box command must say it runs on the box)."""
    lines = [ln.strip() for ln in gap.splitlines() if ln.strip()]
    if not lines:
        return ""
    ln = re.sub(r"[*_`]", "", lines[-1]).strip()
    return ln if len(ln) <= _LABEL_MAX else ln[:_LABEL_MAX - 1].rstrip() + "…"


def command_steps(body: str | None) -> tuple[tuple[str, str], ...]:
    """``(label, command)`` for every command block, the label being the line of
    prose just before it (empty when the command follows another directly)."""
    text = str(body or "")
    found: list[tuple[int, str]] = []
    for m in _FENCE.finditer(text):
        if _is_command_block(m.group(1)):
            found.append((m.start(), m.group(1).strip()))
    fenced = [(m.start(), m.end()) for m in _FENCE.finditer(text)]
    for m in _CODE_LINE.finditer(text):
        inside = any(a <= m.start() < b for a, b in fenced)
        if not inside and _is_command_block(m.group(1)):
            found.append((m.start(), m.group(1).strip()))
    ends = {m.start(): m.end() for m in _FENCE.finditer(text)}
    ends.update({m.start(): m.end() for m in _CODE_LINE.finditer(text)})
    out: list[tuple[str, str]] = []
    prev_end = 0
    for start, cmd in sorted(found):
        label = _label(text[prev_end:start])
        prev_end = ends.get(start, start)
        # Only an IMMEDIATE repeat is a duplicate quote. The same command again
        # after a different step is a step of its own, and dropping it changes
        # the procedure "Run, in order" presents (I13239, codex 88ec14d066fdbf57).
        if not out or out[-1][1] != cmd:
            out.append((label, cmd))
    return tuple(out)


def pull_requests(body: str | None) -> tuple[tuple[str, str], ...]:
    """Every distinct PR the body links, in order, as (url, "repo#N")."""
    out: list[tuple[str, str]] = []
    for m in _PR_URL.finditer(str(body or "")):
        pr = (m.group(0), f"{m.group(2)}#{m.group(3)}")
        if pr not in out:
            out.append(pr)
    return tuple(out[:MAX_PRS])


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
        command=(lambda c: complete(c) if c else None)(command(issue.get("body"))),
        closed=closed, closed_at=issue.get("closed_at") if closed else None,
        merge_ask=_is_merge_ask(issue),
        prs=tuple((u, r, None) for u, r in pull_requests(issue.get("body"))),
        commands=tuple(complete(c) for _, c in command_steps(issue.get("body"))),
        command_labels=tuple(lab for lab, _ in command_steps(issue.get("body"))),
    )


_MERGE_ASK = re.compile(r"<!--\s*merge-ask:", re.IGNORECASE)


def _is_merge_ask(issue: Mapping[str, Any]) -> bool:
    """A merge ask: the hourly filer's marker, or a title that says so."""
    return bool(_MERGE_ASK.search(str(issue.get("body") or ""))
                or re.match(r"\s*merge\b", str(issue.get("title") or ""), re.IGNORECASE))


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
    urls = [u for a in actions for u, _, _ in a.prs]
    for url in urls:
        if url in out:
            continue
        m = _PR_URL.match(url)
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
        out[url] = ("merged" if merged else
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
        truncated = False
        for label in decl.labels:
            for page in range(1, MAX_PAGES + 1):
                q = urllib.parse.urlencode({"state": "open", "labels": label,
                                            "per_page": MAX_ACTIONS, "page": page})
                rows = call("GET", f"{base}/repos/{decl.org}/{decl.repo}/issues?{q}",
                            tok, None) or []
                for issue in rows:
                    if "pull_request" in issue:
                        continue
                    act = _action(decl, issue)
                    seen.setdefault(act.number, act)
                if len(rows) < MAX_ACTIONS:
                    break
            else:
                truncated = True
    except Exception as exc:  # noqa: BLE001 - the reason is the source's
        reason = str(exc)[:240] or type(exc).__name__
        return (Listing(declaration=decl, read_at=now, error=reason),
                AdapterResult(name=SOURCE_NAME, status=AdapterStatus.FAILED,
                              claim_class=ClaimClass.OBSERVATION, fetched_at=now,
                              unavailable=(reason,)))
    states = _pr_states(decl, list(seen.values()), call, tok)
    seen = {n: replace(a, pr_state=states.get(a.pr_url or ""),
                       prs=tuple((u, r, states.get(u)) for u, r, _ in a.prs))
            for n, a in seen.items()}
    # Actionable first; within that, the soonest due date first, undated
    # after, oldest first among equals.
    actions = tuple(sorted(seen.values(), key=lambda a: (
        not a.actionable, a.due is None, a.due or "", a.created_at or "", a.number)))
    archive, archive_error = _archive(decl, call, tok, base)
    return (Listing(declaration=decl, actions=actions, read_at=now,
                    archive=archive, archive_error=archive_error, truncated=truncated),
            AdapterResult(name=SOURCE_NAME, status=AdapterStatus.OK,
                          claim_class=ClaimClass.OBSERVATION, fetched_at=now))


def _archive(decl: Declaration, call: Request, tok: str,
             base: str) -> tuple[tuple[Action, ...], str | None]:
    """The most recently closed actions, newest first."""
    seen: dict[int, Action] = {}
    try:
        for label in decl.labels:
            # The API cannot sort by closure, so read by update time (never
            # earlier than closure) until no unread issue can have closed
            # after the newest ARCHIVE_SIZE already held. The first
            # ARCHIVE_SIZE by update time were not the last ARCHIVE_SIZE
            # closed (I13239, codex eb6b5e9f7f84f623).
            for page in range(1, MAX_PAGES + 1):
                q = urllib.parse.urlencode({"state": "closed", "labels": label,
                                            "sort": "updated", "direction": "desc",
                                            "per_page": MAX_ACTIONS, "page": page})
                rows = call("GET", f"{base}/repos/{decl.org}/{decl.repo}/issues?{q}",
                            tok, None) or []
                for issue in rows:
                    if "pull_request" in issue:
                        continue
                    act = _action(decl, issue)
                    if act.closed:
                        seen.setdefault(act.number, act)
                if len(rows) < MAX_ACTIONS:
                    break
                closed = sorted((a.closed_at or "" for a in seen.values()), reverse=True)
                if (len(closed) >= ARCHIVE_SIZE
                        and str(rows[-1].get("updated_at") or "") < closed[ARCHIVE_SIZE - 1]):
                    break
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


#: Runs a tick's follow-up (its comment) later; returns nothing useful.
Defer = Callable[[Callable[[], None]], Any]

#: One worker, so comments land in the order their labels were accepted.
_COMMENTS: ThreadPoolExecutor | None = None
_COMMENTS_LOCK = threading.Lock()
#: `mark` replaces a listing's tuple; two ticks at once must not lose one.
_MARK_LOCK = threading.Lock()


def in_background(work: Callable[[], None]) -> Future:
    """Queue ``work`` on the process's one comment worker."""
    global _COMMENTS
    with _COMMENTS_LOCK:
        if _COMMENTS is None:
            _COMMENTS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="action-comment")
        return _COMMENTS.submit(work)


def wait_for_comments(timeout: float = 10.0) -> None:
    """Block until every comment queued so far has been tried (tests, shutdown)."""
    in_background(lambda: None).result(timeout=timeout)


def _say(call: Request, url: str, tok: str, body: str, number: int) -> None:
    """Post the tick's comment, retrying once. A failure is logged: the label
    it describes is already on the issue and stays the record."""
    for attempt in (1, 2):
        try:
            call("POST", url, tok, {"body": body})
            return
        except Exception as exc:  # noqa: BLE001 - logged, never raised
            if attempt == 2:
                _LOG.warning("tick on #%s: label written, comment not posted: %s",
                             number, str(exc)[:200])


def tick(index: Any, number: int, done: bool, actor: str | None,
         request: Request | None = None, token: str | None = None,
         defer: Defer | None = None) -> Action:
    """Add (``done``) or remove the done label on one LISTED action, and say
    so on the issue. Returns the action as it now stands.

    Returns once the LABEL write is accepted. The comment is posted through
    ``defer`` when one is given (the server passes `in_background`), else
    before returning; either way its failure never fails the tick."""
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
    except TickRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - shown to the reader, nothing changed here
        raise TickRefused(f"the tracker refused the write: {str(exc)[:200]}", 502) from None
    act = mark(index, number, done)
    text = comment_text(decl, done, actor)
    say = lambda: _say(call, f"{base}/comments", tok, text, number)  # noqa: E731
    if defer is None:
        say()
    else:
        defer(say)
    return act


def mark(index: Any, number: int, done: bool) -> Action:
    """Reflect a tick the tracker accepted in the list already read, so the
    page answering the POST shows it. The next rebuild re-reads the tracker
    and replaces this list whole; nothing here outlives that."""
    found = listing(index)
    assert found is not None
    labels_done = found.declaration.done_label
    with _MARK_LOCK:
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
        "truncated": found.truncated,
    }


def _action_json(a: Action) -> dict[str, Any]:
    return {"ref": a.ref, "number": a.number, "title": a.title, "url": a.url,
            "done": a.done, "actionable": a.actionable, "summary": a.summary,
            "labels": list(a.labels), "due": a.due, "due_source": a.due_source,
            "when": a.when, "pr_url": a.pr_url, "pr_ref": a.pr_ref,
            "pr_state": a.pr_state, "merge_ask": a.merge_ask,
            "command": a.command, "commands": list(a.commands), "command_labels": list(a.command_labels),
            "prs": [{"url": u, "ref": r, "state": st} for u, r, st in a.prs],
            "closed": a.closed,
            "closed_at": a.closed_at, "created_at": a.created_at,
            "updated_at": a.updated_at}
