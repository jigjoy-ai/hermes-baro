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
        self.dispatched = []
        self.replies = list(replies or [])
        self.cron = cron
        self.state = FakeState()

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools.append({"name": name, "toolset": toolset, "schema": schema, "handler": handler})

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
    assert started == {"ok": False, "error": "dispatch_failed", "message": started["message"]}
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


def test_kill_uses_process_manage_kill_and_raises_on_incomplete_kill():
    ctx = FakeCtx(replies=[{"status": "killed", "session_id": "proc_ab12"}])
    HermesBridge(ctx).kill("proc_ab12")
    assert ctx.dispatched == [("process_manage", {"action": "kill", "session_id": "proc_ab12"})]

    HermesBridge(FakeCtx(replies=[{"status": "already_exited", "exit_code": 0}])).kill("proc_ab12")
    incomplete = {"status": "error", "error": "Kill incomplete: 1 process(es) still alive (77); session running"}
    with pytest.raises(DispatchError, match="Kill incomplete"):
        HermesBridge(FakeCtx(replies=[incomplete])).kill("proc_ab12")


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
