"""Board state derivation and persistence tests.

All Baro event input comes from the recorded fixtures via ``load_fixture``,
fed through ``LineBuffer`` in split chunks and ``parse_line``.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from baro_plugin.core.board import Board
from baro_plugin.core.events import LineBuffer, parse_line
from baro_plugin.core.ids import new_run_id
from baro_plugin.core.store import MAX_RUNS, RunRepository

BOARD_KEYS = {
    "schema", "run_id", "goal", "repo", "baro_version", "status", "started_at",
    "ended_at", "stories", "replans", "events_applied", "unparsed_lines",
}
STORY_KEYS = {
    "id", "title", "state", "depends_on", "waits_on", "blocks",
    "blocked_by_failure", "last_progress", "error",
}
REPLAN_KEYS = {"index", "at", "added", "removed", "edges_changed"}
RUN_ID = "baro-20261105T142233Z-a1b2c3"


def new_board(run_id=RUN_ID, status="running"):
    return Board(
        run_id=run_id,
        goal="build it",
        repo="/tmp/repo",
        baro_version="1.2.3",
        status=status,
        started_at="2026-11-05T14:22:33Z",
    )


def feed(board, lines, chunk=7):
    """Feed fixture lines as one stdout stream split every ``chunk`` chars."""
    text = "".join(line + "\n" for line in lines)
    buf = LineBuffer()
    for i in range(0, len(text), chunk):
        for line in buf.feed(text[i:i + chunk]):
            board.apply_line(line)
    for line in buf.flush():
        board.apply_line(line)
    return board


def stories(board):
    return {s["id"]: s for s in board.to_dict()["stories"]}


def test_replan_adds_prereq_to_ready_story_blocks_it(load_fixture):
    lines = load_fixture("run_replan_adds_prereq.jsonl")
    board = new_board()

    # Up to and including story_complete S1: S3 (added by replan #1) is ready.
    feed(board, lines[:4])
    before = stories(board)
    assert before["S1"]["state"] == "done"
    assert before["S3"]["state"] == "ready"
    assert before["S3"]["waits_on"] == []
    assert "S2" not in before

    # Replan #2 adds S2a and rewires S3 onto it.
    feed(board, lines[4:])
    d = board.to_dict()
    after = stories(board)
    assert after["S3"]["state"] == "blocked"
    assert after["S3"]["depends_on"] == ["S2a"]
    assert after["S3"]["waits_on"] == ["S2a"]
    assert after["S3"]["blocked_by_failure"] is False
    assert after["S3"]["title"] == "Three"  # rewired entries keep the title
    assert after["S2a"]["state"] == "ready"
    assert after["S2a"]["title"] == "First half of S2"
    assert after["S2a"]["blocks"] == ["S3"]
    assert after["S1"]["blocks"] == ["S2a"]  # direct undone dependents only
    assert [s["id"] for s in d["stories"]] == ["S1", "S3", "S2a"]
    assert d["status"] == "running"  # stream stops mid-run, no run_end

    assert len(d["replans"]) == 2
    first, second = d["replans"]
    assert set(second) == REPLAN_KEYS
    assert first["index"] == 1 and second["index"] == 2
    assert first["added"] == ["S3"] and first["removed"] == ["S2"]
    assert first["edges_changed"] == [{"from": "S1", "to": "S3"}]
    assert second["added"] == ["S2a"]
    assert second["removed"] == []  # S2 was already gone: a no-op
    assert {"from": "S2a", "to": "S3"} in second["edges_changed"]
    assert {"from": "S1", "to": "S2a"} in second["edges_changed"]
    assert second["at"] is None  # recorded lines carry no ts


def test_replan_records_fallback_time_and_event_ts(load_fixture):
    replan = next(l for l in load_fixture("run_replan_adds_prereq.jsonl") if '"replan"' in l)
    board = feed(new_board(), load_fixture("run_replan_adds_prereq.jsonl")[:2])
    board.apply(parse_line(replan), at="2026-11-05T15:00:00Z")
    assert board.to_dict()["replans"][0]["at"] == "2026-11-05T15:00:00Z"

    obj = json.loads(replan)
    obj["ts"] = "2026-11-05T15:01:00Z"
    board.apply(parse_line(json.dumps(obj)), at="ignored")
    assert board.to_dict()["replans"][1]["at"] == "2026-11-05T15:01:00Z"


def test_failure_blocks_dependents(load_fixture):
    board = feed(new_board(), load_fixture("run_failure_blocks_dependents.jsonl"))
    d = board.to_dict()
    s = stories(board)
    assert s["S1"]["state"] == "done"
    assert s["S2"]["state"] == "failed"
    assert s["S2"]["error"] == "tests failed"
    assert s["S2"]["blocks"] == ["S3"]
    assert s["S2"]["blocked_by_failure"] is False
    # Direct dependent.
    assert s["S3"]["state"] == "blocked"
    assert s["S3"]["waits_on"] == ["S2"]
    assert s["S3"]["blocked_by_failure"] is True
    # Transitive dependent.
    assert s["S4"]["state"] == "blocked"
    assert s["S4"]["waits_on"] == ["S3"]
    assert s["S4"]["blocked_by_failure"] is True
    assert s["S1"]["blocked_by_failure"] is False
    # run_end with success:false.
    assert d["status"] == "failed"
    assert d["ended_at"] is None  # no ts on the recorded line, no fallback given


def test_run_end_success_sets_completed_and_counts_lines(load_fixture):
    board = feed(new_board(status="planning"), load_fixture("run_success.jsonl"))
    d = board.to_dict()
    assert d["status"] == "completed"
    assert d["unparsed_lines"] == 1  # "garbage line"
    assert d["events_applied"] == 4  # init, id-less activity (unknown), complete, done
    assert stories(board)["S1"]["state"] == "done"
    assert stories(board)["S1"]["title"] == ""


def test_plan_only_states_and_progress(load_fixture):
    board = feed(new_board(status="planning"), load_fixture("plan_only.jsonl"))
    d = board.to_dict()
    s = stories(board)
    assert d["status"] == "running"
    assert s["S1"]["state"] == "running"
    assert s["S1"]["last_progress"] == "cargo test"
    assert s["S1"]["blocks"] == ["S2"]
    assert s["S2"]["state"] == "blocked"
    assert s["S2"]["waits_on"] == ["S1"]


def test_unknown_story_ids_are_ignored(load_fixture):
    lines = load_fixture("plan_only.jsonl")
    board = feed(new_board(), lines[:2])
    log = json.loads(lines[5])
    log["id"] = "_git"
    board.apply_line(json.dumps(log))
    assert [s["id"] for s in board.to_dict()["stories"]] == ["S1", "S2"]


def test_to_dict_has_exactly_documented_keys(load_fixture):
    d = feed(new_board(), load_fixture("run_replan_adds_prereq.jsonl")).to_dict()
    assert set(d) == BOARD_KEYS
    assert d["schema"] == 1
    for story in d["stories"]:
        assert set(story) == STORY_KEYS
        assert story["state"] in {"blocked", "ready", "running", "done", "failed"}
    for replan in d["replans"]:
        assert set(replan) == REPLAN_KEYS
        for edge in replan["edges_changed"]:
            assert set(edge) == {"from", "to"}
    json.dumps(d)  # JSON-serialisable


@pytest.mark.parametrize(
    "name",
    ["run_success.jsonl", "run_replan_adds_prereq.jsonl",
     "run_failure_blocks_dependents.jsonl", "plan_only.jsonl"],
)
def test_from_dict_round_trips(load_fixture, name):
    board = feed(new_board(), load_fixture(name))
    d = board.to_dict()
    assert Board.from_dict(d).to_dict() == d
    assert Board.from_dict(json.loads(json.dumps(d))).to_dict() == d


def test_mark_cancelled(load_fixture):
    board = feed(new_board(), load_fixture("plan_only.jsonl"))
    assert board.mark_cancelled(at="2026-11-05T15:00:00Z") is True
    d = board.to_dict()
    assert d["status"] == "cancelled"
    assert d["ended_at"] == "2026-11-05T15:00:00Z"
    assert Board.from_dict(d).to_dict() == d

    # A run that already ended keeps its status; a late run_end does not undo a cancel.
    ended = feed(new_board(), load_fixture("run_success.jsonl"))
    assert ended.mark_cancelled() is False
    assert ended.status == "completed"
    done_line = load_fixture("run_success.jsonl")[-1]
    board.apply_line(done_line)
    assert board.status == "cancelled"


def _run_ids(n):
    start = datetime(2026, 11, 5, 14, 0, 0, tzinfo=timezone.utc)
    return [new_run_id(start + timedelta(seconds=i), lambda i=i: f"{i:06x}") for i in range(n)]


def test_store_caps_runs_and_evicts_oldest_board_and_handle(memory_store):
    repo = RunRepository(memory_store)
    ids = _run_ids(MAX_RUNS + 1)
    for i, run_id in enumerate(ids):
        repo.save_board(new_board(run_id))
        repo.save_handle(run_id, {"process_id": f"proc_{i}", "read_offset": i})
    assert MAX_RUNS == 50

    runs = repo.list_runs()
    assert runs == ids[1:]  # oldest first, capped at 50
    assert memory_store.get("runs") == ids[1:]
    oldest = ids[0]
    assert memory_store.get(f"run:{oldest}") is None
    assert memory_store.get(f"handle:{oldest}") is None
    assert repo.load_board(oldest) is None
    # The evicted run's handle was saved before eviction and is gone now.
    assert repo.load_handle(oldest) is None
    for i, run_id in enumerate(ids[1:], start=1):
        assert memory_store.get(f"run:{run_id}")["run_id"] == run_id
        assert memory_store.get(f"handle:{run_id}") == {"process_id": f"proc_{i}", "read_offset": i}
    # Every stored value is plain JSON.
    for text in memory_store.data.values():
        json.loads(text)


def test_store_round_trips_board_and_handle(memory_store, load_fixture):
    repo = RunRepository(memory_store)
    board = feed(new_board(), load_fixture("run_replan_adds_prereq.jsonl"))
    repo.save_board(board)
    repo.save_board(board)  # re-saving does not duplicate the run id
    assert repo.list_runs() == [RUN_ID]
    assert repo.load_board(RUN_ID).to_dict() == board.to_dict()

    repo.save_handle(RUN_ID, {"process_id": "proc_1", "read_offset": 0})
    repo.save_handle(RUN_ID, {"process_id": "proc_1", "read_offset": 120})
    assert repo.load_handle(RUN_ID) == {"process_id": "proc_1", "read_offset": 120}
    assert repo.load_board("baro-20261105T000000Z-000000") is None
    assert repo.load_handle("baro-20261105T000000Z-000000") is None
    with pytest.raises(ValueError):
        repo.save_handle(RUN_ID, {"process_id": "", "read_offset": 0})
    with pytest.raises(ValueError):
        repo.save_handle(RUN_ID, {"process_id": "p", "read_offset": -1})


def test_memory_store_rejects_non_json(memory_store):
    with pytest.raises(TypeError):
        memory_store.set("x", {1, 2})


def test_dependency_suspension_and_retry_are_not_failures():
    # Seen in Hermes Desktop: a docs story started early, found it needed the
    # others, and paused; the board showed it as Failed.
    lines = [
        '{"type":"init","protocol":3,"project":"p","stories":[{"id":"S1","title":"S1","depends_on":[]},{"id":"S5","title":"Docs","depends_on":[]}],"runner":"m"}',
        '{"type":"story_start","id":"S5","title":"Docs"}',
        '{"type":"story_error","id":"S5","error":"suspended on dependency block block-ff6e","attempt":1,"max_retries":2}',
        '{"type":"story_start","id":"S1","title":"S1"}',
        '{"type":"story_error","id":"S1","error":"execution infrastructure failed in inference round 1","attempt":1,"max_retries":2}',
    ]
    board = feed(new_board(), lines)
    s = stories(board)
    assert s["S5"]["state"] in ("ready", "blocked")
    assert s["S5"]["error"] is None
    assert s["S1"]["state"] == "failed"
    # Baro retries S1: a new start clears the failure.
    board = feed(board, ['{"type":"story_start","id":"S1","title":"S1"}'])
    assert stories(board)["S1"]["state"] == "running"


def test_board_replays_the_mirrored_event_stream():
    from baro_plugin.core.events_file import live_board

    stored = new_board(status="running")
    text = (
        '{"type":"init","protocol":3,"project":"p","stories":[{"id":"S1","title":"S1","depends_on":[]},'
        '{"id":"S2","title":"S2","depends_on":["S1"]}],"runner":"m"}\n'
        '{"type":"story_start","id":"S1","title":"S1"}\n'
        '{"type":"story_complete","id":"S1"}\n'
        '{"type":"story_start","id":"S2"'  # a line still being written
    )
    s = stories(live_board(stored, text))
    assert s["S1"]["state"] == "done"
    assert s["S2"]["state"] == "ready"
    # A finished board is what the store says, not a replay.
    done = new_board(status="cancelled")
    assert live_board(done, text) is done
