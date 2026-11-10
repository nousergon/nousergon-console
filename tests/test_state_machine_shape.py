"""Execution statuses map to the shared state vocabulary without I/O."""
from __future__ import annotations

import pytest

from console.model.kinds import State
from console.state_machine_shape import run_state


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("SUCCEEDED", State.HEALTHY),
        ("FAILED", State.FAILED),
        ("ABORTED", State.FAILED),
        ("TIMED_OUT", State.STALLED),
        ("RUNNING", State.RUNNING),
        ("PENDING", State.RUNNING),
        ("PENDING_REDRIVE", State.RUNNING),
        ("", State.UNREPORTED),
        ("succeeded", State.UNREPORTED),
        ("UNKNOWN", State.UNREPORTED),
    ],
)
def test_run_state(status: str, expected: State) -> None:
    assert run_state(status) is expected
