"""Tool handler tests (tools.py) over FakeBridge and MemoryStore.

All Baro output comes from the recorded fixtures via ``load_fixture``: whole
fixtures, or fixtures with lines dropped to model a different ordering.
"""

import json
import re
import shlex

import pytest

from baro_plugin import tools
from baro_plugin.core.board import Board
from baro_plugin.core.command import (
    BARO_SPEC,
    BARO_VERSION,
    LAUNCHER,
    MAX_GOAL_CHARS,
    build_command,
)
from baro_plugin.core.ids import RUN_ID_RE
from baro_plugin.core.store import board_key, handle_key

from fakes import ApprovalDenied, DispatchError, FakeBridge, chunks_by_line, chunks_mid_line, stdout_of

STATES = {"blocked", "ready", "running", "done", "failed"}


class FakeClock:
    """Simulated monotonic clock; ``sleep`` advances it instead of waiting."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.sleeps += 1
        assert self.sleeps < 100_000, "handler never stopped polling"
        self.now += secs


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(tools, "_monotonic", c.monotonic)
    monkeypatch.setattr(tools, "_sleep", c.sleep)
    return c


@pytest.fixture
def repo(tmp_path):
    return str(tmp_path)


def goal_args(repo, **extra):
    return {"goal": "Add a health endpoint", "repo": repo, **extra}


def start_run(bridge, repo):
    result = tools.handle_baro_run(bridge, goal_args(repo))
    assert result["ok"] is True, result
    return result["run_id"]


def sync_all(bridge, run_id):
    """Call baro_status once per scripted chunk (plus one), recording boards and offsets."""
    boards, offsets = [], []
    for _ in range(len(bridge.chunks) + 1):
        result = tools.handle_baro_status(bridge, {"run_id": run_id})
        assert result["ok"] is True, result
        boards.append(result["board"])
        offsets.append(bridge.memory.get(handle_key(run_id))["read_offset"])
    return boards, offsets


def by_id(board):
    return {s["id"]: s for s in board["stories"]}


# ------------------------------------------------------------------ schemas


def test_tool_schemas_cover_the_four_tools():
    assert set(tools.TOOL_SCHEMAS) == set(tools.HANDLERS) == {
        "baro_plan",
        "baro_run",
        "baro_status",
        "baro_cancel",
    }
    for name, schema in tools.TOOL_SCHEMAS.items():
        assert schema["name"] == name
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"
        assert set(schema["parameters"]["required"]) <= set(schema["parameters"]["properties"])


# ---------------------------------------------------------------- baro_plan


def test_plan_returns_stories_and_edges(load_fixture, repo, clock):
    bridge = FakeBridge(chunks_by_line(load_fixture("plan_only.jsonl")))
    result = tools.handle_baro_plan(bridge, goal_args(repo))
    assert result == {
        "ok": True,
        "goal": "Add a health endpoint",
        "repo": repo,
        "baro_version": BARO_VERSION,
        "stories": [
            {"id": "S1", "title": "First story", "depends_on": []},
            {"id": "S2", "title": "Second story", "depends_on": ["S1"]},
        ],
        "edges": [{"from": "S1", "to": "S2"}],
        "native_plan_only": False,
        "stories_started": 0,
    }
    # One edge per depends_on entry.
    assert len(result["edges"]) == sum(len(s["depends_on"]) for s in result["stories"])
    [start] = bridge.calls_named("start_background")
    assert start == ("start_background", build_command("Add a health endpoint", repo), repo)


def test_plan_kills_after_first_plan_event(load_fixture, repo, clock):
    lines = load_fixture("plan_only.jsonl")
    bridge = FakeBridge(chunks_by_line(lines))
    result = tools.handle_baro_plan(bridge, goal_args(repo))
    assert result["ok"] is True
    assert result["stories_started"] == 0
    assert bridge.calls_named("kill") == [("kill", "proc-1")]
    # Reading stopped at the chunk holding the plan (line 2); the kill is the
    # last call, and the story_start line (line 4) was never read.
    assert len(bridge.calls_named("read_output")) == 2
    assert bridge.dispatches[-1] == ("kill", "proc-1")
    assert bridge.delivered == stdout_of(lines[:2])
    assert "story_start" not in bridge.delivered


def test_plan_story_start_seen_before_plan(load_fixture, repo, clock):
    lines = load_fixture("plan_only.jsonl")
    # Drop the init (plan) line: Baro's story_start comes before any plan.
    bridge = FakeBridge(chunks_by_line(lines[:1] + lines[2:]))
    result = tools.handle_baro_plan(bridge, goal_args(repo))
    assert result["ok"] is False
    assert result["error"] == "dispatch_failed"
    assert "began executing" in result["message"]
    assert bridge.calls_named("kill") == [("kill", "proc-1")]


def test_plan_kills_after_first_plan_event_ignores_later_lines_in_same_read(
    load_fixture, repo, clock, monkeypatch
):
    # The whole stream arrives in one read: the plan, then dag, story_start,
    # routed and activity. The handler stops consuming at the plan: it kills
    # Baro at once and reports the plan, and the later story_start is never
    # consumed.
    lines = load_fixture("plan_only.jsonl")
    assert any('"story_start"' in line for line in lines[2:])
    bridge = FakeBridge([stdout_of(lines)])
    consumed = []
    real_parse = tools.parse_line

    def recording_parse(line):
        consumed.append(line)
        return real_parse(line)

    monkeypatch.setattr(tools, "parse_line", recording_parse)
    result = tools.handle_baro_plan(bridge, goal_args(repo))
    assert result["ok"] is True, result
    assert result["stories_started"] == 0
    assert [s["id"] for s in result["stories"]] == ["S1", "S2"]
    assert result["edges"] == [{"from": "S1", "to": "S2"}]
    # Kill is immediate: exactly one read, then the kill, and nothing after.
    assert bridge.dispatches == [
        ("start_background", build_command("Add a health endpoint", repo), repo),
        ("read_output", "proc-1", 0),
        ("kill", "proc-1"),
    ]
    # Only the lines up to and including the plan (init) line were parsed.
    assert consumed == lines[:2]


def test_plan_timeout(load_fixture, repo, clock):
    lines = load_fixture("plan_only.jsonl")
    # Only plan_ready and dag: no plan event ever arrives, and the process never exits.
    bridge = FakeBridge(chunks_by_line([lines[0], lines[2]]))
    result = tools.handle_baro_plan(bridge, goal_args(repo))
    assert result["ok"] is False
    assert result["error"] == "plan_timeout"
    assert clock.now >= tools.PLAN_TIMEOUT_SECS
    assert clock.now < tools.PLAN_TIMEOUT_SECS + 1
    assert bridge.calls_named("kill") == [("kill", "proc-1")]


def test_plan_exit_without_plan_is_dispatch_failed(load_fixture, repo, clock):
    lines = load_fixture("plan_only.jsonl")
    bridge = FakeBridge(chunks_by_line([lines[0]]), exit_at_end=True)
    result = tools.handle_baro_plan(bridge, goal_args(repo))
    assert result["error"] == "dispatch_failed"
    assert "exited" in result["message"]
    assert bridge.calls_named("kill") == []


def test_plan_kill_failure_reports_process_may_still_run(load_fixture, repo, clock):
    bridge = FakeBridge(
        chunks_by_line(load_fixture("plan_only.jsonl")), kill_error=DispatchError("terminal busy")
    )
    result = tools.handle_baro_plan(bridge, goal_args(repo))
    assert result["ok"] is False
    assert result["error"] == "dispatch_failed"
    assert "may still be running" in result["message"]


def test_plan_approval_denied(repo, clock):
    bridge = FakeBridge(start_error=ApprovalDenied("user declined"))
    result = tools.handle_baro_plan(bridge, goal_args(repo))
    assert result["ok"] is False
    assert result["error"] == "approval_denied"
    assert result["message"]
    # No retry and nothing else dispatched.
    assert [c[0] for c in bridge.dispatches] == ["start_background"]


# ----------------------------------------------------------------- baro_run


def test_run_starts_background_and_returns_id(repo):
    bridge = FakeBridge()
    result = tools.handle_baro_run(bridge, goal_args(repo))
    command = build_command("Add a health endpoint", repo)
    assert result["ok"] is True
    assert re.fullmatch(RUN_ID_RE, result["run_id"])
    assert result["status"] == "running"
    assert result["baro_version"] == BARO_VERSION
    assert result["command"] == command
    assert bridge.calls_named("start_background") == [("start_background", command, repo)]
    assert bridge.calls_named("read_output") == []
    run_id = result["run_id"]
    stored = bridge.memory.get(board_key(run_id))
    assert stored["status"] == "running"
    assert stored["run_id"] == run_id
    assert stored["goal"] == "Add a health endpoint"
    assert stored["started_at"]
    assert bridge.memory.get(handle_key(run_id)) == {"process_id": "proc-1", "read_offset": 0}
    assert bridge.memory.get("runs") == [run_id]


def test_run_passes_optional_flags(repo):
    bridge = FakeBridge()
    extra = {"llm": "openai", "openai_base_url": "http://localhost:1/v1", "model": "m"}
    result = tools.handle_baro_run(bridge, goal_args(repo, **extra))
    assert result["command"] == build_command("Add a health endpoint", repo, **extra)


def test_run_build_command_pinned(repo):
    # baro_run dispatches exactly the pinned command from core.command, never a
    # bare or floating package spec.
    bridge = FakeBridge()
    result = tools.handle_baro_run(bridge, goal_args(repo))
    argv = shlex.split(result["command"])
    assert argv[:4] == [LAUNCHER, "--yes", BARO_SPEC, "--headless"]
    assert BARO_SPEC.endswith("@" + BARO_VERSION)
    assert re.fullmatch(r"\d+\.\d+\.\d+", BARO_VERSION)
    assert [c[1] for c in bridge.calls_named("start_background")] == [result["command"]]


def test_run_and_status_persist_through_store(load_fixture, repo):
    # Every value written through RunRepository is plain JSON, and the stored
    # board round-trips through Board.from_dict.
    lines = load_fixture("run_success.jsonl")
    bridge = FakeBridge(chunks_by_line(lines), exit_at_end=True)
    run_id = start_run(bridge, repo)
    sync_all(bridge, run_id)
    assert bridge.memory.get("runs") == [run_id]
    for key in ("runs", board_key(run_id), handle_key(run_id)):
        value = bridge.memory.get(key)
        assert json.loads(json.dumps(value)) == value
    stored = bridge.memory.get(board_key(run_id))
    assert stored["status"] == "completed"
    assert Board.from_dict(stored).to_dict() == stored
    assert bridge.memory.get(handle_key(run_id))["read_offset"] == len(stdout_of(lines))


def test_run_unattended_returns_error_without_start(repo):
    bridge = FakeBridge(unattended=True)
    result = tools.handle_baro_run(bridge, goal_args(repo))
    assert result["ok"] is False
    assert result["error"] == "unattended_context"
    assert bridge.dispatches == []
    assert bridge.memory.data == {}


@pytest.mark.parametrize(
    "make_args",
    [
        lambda repo: {"goal": "", "repo": repo},
        lambda repo: {"goal": "   ", "repo": repo},
        lambda repo: {"repo": repo},
        lambda repo: {"goal": "x" * (MAX_GOAL_CHARS + 1), "repo": repo},
        lambda repo: {"goal": "a\x00b", "repo": repo},
        lambda repo: {"goal": "--help", "repo": repo},
        lambda repo: {"goal": "g", "repo": "relative/path"},
        lambda repo: {"goal": "g", "repo": repo + "/missing"},
        lambda repo: {"goal": "g"},
        lambda repo: {"goal": "g", "repo": repo, "model": 3},
        lambda repo: {"goal": "g", "repo": repo, "llm": "a\x00"},
        lambda repo: {"goal": "g", "repo": repo, "model": "-x"},
        lambda repo: "not an object",
    ],
)
@pytest.mark.parametrize("handler", [tools.handle_baro_run, tools.handle_baro_plan])
def test_run_invalid_argument(handler, make_args, repo):
    bridge = FakeBridge()
    result = handler(bridge, make_args(repo))
    assert result["ok"] is False
    assert result["error"] == "invalid_argument"
    assert result["message"]
    assert bridge.dispatches == []


def test_run_approval_denied_saves_nothing(repo):
    bridge = FakeBridge(start_error=ApprovalDenied("user declined"))
    result = tools.handle_baro_run(bridge, goal_args(repo))
    assert result["ok"] is False
    assert result["error"] == "approval_denied"
    assert result["message"]
    assert len(bridge.calls_named("start_background")) == 1
    assert bridge.memory.data == {}


def test_run_dispatch_error_saves_nothing(repo):
    bridge = FakeBridge(start_error=DispatchError("terminal tool unavailable"))
    result = tools.handle_baro_run(bridge, goal_args(repo))
    assert result["error"] == "dispatch_failed"
    assert bridge.memory.data == {}


# -------------------------------------------------------------- baro_status


def test_status_replan_adds_prereq_with_mid_line_chunks(load_fixture, repo):
    lines = load_fixture("run_replan_adds_prereq.jsonl")
    bridge = FakeBridge(chunks_mid_line(lines))
    run_id = start_run(bridge, repo)
    boards, offsets = sync_all(bridge, run_id)

    final = boards[-1]
    for story in final["stories"]:
        assert story["state"] in STATES
        assert isinstance(story["waits_on"], list) and isinstance(story["blocks"], list)
    stories = by_id(final)
    assert set(stories) == {"S1", "S3", "S2a"}
    assert stories["S1"]["state"] == "done"
    assert stories["S2a"]["state"] == "ready"
    # S3 was ready (S1 done) until the second replan gave it prerequisite S2a.
    assert any(by_id(b).get("S3", {}).get("state") == "ready" for b in boards[:-1])
    assert stories["S3"]["state"] == "blocked"
    assert stories["S3"]["waits_on"] == ["S2a"]
    assert stories["S2a"]["blocks"] == ["S3"]

    assert [r["index"] for r in final["replans"]] == [1, 2]
    second = final["replans"][1]
    assert set(second) == {"index", "at", "added", "removed", "edges_changed"}
    assert isinstance(second["at"], str)
    assert second["added"] == ["S2a"]
    assert second["removed"] == []  # S2 was already gone
    assert {"from": "S2a", "to": "S3"} in second["edges_changed"]
    assert final["status"] == "running"  # the stream stops mid-run
    assert final["unparsed_lines"] == 0

    # The offset advances, only ever to a line boundary, holding back partial
    # lines, and ends past the whole output.
    output = bridge.output
    assert offsets == sorted(offsets)
    assert offsets[-1] == len(output)
    assert all(o == 0 or output[o - 1] == "\n" for o in offsets)
    reads = [c[2] for c in bridge.calls_named("read_output")]
    assert reads == [0] + offsets[:-1]


def test_status_failure_blocks_dependents(load_fixture, repo):
    bridge = FakeBridge(
        chunks_mid_line(load_fixture("run_failure_blocks_dependents.jsonl")), exit_at_end=True
    )
    run_id = start_run(bridge, repo)
    boards, _ = sync_all(bridge, run_id)
    final = boards[-1]
    stories = by_id(final)
    assert stories["S1"]["state"] == "done"
    assert stories["S2"]["state"] == "failed"
    assert stories["S2"]["error"] == "tests failed"
    assert stories["S2"]["blocks"] == ["S3"]
    assert stories["S3"]["state"] == "blocked"
    assert stories["S3"]["waits_on"] == ["S2"]
    assert stories["S3"]["blocked_by_failure"] is True
    assert stories["S4"]["state"] == "blocked"
    assert stories["S4"]["blocked_by_failure"] is True
    assert final["status"] == "failed"
    assert final["ended_at"]
    # The persisted board is the returned one.
    assert bridge.memory.get(board_key(run_id)) == final


def test_status_run_success_completes(load_fixture, repo):
    bridge = FakeBridge(chunks_by_line(load_fixture("run_success.jsonl")), exit_at_end=True)
    run_id = start_run(bridge, repo)
    boards, _ = sync_all(bridge, run_id)
    final = boards[-1]
    assert final["status"] == "completed"
    assert by_id(final)["S1"]["state"] == "done"
    assert final["unparsed_lines"] == 1  # "garbage line"


def test_status_exit_without_run_end_marks_failed(load_fixture, repo):
    lines = load_fixture("run_success.jsonl")
    # Drop the trailing done event and the final newline: the process just exits.
    bridge = FakeBridge([stdout_of(lines[:-1])[:-1]], exit_at_end=True)
    run_id = start_run(bridge, repo)
    result = tools.handle_baro_status(bridge, {"run_id": run_id})
    board = result["board"]
    assert by_id(board)["S1"]["state"] == "done"  # unterminated last line still applied
    assert board["status"] == "failed"
    assert bridge.memory.get(handle_key(run_id))["read_offset"] == len(bridge.output)


def test_status_after_run_end_does_not_read(load_fixture, repo):
    bridge = FakeBridge(chunks_by_line(load_fixture("run_success.jsonl")), exit_at_end=True)
    run_id = start_run(bridge, repo)
    sync_all(bridge, run_id)
    reads = len(bridge.calls_named("read_output"))
    result = tools.handle_baro_status(bridge, {"run_id": run_id})
    assert result["board"]["status"] == "completed"
    assert len(bridge.calls_named("read_output")) == reads


@pytest.mark.parametrize(
    "run_id", ["", "nope", "baro-20261105T142233Z-ABCDEF", "baro-20261105T142233Z-a1b2c3\n", None, 7]
)
def test_status_invalid_run_id(run_id):
    bridge = FakeBridge()
    result = tools.handle_baro_status(bridge, {"run_id": run_id})
    assert result["ok"] is False
    assert result["error"] == "invalid_argument"
    assert bridge.dispatches == []


def test_status_unknown_run():
    bridge = FakeBridge()
    result = tools.handle_baro_status(bridge, {"run_id": "baro-20261105T142233Z-a1b2c3"})
    assert result["ok"] is False
    assert result["error"] == "unknown_run"


def test_status_read_error_is_dispatch_failed(repo):
    bridge = FakeBridge(read_error=DispatchError("process tool failed"))
    run_id = start_run(bridge, repo)
    result = tools.handle_baro_status(bridge, {"run_id": run_id})
    assert result["error"] == "dispatch_failed"
    assert bridge.memory.get(handle_key(run_id))["read_offset"] == 0


# ------------------------------------------------------------- unattended


@pytest.mark.parametrize("tool", ["baro_plan", "baro_run", "baro_cancel"])
def test_unattended_refuses_without_dispatch(tool, repo, clock, load_fixture):
    bridge = FakeBridge(chunks_by_line(load_fixture("plan_only.jsonl")))
    run_id = start_run(bridge, repo)
    bridge.unattended = True
    bridge.calls.clear()
    args = {"run_id": run_id} if tool == "baro_cancel" else goal_args(repo)
    result = tools.HANDLERS[tool](bridge, args)
    assert result["ok"] is False
    assert result["error"] == "unattended_context"
    assert "approval" in result["message"]
    assert bridge.dispatches == []
    assert bridge.memory.get(board_key(run_id))["status"] == "running"


def test_unattended_status_still_allowed(load_fixture, repo):
    bridge = FakeBridge(chunks_by_line(load_fixture("run_success.jsonl")))
    run_id = start_run(bridge, repo)
    bridge.unattended = True
    bridge.calls.clear()
    result = tools.handle_baro_status(bridge, {"run_id": run_id})
    assert result["ok"] is True
    assert result["board"]["run_id"] == run_id
    assert bridge.calls_named("start_background") == []
    assert bridge.calls_named("kill") == []
