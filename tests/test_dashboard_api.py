"""dashboard/plugin_api.py: the read-only routes desktop/plugin.js calls via ctx.rest.

The module is loaded the way Hermes loads it (hermes_cli/web_server_dashboard.py:1226-1235:
a top-level module from its file path). FastAPI is not a test dependency. When it is not
installed, a minimal stand-in exposing APIRouter, Depends and HTTPException is used, and the
route functions are called directly with an in-memory store. When FastAPI and its test
client are installed, the routes are also exercised over HTTP with the store dependency overridden.
"""

import importlib.util
import sys
import re
import types
from pathlib import Path

import pytest

from baro_plugin.core.board import Board
from baro_plugin.core.store import RunRepository

ROOT = Path(__file__).resolve().parent.parent
API_FILE = ROOT / "dashboard" / "plugin_api.py"
MODULE = "hermes_dashboard_plugin_baro"
CORE_MODULE = "hermes_dashboard_plugin_baro_core"
OLD_RUN = "baro-20261105T142233Z-a1b2c3"
NEW_RUN = "baro-20261105T150000Z-d4e5f6"


def _fastapi_stub() -> types.ModuleType:
    stub = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            super().__init__(status_code, detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self):
            self.routes = []

        def get(self, path):
            def decorate(fn):
                self.routes.append(types.SimpleNamespace(path=path, methods={"GET"}, endpoint=fn))
                return fn
            return decorate

    stub.APIRouter = APIRouter
    stub.HTTPException = HTTPException
    stub.Depends = lambda dependency: None
    return stub


def _has_real_fastapi() -> bool:
    try:
        import fastapi  # noqa: F401
    except ImportError:
        return False
    return True


# Decided at collection, before any test installs the stand-in under sys.modules['fastapi'].
REAL_FASTAPI = _has_real_fastapi()


