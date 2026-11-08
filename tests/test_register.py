"""register(ctx) wiring and HermesBridge's mapping of Hermes tool replies (bridge.py).

The fake ctx records ``register_tool`` calls and answers ``dispatch_tool`` with the
JSON shapes the cited Hermes tools return. No Hermes is imported or run.
"""

import json
import sys

import pytest

import baro_plugin
from baro_plugin import bridge as bridge_mod
from baro_plugin.bridge import HermesBridge, PluginStateStore, register
from baro_plugin.core.store import RunRepository
from baro_plugin.tools import ApprovalDenied, DispatchError

SESSION_VARS = ("HERMES_PLATFORM", "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE", "HERMES_CRON_SESSION")


class FakeState:
    """ctx.state: get(key, default) / set(key, value), JSON values."""

    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return json.loads(self.data[key]) if key in self.data else default

    def set(self, key, value):
        self.data[key] = json.dumps(value)


class FakeCtx:
    def __init__(self, replies=None, cron=None):
        self.tools = []
        self.hooks = []
        self.dispatched = []
        self.replies = list(replies or [])
        self.cron = cron
        self.state = FakeState()

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools.append({"name": name, "toolset": toolset, "schema": schema, "handler": handler})

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def dispatch_tool(self, name, args, **kwargs):
        self.dispatched.append((name, args))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)

    def current_cron_execution(self):
        return self.cron


@pytest.fixture(autouse=True)
def _attended_session(monkeypatch):
    for name in SESSION_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- register


def test_register_records_exactly_four_tools_in_toolset_baro():
    ctx = FakeCtx()
    register(ctx)
    assert [t["name"] for t in ctx.tools] == ["baro_plan", "baro_run", "baro_status", "baro_cancel"]
    assert {t["toolset"] for t in ctx.tools} == {"baro"}
    for tool in ctx.tools:
        assert tool["schema"]["name"] == tool["name"]
        assert callable(tool["handler"])
    assert ctx.dispatched == []  # register() dispatches nothing and builds no bridge


def test_package_entry_point_delegates_to_bridge_register():
    # Hermes calls the package's register(ctx) (__init__.py), which must register the same tools.
    ctx = FakeCtx()
    assert baro_plugin.register(ctx) is None
    assert [(t["name"], t["toolset"]) for t in ctx.tools] == [
        ("baro_plan", "baro"), ("baro_run", "baro"), ("baro_status", "baro"), ("baro_cancel", "baro"),
    ]
    assert ctx.dispatched == []


def test_loading_the_plugin_imports_no_hermes_module():
    assert baro_plugin.bridge is bridge_mod
    assert [m for m in sys.modules if m.startswith("hermes")] == []


def test_handler_returns_json_and_never_raises():
    ctx = FakeCtx(replies=[RuntimeError("registry exploded")])
    register(ctx)
    handlers = {t["name"]: t["handler"] for t in ctx.tools}

    bad = json.loads(handlers["baro_status"]({"run_id": "nope"}, task_id="t1"))
    assert bad["ok"] is False and bad["error"] == "invalid_argument"

    # start_background's dispatch raises inside Hermes: surfaced as dispatch_failed JSON.
    started = json.loads(handlers["baro_run"]({"goal": "add a README", "repo": str(bridge_mod.__file__).rsplit("/", 1)[0]}))
    assert started == {
        "ok": False,
        "error": "dispatch_failed",
        "message": started["message"],
        "instruction": started["instruction"],
    }
    # A failed dispatch tells the model Baro owns the work, so it does not do it itself.
    assert "Do not write, edit or test code" in started["instruction"]
    assert ctx.dispatched[0][0] == "terminal"


# ------------------------------------------------------------ start / kill


def test_start_background_dispatches_terminal_in_background_mode():
    ctx = FakeCtx(replies=[{"output": "Background process started", "session_id": "proc_ab12",
                            "pid": 4242, "exit_code": 0, "error": None}])
    assert HermesBridge(ctx).start_background("echo hi", "/tmp/repo") == {"process_id": "proc_ab12"}
    assert ctx.dispatched == [("terminal", {"command": "echo hi", "background": True, "workdir": "/tmp/repo"})]


