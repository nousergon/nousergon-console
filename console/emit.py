"""The emission contract — how a process or module gets onto the surface (§2.6).

`console-policy.md` §2.3 says adding a *source* is adding an adapter. **A
process or module is not a source** — it writes into one. If every new module
needed adapter code, onboarding would cost console work forever, and a surface
whose coverage is bounded by how much adapter code somebody felt like writing
will always render a subset **while looking complete**.

So this is the default onboarding path, and it costs nothing in this
repository:

    from console.emit import report, write_json

    write_json(
        report(
            component_id="my-nightly-job",
            status="ok",
            cadence_minutes=1440,
            summary="processed 903 tickers, 0 rejected",
            deep_link="https://ci.example/run/1234",
        ),
        "/var/lib/reports/my-nightly-job/latest.json",
    )

Add the module's row to the registry, point an existing adapter at the
directory or prefix it writes to, and it appears — **with zero edits to this
repository and zero edits to the console's configuration** (§9.8 measures
exactly that, and the acceptance test in `tests/test_emission_contract.py`
forbids itself from touching any file under `console/`).

**Every field is optional with a declared default.** A required field added
later is a fleet-wide breaking change dressed as a schema improvement, and it
lands on every emitter at once — most of which nobody is going to redeploy. A
consumer that needs a value it did not get renders the absence (§5.5) rather
than rejecting the document.

A **bespoke adapter is the exception** and needs a written reason (§12),
reserved for sources the operator does not control: a vendor API, a cloud
control plane, someone else's registry. A source we write to is a source we can
make emit this.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterable, Mapping
from .aws import client as _aws_client

#: The envelope version. Bumped only on a change a consumer must react to;
#: adding an optional field is not one (§12). The previous version stays
#: ingestible for a stated period — an emitter is not obliged to redeploy
#: because the console changed its mind.
SCHEMA_VERSION = 1

#: Where the published JSON Schema lives, relative to this package.
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schemas",
                           "component_report.schema.json")

#: The status vocabulary an emitter may use. Deliberately SMALL and about the
#: run, not about the component: an emitter knows whether its own work
#: succeeded, and it does not know whether it is deprecated, disabled or
#: missing — those are the registry's to declare (`observability-policy.md`
#: §8.3), and inferring them from telemetry is what that section forbids.
STATUSES: tuple[str, ...] = ("ok", "attention", "error")


def report(
    *,
    component_id: str | None = None,
    status: str | None = None,
    ran_at: str | None = None,
    cadence_minutes: float | None = None,
    label: str | None = None,
    summary: str | None = None,
    deep_link: str | None = None,
    findings: Iterable[Any] | None = None,
    facets: Mapping[str, str] | None = None,
    fields: Mapping[str, Mapping[str, Any]] | None = None,
    produces: Iterable[str] | None = None,
    consumes: Iterable[str] | None = None,
    run_id: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Build one component report. Every argument is optional (§2.6).

    - ``component_id`` — the one name every signal, log key, alert and console
      row uses (§3.6). Omitted, the reading adapter falls back to the location
      the document was written to, which is why this is optional rather than
      required: a report at `…/my-job/latest.json` already says who it is about.
    - ``status`` — one of `ok` · `attention` · `error`, about **this run**.
    - ``ran_at`` — ISO-8601. Omitted, the adapter falls back to the object's
      last-modified, which is a weaker fact and is declared as such.
    - ``cadence_minutes`` — how often this is expected to publish. **Without it
      staleness is not computable**, and the console says so rather than
      assuming fresh (§5.2).
    - ``facets`` — the §2.2 slicing dimensions this emitter knows about itself
      (owner, substrate, repo, environment). Not lifecycle: that is declared in
      the registry and never inferred (§8.3).
    - ``fields`` — self-describing extra data, each carrying `type`, `unit`,
      `baseline` and a `render` hint (§5.8). This is how a module's own numbers
      reach the surface without any rendering code that knows about it.
    - ``produces`` / ``consumes`` — artifact keys, which become the §6 lineage
      edges. The **consumes** direction is the one that matters: "who breaks if
      this is stale" exists nowhere unless somebody declares it.
    - ``run_id`` / ``cycle_id`` — this execution and the business period it
      belongs to, so "what else broke when this broke" is a traversal.
    """
    if status is not None and status not in STATUSES:
        raise ValueError(
            f"status {status!r} is not one of {STATUSES}. The vocabulary is "
            "small on purpose: an emitter knows whether its own run succeeded, "
            "and it does not know whether it is disabled, deprecated or "
            "retired — the registry declares those (observability-policy.md "
            "§8.3), and inferring them from telemetry is what §8.3 forbids."
        )

    doc: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for key, value in (
        ("component_id", component_id),
        ("status", status),
        ("ran_at", ran_at),
        ("cadence_minutes", cadence_minutes),
        ("label", label),
        ("summary", summary),
        ("deep_link", deep_link),
        ("run_id", run_id),
        ("cycle_id", cycle_id),
    ):
        if value is not None:
            doc[key] = value
    if findings is not None:
        doc["findings"] = list(findings)
    if facets:
        doc["facets"] = dict(facets)
    if fields:
        doc["fields"] = {k: dict(v) for k, v in fields.items()}
    if produces:
        doc["produces"] = list(produces)
    if consumes:
        doc["consumes"] = list(consumes)
    return doc


def write_json(doc: Mapping[str, Any], path: str) -> str:
    """Write a report to a local path, creating parent directories.

    Atomic by rename, because a reader that catches a half-written document
    renders a parse failure as an unhealthy component — a bug in the emitter
    surfacing as a lie about the fleet.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def write_object(
    doc: Mapping[str, Any],
    bucket: str,
    key: str,
    putter: Callable[[str, str, bytes], None] | None = None,
) -> str:
    """Write a report to an object store.

    ``putter`` is injectable so this is testable with no credentials and no
    network — the same hermeticity rule every adapter follows (`docs/adapters.md`).
    The default uses boto3 from the optional ``aws`` extra.
    """
    payload = json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
    if putter is None:
        putter = _default_putter()
    putter(bucket, key, payload)
    return f"s3://{bucket}/{key}"


def _default_putter() -> Callable[[str, str, bytes], None]:
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise RuntimeError(
            "write_object needs the `aws` extra (pip install "
            "'nousergon-console[aws]'), or pass your own putter"
        ) from exc

    client = _aws_client("s3")

    def _put(bucket: str, key: str, payload: bytes) -> None:
        client.put_object(
            Bucket=bucket, Key=key, Body=payload,
            ContentType="application/json",
        )

    return _put


def load_schema() -> dict[str, Any]:
    """The published JSON Schema, as a dict. Part of the product contract."""
    with open(SCHEMA_PATH) as fh:
        return json.load(fh)
