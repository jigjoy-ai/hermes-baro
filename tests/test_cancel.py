"""baro_cancel tests: process-tree kill through the bridge and the cancelled board.

Baro output comes from the recorded fixtures via ``load_fixture``.
"""

import pytest

from baro_plugin import tools
from baro_plugin.core.store import board_key, handle_key

from fakes import ApprovalDenied, DispatchError, FakeBridge, chunks_by_line


@pytest.fixture
def repo(tmp_path):
    return str(tmp_path)


def start_run(bridge, repo):
    result = tools.handle_baro_run(bridge, {"goal": "Add a health endpoint", "repo": repo})
    assert result["ok"] is True, result
    return result["run_id"]


def sync_to_end(bridge, run_id):
    for _ in range(len(bridge.chunks) + 1):
        assert tools.handle_baro_status(bridge, {"run_id": run_id})["ok"] is True


def test_cancel_running_run_kills_once_and_marks_cancelled(load_fixture, repo):
    bridge = FakeBridge(chunks_by_line(load_fixture("run_replan_adds_prereq.jsonl")), process_id="proc-42")
    run_id = start_run(bridge, repo)
    # Part of the run has been observed: the board is live.
    tools.handle_baro_status(bridge, {"run_id": run_id})
    assert bridge.memory.get(board_key(run_id))["status"] == "running"

    result = tools.handle_baro_cancel(bridge, {"run_id": run_id})
    assert result == {"ok": True, "run_id": run_id, "status": "cancelled", "already_finished": False}
    assert bridge.calls_named("kill") == [("kill", "proc-42")]
    stored = bridge.memory.get(board_key(run_id))
    assert stored["status"] == "cancelled"
    assert stored["ended_at"]
    # Stories keep the state they had; the board itself is cancelled.
    assert [s["id"] for s in stored["stories"]] == ["S1", "S2"]


def test_cancel_twice_does_not_kill_again(repo):
    bridge = FakeBridge()
    run_id = start_run(bridge, repo)
    assert tools.handle_baro_cancel(bridge, {"run_id": run_id})["already_finished"] is False
    result = tools.handle_baro_cancel(bridge, {"run_id": run_id})
    assert result == {"ok": True, "run_id": run_id, "status": "cancelled", "already_finished": True}
    assert len(bridge.calls_named("kill")) == 1


@pytest.mark.parametrize(
    "fixture, status",
    [("run_success.jsonl", "completed"), ("run_failure_blocks_dependents.jsonl", "failed")],
)
def test_cancel_finished_run_does_not_kill(load_fixture, repo, fixture, status):
    bridge = FakeBridge(chunks_by_line(load_fixture(fixture)), exit_at_end=True)
    run_id = start_run(bridge, repo)
    sync_to_end(bridge, run_id)
    before = bridge.memory.get(board_key(run_id))
    assert before["status"] == status

    result = tools.handle_baro_cancel(bridge, {"run_id": run_id})
    assert result == {"ok": True, "run_id": run_id, "status": status, "already_finished": True}
    assert bridge.calls_named("kill") == []
    assert bridge.memory.get(board_key(run_id)) == before


@pytest.mark.parametrize(
    "kill_error, code",
    [(ApprovalDenied("user declined"), "approval_denied"), (DispatchError("no such process"), "dispatch_failed")],
)
def test_cancel_kill_failure_leaves_run_running(repo, kill_error, code):
    bridge = FakeBridge(kill_error=kill_error)
    run_id = start_run(bridge, repo)
    result = tools.handle_baro_cancel(bridge, {"run_id": run_id})
    assert result["ok"] is False
    assert result["error"] == code
    assert "may still be running" in result["message"]
    assert len(bridge.calls_named("kill")) == 1
    assert bridge.memory.get(board_key(run_id))["status"] == "running"
    assert bridge.memory.get(handle_key(run_id))["process_id"] == "proc-1"


def test_cancel_invalid_and_unknown_run_id():
    bridge = FakeBridge()
    bad = tools.handle_baro_cancel(bridge, {"run_id": "run-1"})
    assert bad["error"] == "invalid_argument"
    unknown = tools.handle_baro_cancel(bridge, {"run_id": "baro-20261105T142233Z-a1b2c3"})
    assert unknown["error"] == "unknown_run"
    assert bridge.calls_named("kill") == []