@pytest.mark.parametrize("status", ["blocked", "pending_approval", "approval_required"])
def test_start_background_maps_refused_approval_to_approval_denied(status):
    ctx = FakeCtx(replies=[{"output": "", "exit_code": -1, "error": "Command denied: user declined", "status": status}])
    with pytest.raises(ApprovalDenied):
        HermesBridge(ctx).start_background("echo hi", "/tmp/repo")


def test_start_background_maps_other_failures_to_dispatch_error():
    for reply in ({"error": "Unknown tool: terminal"}, {"output": "started", "error": None}):
        with pytest.raises(DispatchError):
            HermesBridge(FakeCtx(replies=[reply])).start_background("echo hi", "/tmp/repo")


POLL = ("process_manage", {"action": "poll", "session_id": "proc_ab12"})
PM_KILL = ("process_manage", {"action": "kill", "session_id": "proc_ab12"})
PKILL = ("terminal", {"command": "pkill -TERM -P 4242"})
KILL_ROOT = ("terminal", {"command": "kill -TERM 4242"})


def _poll(status="running", pid=4242):
    return {"session_id": "proc_ab12", "command": "x", "status": status, "pid": pid}


def _term(exit_code=0):
    return {"output": "", "exit_code": exit_code, "error": None}


def test_kill_signals_children_then_hermes_kill_then_root_through_dispatch():
    # pkill exits 1 when the root has no children: that is not a failure.
    ctx = FakeCtx(replies=[_poll(), _term(1), {"status": "killed", "session_id": "proc_ab12"}, _term(1)])
    HermesBridge(ctx).kill("proc_ab12")
    assert ctx.dispatched == [POLL, PKILL, PM_KILL, KILL_ROOT]


@pytest.mark.parametrize("poll", [_poll(status="exited"), _poll(pid=None), _poll(pid=True), _poll(pid=1),
                                  {"status": "not_found", "error": "No process with ID proc_ab12"}])
def test_kill_never_signals_an_exited_or_unknown_pid(poll):
    ctx = FakeCtx(replies=[poll, {"status": "already_exited", "exit_code": 0}])
    HermesBridge(ctx).kill("proc_ab12")
    assert ctx.dispatched == [POLL, PM_KILL]


def test_kill_raises_on_incomplete_kill_after_signalling_the_root():
    incomplete = {"status": "error", "error": "Kill incomplete: 1 process(es) still alive (77); session running"}
    ctx = FakeCtx(replies=[_poll(), _term(), incomplete, _term()])
    with pytest.raises(DispatchError, match="Kill incomplete"):
        HermesBridge(ctx).kill("proc_ab12")
    assert ctx.dispatched == [POLL, PKILL, PM_KILL, KILL_ROOT]


def test_kill_refused_signal_raises_approval_denied_but_hermes_kill_still_runs():
    refused = {"output": "", "exit_code": 1, "error": "denied by user", "status": "blocked"}
    ctx = FakeCtx(replies=[_poll(), refused, {"status": "killed"}, _term()])
    with pytest.raises(ApprovalDenied, match="denied by user"):
        HermesBridge(ctx).kill("proc_ab12")
    assert ctx.dispatched == [POLL, PKILL, PM_KILL, KILL_ROOT]


# --------------------------------------------------------------- read_output


def _log(output, status="running"):
    return {"session_id": "proc_ab12", "command": "x", "status": status, "output": output,
            "total_lines": output.count("\n") + 1, "showing": "n lines"}


def test_read_output_returns_text_from_character_offset():
    ctx = FakeCtx(replies=[_log('{"a":1}\n{"b'), _log('{"a":1}\n{"b":2}\n{"c":3}', status="exited")])
    hb = HermesBridge(ctx)
    text, new_offset, exited = hb.read_output("proc_ab12", 0)
    assert (text, new_offset, exited) == ('{"a":1}\n{"b', 11, False)
    text, new_offset, exited = hb.read_output("proc_ab12", 8)  # re-read from the last line boundary
    assert (text, new_offset, exited) == ('{"b":2}\n{"c":3}', 8 + len(text), True)
    assert ctx.dispatched[0] == ("process_manage", {"action": "log", "session_id": "proc_ab12",
                                                    "offset": 0, "limit": bridge_mod._LOG_LINE_LIMIT})


