"""The state-machine execution-status SHAPE — read once, used at two layers.

`run_state()` is the Step Functions execution-status -> thirteen-state mapping,
lifted out of `console/adapters/state_machine.py` so both the adapter and the
`state-machine` **driver** (`console/drivers/state_machine.py`,
`nousergon-console#99`) import the same function rather than forking it — the
identical pattern `console/records_shape.py` set for the `s3-records`
adapter/driver pair (`nousergon-console#98`).

Everything here is pure: no I/O, no AWS. Both callers own their own source
access (an adapter's configured list of ARNs vs. a driver's one descriptor-
declared ARN) and pass this module only the source's own execution status
string.
"""
from __future__ import annotations

from .model.kinds import State


def run_state(status: str) -> State:
    """Map the source's own execution status onto the closed vocabulary.

    The source's statement, never a guessed verdict (§2.3).

    ``TIMED_OUT`` is STALLED rather than FAILED: it started and nothing
    reported an ending, which is exactly §8.3's distinction — retry logic and
    diagnosis differ from a run that stopped.

    ``RUNNING``/``PENDING``/``PENDING_REDRIVE`` map to `State.RUNNING`
    (`alpha-engine-config-I6358`): they have neither failed nor finished, and
    §8.3 added a thirteenth state for exactly this rather than collapsing an
    in-flight execution into HEALTHY (claims an ending that has not happened)
    or STALLED (claims a stale heartbeat before one exists) or UNREPORTED
    (would put every normal execution on the exception list and inflate the
    transparency-gap count, whose objective is zero). Before this state
    existed, a long-running execution that would eventually fail rendered
    green for its entire duration — that defect is what this mapping fixes.
    """
    if status == "SUCCEEDED":
        return State.HEALTHY
    if status in ("FAILED", "ABORTED"):
        return State.FAILED
    if status == "TIMED_OUT":
        return State.STALLED
    if status in ("RUNNING", "PENDING", "PENDING_REDRIVE"):
        return State.RUNNING
    return State.UNREPORTED