@pytest.fixture
def api(monkeypatch):
    if not REAL_FASTAPI:
        monkeypatch.setitem(sys.modules, "fastapi", _fastapi_stub())
    for name in (MODULE, CORE_MODULE, CORE_MODULE + ".ids", CORE_MODULE + ".store",
                 CORE_MODULE + ".board", CORE_MODULE + ".events"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(MODULE, API_FILE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, MODULE, module)
    spec.loader.exec_module(module)
    yield module
    for name in list(sys.modules):
        if name == CORE_MODULE or name.startswith(CORE_MODULE + "."):
            sys.modules.pop(name, None)


@pytest.fixture
def populated_store(memory_store, load_fixture):
    """Two runs saved through RunRepository, the writer bridge.py uses."""
    repo = RunRepository(memory_store)
    for run_id in (OLD_RUN, NEW_RUN):
        board = Board(run_id=run_id, goal="build it", repo="/tmp/repo", baro_version="0.125.0")
        for line in load_fixture("run_failure_blocks_dependents.jsonl"):
            board.apply_line(line, at="2026-11-05T14:22:33Z")
        repo.save_board(board)
    return memory_store


def _status(exc_info):
    return exc_info.value.status_code


def test_router_exposes_the_two_read_only_routes(api):
    routes = {(route.path, tuple(sorted(route.methods))) for route in api.router.routes}
    assert routes == {("/runs", ("GET",)), ("/runs/{run_id}", ("GET",))}


def test_list_runs_returns_ids_oldest_first_from_runs_key(api, populated_store):
    assert api.list_runs(store=populated_store) == {"runs": [OLD_RUN, NEW_RUN]}
    assert populated_store.get("runs") == [OLD_RUN, NEW_RUN]


def test_list_runs_on_empty_store(api, memory_store):
    assert api.list_runs(store=memory_store) == {"runs": []}


def test_get_run_returns_the_board_stored_at_run_key(api, populated_store):
    result = api.get_run(NEW_RUN, store=populated_store)
    assert result == {"board": populated_store.get(f"run:{NEW_RUN}")}
    board = result["board"]
    assert board["run_id"] == NEW_RUN
    assert board["status"] == "failed"
    states = {story["id"]: story["state"] for story in board["stories"]}
    assert states == {"S1": "done", "S2": "failed", "S3": "blocked", "S4": "blocked"}


def test_reads_do_not_write(api, populated_store):
    before = dict(populated_store.data)
    api.list_runs(store=populated_store)
    api.get_run(OLD_RUN, store=populated_store)
    assert populated_store.data == before


def test_unknown_run_is_404(api, populated_store):
    with pytest.raises(api.HTTPException) as exc_info:
        api.get_run("baro-20991231T000000Z-000000", store=populated_store)
    assert _status(exc_info) == 404


def test_evicted_run_is_404(api, populated_store):
    populated_store.set(f"run:{OLD_RUN}", None)  # how RunRepository deletes an evicted board
    with pytest.raises(api.HTTPException) as exc_info:
        api.get_run(OLD_RUN, store=populated_store)
    assert _status(exc_info) == 404


@pytest.mark.parametrize(
    "run_id",
    [
        "nope",
        "runs",
        OLD_RUN + "\n",  # accepted by re.match on the '$' pattern, rejected by fullmatch
        OLD_RUN.upper(),
        "../" + OLD_RUN,
        "",
    ],
)
def test_invalid_run_id_is_404_without_touching_the_store(api, run_id):
    class ExplodingStore:
        def get(self, key):
            raise AssertionError(f"store read for invalid run id: {key!r}")

        def set(self, key, value):
            raise AssertionError("store write")

    with pytest.raises(api.HTTPException) as exc_info:
        api.get_run(run_id, store=ExplodingStore())
    assert _status(exc_info) == 404


PANEL_FILE = ROOT / "desktop" / "plugin.js"


def test_panel_header_comment_declares_the_shapes_served(api, populated_store):
    """The responses match the contract in desktop/plugin.js's header comment and its parsers."""
    panel = PANEL_FILE.read_text(encoding="utf-8")
    # Header comment (desktop/plugin.js:26-27): the two calls and their response shapes.
    assert "//     GET /runs        -> { runs: [run_id, ...] }  (storage key `runs`, oldest first)" in panel
    assert "//     GET /runs/<id>   -> { board: Board.to_dict() }  (storage key `run:<id>`)" in panel
    # The paths the panel actually calls through ctx.rest.
    assert ".rest('/runs')" in panel
    assert ".rest('/runs/' + encodeURIComponent(runId)" in panel
    # What its parsers read: payload.runs (a list of strings) and payload.board.stories (a list).
    assert "Array.isArray(payload.runs) ? payload.runs" in panel
    assert "payload.board && typeof payload.board === 'object' ? payload.board" in panel
    assert "Array.isArray(board.stories) ? board : null" in panel

    runs = api.list_runs(store=populated_store)
    assert set(runs) == {"runs"}
    assert runs["runs"] == [OLD_RUN, NEW_RUN]  # oldest first: the panel picks ids[ids.length - 1]
    assert all(isinstance(run_id, str) and run_id for run_id in runs["runs"])

    for run_id in runs["runs"]:
        # Only characters encodeURIComponent leaves unchanged, so the panel's path is /runs/<run_id>.
        assert re.fullmatch(r"[A-Za-z0-9\-_.!~*'()]+", run_id)
        payload = api.get_run(run_id, store=populated_store)
        assert set(payload) == {"board"}
        assert isinstance(payload["board"], dict)
        assert isinstance(payload["board"]["stories"], list) and payload["board"]["stories"]
        assert payload["board"] == Board.from_dict(populated_store.get(f"run:{run_id}")).to_dict()


def test_routes_over_http_when_fastapi_is_installed(api, populated_store):
    if not REAL_FASTAPI:
        pytest.skip("fastapi is not installed; the routes are covered by direct calls above")
    from fastapi import FastAPI

    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:  # the test client needs an optional HTTP library
        pytest.skip(f"fastapi test client unavailable: {exc}")

    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/baro")
    app.dependency_overrides[api.get_store] = lambda: populated_store
    client = TestClient(app)

    runs = client.get("/api/plugins/baro/runs")
    assert runs.status_code == 200
    assert runs.json() == {"runs": [OLD_RUN, NEW_RUN]}

    board = client.get(f"/api/plugins/baro/runs/{OLD_RUN}")
    assert board.status_code == 200
    assert board.json() == {"board": populated_store.get(f"run:{OLD_RUN}")}

    assert client.get("/api/plugins/baro/runs/not-a-run-id").status_code == 404
    assert client.get("/api/plugins/baro/runs/baro-20991231T000000Z-000000").status_code == 404