def test_read_output_keeps_offsets_when_hermes_buffer_rolls(monkeypatch):
    monkeypatch.setattr(bridge_mod, "_ANCHOR_CHARS", 8)
    first = "line-001\nline-002\nline-003"
    rolled = "ne-002\nline-003\nline-004"  # the front rolled away, one line was added
    hb = HermesBridge(FakeCtx(replies=[_log(first), _log(rolled)]))
    text, offset, _ = hb.read_output("proc_ab12", 0)
    assert offset == len(first)
    text, new_offset, _ = hb.read_output("proc_ab12", 18)  # start of line-003 in virtual offsets
    assert text == "line-003\nline-004"
    assert new_offset == 18 + len(text)


def test_read_output_raises_for_unknown_process():
    with pytest.raises(DispatchError):
        HermesBridge(FakeCtx(replies=[{"status": "not_found", "error": "No process with ID x"}])).read_output("x", 0)


# ------------------------------------------------------------- unattended


def test_attended_cli_session_is_not_unattended(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    assert HermesBridge(FakeCtx()).is_unattended() is False


def test_cron_execution_is_unattended():
    assert HermesBridge(FakeCtx(cron=object())).is_unattended() is True


@pytest.mark.parametrize("name,value", [
    ("HERMES_CRON_SESSION", "1"),
    ("HERMES_SESSION_PLATFORM", "telegram"),
    ("HERMES_SESSION_PLATFORM", "webhook"),
    ("HERMES_SESSION_SOURCE", "discord"),
])
def test_gateway_and_cron_session_flags_are_unattended(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert HermesBridge(FakeCtx()).is_unattended() is True


def test_unattended_baro_run_never_dispatches(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    ctx = FakeCtx()
    register(ctx)
    run = next(t["handler"] for t in ctx.tools if t["name"] == "baro_run")
    result = json.loads(run({"goal": "x", "repo": "/tmp"}))
    assert result["error"] == "unattended_context"
    assert ctx.dispatched == []


# ------------------------------------------------------------------ store


def test_store_persists_boards_through_ctx_state():
    ctx = FakeCtx()
    store = HermesBridge(ctx).store()
    assert isinstance(store, PluginStateStore)
    runs = RunRepository(store)
    runs.save_handle("baro-20261105T142233Z-a1b2c3", {"process_id": "proc_ab12", "read_offset": 5})
    assert runs.load_handle("baro-20261105T142233Z-a1b2c3") == {"process_id": "proc_ab12", "read_offset": 5}
    assert "handle:baro-20261105T142233Z-a1b2c3" in ctx.state.data
    assert store.get("missing") is None


# ---------------------------------------------------------------- launch approval


def test_register_adds_one_pre_tool_call_hook():
    ctx = FakeCtx()
    register(ctx)
    assert [name for name, _ in ctx.hooks] == ["pre_tool_call"]


@pytest.mark.parametrize("tool", ["baro_run", "baro_plan"])
def test_every_launch_goes_to_the_human_approval_gate(tool):
    ctx = FakeCtx()
    register(ctx)
    _, hook = ctx.hooks[0]
    directive = hook(tool_name=tool, args={"goal": "add a kanban", "repo": "/work/app"}, task_id="t1")
    assert directive["action"] == "approve"
    assert directive["rule_key"] == "baro:launch"
    assert "/work/app" in directive["message"]
    assert '"add a kanban"' in directive["message"]
    assert "without asking you again" in directive["message"]
    assert "--local-only" in directive["message"]


@pytest.mark.parametrize("tool", ["baro_status", "baro_cancel", "terminal", "read_file"])
def test_other_tools_are_left_to_hermes(tool):
    ctx = FakeCtx()
    register(ctx)
    _, hook = ctx.hooks[0]
    assert hook(tool_name=tool, args={}, task_id="") is None


def test_the_approval_message_survives_odd_arguments():
    ctx = FakeCtx()
    register(ctx)
    _, hook = ctx.hooks[0]
    directive = hook(tool_name="baro_run", args=None, task_id="")
    assert "the repository you named" in directive["message"]
    long_goal = hook(tool_name="baro_plan", args={"goal": "x " * 500, "repo": "/r"}, task_id="")
    assert "…" in long_goal["message"]
    assert "stopped as soon as the plan is ready" in long_goal["message"]
