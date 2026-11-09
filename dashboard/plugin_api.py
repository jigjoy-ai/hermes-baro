"""Baro run board backend, mounted at /api/plugins/baro/ (read only).

desktop/plugin.js reaches this module through ``ctx.rest`` and expects
(see its header comment):

    GET /runs        -> {"runs": [run_id, ...]}       storage key ``runs``, oldest first
    GET /runs/<id>   -> {"board": Board.to_dict()}    storage key ``run:<id>``
    GET /runs/<id>/activity -> {"activity": core.activity.summarize(...)}

Sources followed:
- reference-hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md:2130-2152
  puts a ``dashboard/`` backend beside ``desktop/plugin.js`` in one package, and
  :2236-2262 says it exports ``router = APIRouter()``, with routes mounted under
  ``/api/plugins/<id>/``. :2268-2274 adds that it is imported only when the
  plugin is in ``plugins.enabled``.
- plugins/kanban/dashboard/manifest.json:13 declares ``"api": "plugin_api.py"``.
  plugins/kanban/dashboard/plugin_api.py:43 defines ``router = APIRouter()`` and
  :359 serves its board read as ``@router.get("/board")``. This module has the
  same shape.
- hermes_cli/web_server_dashboard.py:1226-1235 imports this file as a top-level
  module (``hermes_dashboard_plugin_baro``) with no parent package, so relative
  imports into the plugin package do not work. ``_load_core`` below loads the
  sibling ``core/`` package by path instead.

Storage accessor: the tool side persists boards through ``ctx.state``.
hermes_cli/plugins.py:282-285 defines it as
``PluginState(self.plugin_id, self.manifest.data_namespace)``. For a native
plugin, ``data_namespace`` is empty and ``plugin_id`` is the manifest name
``baro`` (plugins.py:245-248). hermes_cli/plugins_state.py:134-177
(``PluginState.__init__``/``get``) reads the same profile-scoped
``<HERMES_HOME>/plugin-data/<namespace>/state.json``. So ``PluginState("baro")``
here opens exactly the store that bridge.py writes with core/store.py
``RunRepository``. The keys are read only through ``RunRepository``, so they
cannot drift. No route writes anything.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

PLUGIN_ID = "baro"

_CORE_DIR = Path(__file__).resolve().parent.parent / "core"
_CORE_MODULE = "hermes_dashboard_plugin_baro_core"


def _load_core() -> Any:
    """Import the plugin's sibling ``core/`` package (standard library only) by path."""
    existing = sys.modules.get(_CORE_MODULE)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        _CORE_MODULE,
        _CORE_DIR / "__init__.py",
        submodule_search_locations=[str(_CORE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load baro core package from {_CORE_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_CORE_MODULE, None)
        raise
    return module


_load_core()
from hermes_dashboard_plugin_baro_core.ids import RUN_ID_RE  # noqa: E402
from hermes_dashboard_plugin_baro_core.store import RunRepository  # noqa: E402
from hermes_dashboard_plugin_baro_core.events_file import events_path, live_board  # noqa: E402
from hermes_dashboard_plugin_baro_core.activity import summarize  # noqa: E402

router = APIRouter()


def get_store() -> Any:
    """The plugin's ``ctx.state`` store, opened without a PluginContext (see module docstring)."""
    from hermes_cli.plugins_state import PluginState

    return PluginState(PLUGIN_ID)


@router.get("/runs")
def list_runs(store: Any = Depends(get_store)) -> dict:
    return {"runs": RunRepository(store).list_runs()}


@router.get("/runs/{run_id}")
def get_run(run_id: str, store: Any = Depends(get_store)) -> dict:
    if not isinstance(run_id, str) or re.fullmatch(RUN_ID_RE, run_id) is None:
        raise HTTPException(status_code=404, detail="unknown run")
    board = RunRepository(store).load_board(run_id)
    if board is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return {"board": live_board(board, _events_text(store, run_id)).to_dict()}


@router.get("/runs/{run_id}/activity")
def get_activity(run_id: str, store: Any = Depends(get_store)) -> dict:
    """What each agent is doing (core/activity.py), from the run's mirrored events."""
    if not isinstance(run_id, str) or re.fullmatch(RUN_ID_RE, run_id) is None:
        raise HTTPException(status_code=404, detail="unknown run")
    if RunRepository(store).load_board(run_id) is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return {"activity": summarize(_events_text(store, run_id))}


def _events_text(store: Any, run_id: str) -> str:
    """The run's mirrored event stream (core/events_file.py), or "" when there is none."""
    data_dir = getattr(store, "data_dir", None)
    if data_dir is None:
        return ""
    try:
        return events_path(data_dir, run_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
