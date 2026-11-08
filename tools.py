"""Tool schemas and handlers for the ``baro`` toolset (ADR-010).

Handlers are ``handle_<name>(bridge, args) -> dict``. ``__init__.py`` adapts
them to Hermes's handler signature. The guide requires ``def handler(args,
**kwargs) -> str`` returning JSON and never raising
(reference-hermes-agent/website/docs/developer-guide/plugins/index.md:885-892).
Schemas use the guide's ``{name, description, parameters}`` shape (index.md:748-767).

``bridge`` is the only route to Hermes. It exposes:

- ``is_unattended() -> bool``
- ``start_background(command, cwd) -> {"process_id": str}``. It goes through
  Hermes's gated tool pipeline and raises ApprovalDenied or DispatchError.
- ``read_output(process_id, offset) -> (text, new_offset, exited)``. ``offset``
  counts characters of the process's cumulative output, so
  ``new_offset == offset + len(text)``.
- ``kill(process_id) -> None``, which kills the process tree.
- ``store() -> BoardStore``

Every failure is ``{"ok": False, "error": <code>, "message": str}``.
Unattended contexts (cron, messaging gateway) cannot answer an approval
prompt. So baro_plan, baro_run and baro_cancel refuse there before any
dispatch. baro_status only reads, so it stays allowed.
"""

from __future__ import annotations

import re
import shlex
import time
from datetime import datetime, timezone
from typing import Any

from .core.board import TERMINAL_STATUSES, Board
from .core.settings import is_local_url, subscription_backend
from .core.command import (
    BARO_VERSION,
    PLAN_ONLY_FLAG,
    build_command,
    normalize_base_url,
    unwrap_references,
    validate_args,
)
from .core.events import BaroEvent, LineBuffer, parse_line
from .core.ids import RUN_ID_RE, new_run_id
from .core.store import RunRepository

# Error codes (ADR-010).
INVALID_ARGUMENT = "invalid_argument"
UNATTENDED_CONTEXT = "unattended_context"
APPROVAL_DENIED = "approval_denied"
DISPATCH_FAILED = "dispatch_failed"
UNKNOWN_RUN = "unknown_run"
PLAN_TIMEOUT = "plan_timeout"
ERROR_CODES = (
    INVALID_ARGUMENT,
    UNATTENDED_CONTEXT,
    APPROVAL_DENIED,
    DISPATCH_FAILED,
    UNKNOWN_RUN,
    PLAN_TIMEOUT,
)

# Under Hermes's own deadline for one tool call (timeouts.tools.sequential_call,
# 420 s by default): past it Hermes abandons the call and Desktop shows
# "Result unavailable" while Baro keeps planning.
PLAN_TIMEOUT_SECS = 360.0
PLAN_POLL_SECS = 0.5
MAX_OPTION_CHARS = 2000

# Injectable clock and sleep. Tests monkeypatch these module attributes.
_monotonic = time.monotonic
_sleep = time.sleep


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ApprovalDenied(Exception):
    """The user (or Hermes policy) declined the gated command."""


class DispatchError(Exception):
    """Hermes could not run the dispatched tool call."""


# Read by the model with every result: when it uses Baro, Baro does the coding.
# Without it, a failed run was followed by Hermes writing and testing the code
# itself, asking the user to approve each command.
BARO_OWNS_THE_WORK = (
    "Baro does the coding for this goal. Do not write, edit or test code in this "
    "repository yourself and do not carry out the goal another way. Tell the user "
    "what happened and wait for their instructions."
)
_RUN_ACTIVE = (
    "The run is in progress and Baro's agents are working in the repository; the "
    "user can watch it live on the Baro board. Hermes notifies you when Baro exits: "
    "do not sleep in the terminal or poll baro_status in a loop while you wait, and "
    "do not edit or test code in the repository yourself. When notified, call "
    "baro_status once and report the outcome."
)

# Errors about the request itself are for the model to fix; the rest end Baro's part.
_ARGUMENT_ERRORS = ("invalid_argument", "unknown_run")


def error(code: str, message: str) -> dict:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code {code!r}")
    result = {"ok": False, "error": code, "message": message}
    if code not in _ARGUMENT_ERRORS:
        result["instruction"] = BARO_OWNS_THE_WORK
    return result


def _status_result(board: Any) -> dict:
    result = {"ok": True, "board": board.to_dict()}
    if board.status in ("failed", "cancelled"):
        result["instruction"] = BARO_OWNS_THE_WORK
    elif board.status not in TERMINAL_STATUSES:
        result["instruction"] = _RUN_ACTIVE
    return result


_UNATTENDED_MESSAGE = (
    "{tool} needs an interactive Hermes session: it runs a command that Hermes "
    "must ask you to approve, and this context (cron job or messaging gateway) "
    "cannot show an approval prompt. Run it from an interactive session."
)

# ---------------------------------------------------------------- schemas

_GOAL_REPO_PROPS = {
    "goal": {
        "type": "string",
        "description": "The software goal for Baro, in plain language.",
    },
    "repo": {
        "type": "string",
        "description": "Absolute path of an existing directory: the repository Baro works in.",
    },
    "llm": {
        "type": "string",
        "description": "Optional Baro --llm backend (e.g. 'openai'). Omit to use Baro's configured backend.",
    },
    "openai_base_url": {
        "type": "string",
        "description": "Optional Baro --openai-base-url for an OpenAI-compatible endpoint.",
    },
    "model": {
        "type": "string",
        "description": "Optional Baro --model name.",
    },
}

_RUN_ID_PROPS = {
    "run_id": {
        "type": "string",
        "description": "Run id returned by baro_run, e.g. 'baro-20261105T142233Z-a1b2c3'.",
    },
}

TOOL_SCHEMAS: dict[str, dict] = {
    "baro_plan": {
        "name": "baro_plan",
        "description": (
            "Only when the user asks to see a plan without running it: ask Baro to "
            "plan a software goal and return the stories with their dependency "
            "edges, without executing any. To carry out a goal, call baro_run "
            "directly instead; it plans as part of the run. Baro does the coding: "
            "never implement the goal yourself, even if planning fails."
        ),
        "parameters": {
            "type": "object",
            "properties": dict(_GOAL_REPO_PROPS),
            "required": ["goal", "repo"],
        },
    },
    "baro_run": {
        "name": "baro_run",
        "description": (
            "Carry out a software goal with Baro: it plans the goal into stories and "
            "runs them with coding agents in the repository, as a background process. "
            "Call it directly, without baro_plan first. Pass the goal as the user "
            "stated it; do not add methods or requirements they did not ask for. "
            "Returns a run id immediately; Hermes notifies you when Baro exits, and "
            "the user watches progress on the Baro board, so do not poll in a loop. "
            "Baro does all the coding: do not write, edit or test code in that "
            "repository yourself, and if the run fails, report it instead of doing "
            "the work."
        ),
        "parameters": {
            "type": "object",
            "properties": dict(_GOAL_REPO_PROPS),
            "required": ["goal", "repo"],
        },
    },
    "baro_status": {
        "name": "baro_status",
        "description": (
            "Return the live board of a Baro run: every story with its state "
            "(blocked, ready, running, done, failed), what it waits on, what it "
            "blocks, and the replans so far."
        ),
        "parameters": {
            "type": "object",
            "properties": dict(_RUN_ID_PROPS),
            "required": ["run_id"],
        },
    },
    "baro_cancel": {
        "name": "baro_cancel",
        "description": "Stop a Baro run and its whole process tree.",
        "parameters": {
            "type": "object",
            "properties": dict(_RUN_ID_PROPS),
            "required": ["run_id"],
        },
    },
}

# ------------------------------------------------------------- validation


def _goal_args(args: Any, bridge: Any = None) -> tuple[dict | None, dict | None]:
    """Return (validated values, None) or (None, error).

    With no backend in the arguments, the subscription setting (core/settings.py)
    may supply one; a backend named in the call always wins.
    """
    if not isinstance(args, dict):
        return None, error(INVALID_ARGUMENT, "arguments must be an object")
    goal, repo = args.get("goal"), args.get("repo")
    if isinstance(goal, str):
        goal = unwrap_references(goal)
    try:
        validate_args(goal, repo)
    except ValueError as exc:
        return None, error(INVALID_ARGUMENT, str(exc))
    values = {"goal": goal, "repo": repo}
    for name in ("llm", "openai_base_url", "model"):
        value = args.get(name)
        if value is None or value == "":
            values[name] = None
            continue
        if not isinstance(value, str):
            return None, error(INVALID_ARGUMENT, f"{name} must be a string")
        if "\x00" in value or len(value) > MAX_OPTION_CHARS:
            return None, error(
                INVALID_ARGUMENT,
                f"{name} must have no NUL and at most {MAX_OPTION_CHARS} characters",
            )
        value = unwrap_references(value)
        if name == "openai_base_url":
            try:
                value = normalize_base_url(value)
            except ValueError as exc:
                return None, error(INVALID_ARGUMENT, str(exc))
        if value.startswith("-"):
            return None, error(INVALID_ARGUMENT, f"{name} must not start with '-'")
        values[name] = value
    if bridge is not None and not any(values[n] for n in ("llm", "openai_base_url", "model")):
        settings_of = getattr(bridge, "settings", None)
        implied = subscription_backend(settings_of()) if callable(settings_of) else None
        if implied:
            values.update(implied)
    url = values["openai_base_url"]
    probe = getattr(bridge, "endpoint_reachable", None)
    if url and is_local_url(url) and callable(probe) and not probe(url):
        return None, error(
            DISPATCH_FAILED,
            f"Nothing answers at {url}. If this is the Hermes subscription proxy, "
            "start it with `hermes proxy start` and try again.",
        )
    return values, None


def _run_id_arg(args: Any) -> tuple[str | None, dict | None]:
    run_id = args.get("run_id") if isinstance(args, dict) else None
    if not isinstance(run_id, str) or not re.fullmatch(RUN_ID_RE, run_id):
        return None, error(
            INVALID_ARGUMENT,
            "run_id must look like 'baro-YYYYMMDDTHHMMSSZ-xxxxxx' (as returned by baro_run)",
        )
    return run_id, None


def _command(values: dict, *, plan_only: bool = False) -> str:
    return build_command(
        values["goal"],
        values["repo"],
        llm=values["llm"],
        openai_base_url=values["openai_base_url"],
        model=values["model"],
        plan_only=plan_only,
    )


# --------------------------------------------------------------- dispatch


def _start(
    bridge: Any, command: str, cwd: str, notify_on_complete: bool = False
) -> tuple[str | None, dict | None]:
    """Start the command through the bridge. Return (process_id, None) or (None, error)."""
    try:
        handle = (
            bridge.start_background(command, cwd, notify_on_complete=True)
            if notify_on_complete
            else bridge.start_background(command, cwd)
        )
    except ApprovalDenied as exc:
        return None, error(APPROVAL_DENIED, f"Starting Baro was not approved: {exc}")
    except DispatchError as exc:
        return None, error(DISPATCH_FAILED, f"Could not start Baro: {exc}")
    process_id = handle.get("process_id") if isinstance(handle, dict) else None
    if not isinstance(process_id, str) or not process_id:
        return None, error(DISPATCH_FAILED, "Hermes did not return a process id for Baro")
    return process_id, None


def _kill(bridge: Any, process_id: str) -> dict | None:
    """Kill the process tree. Return None on success, else an error."""
    try:
        bridge.kill(process_id)
    except ApprovalDenied as exc:
        return error(
            APPROVAL_DENIED,
            f"Stopping Baro process {process_id} was not approved, so it may still be running: {exc}",
        )
    except DispatchError as exc:
        return error(
            DISPATCH_FAILED,
            f"Could not stop Baro process {process_id}, so it may still be running: {exc}",
        )
    return None


def _read(bridge: Any, process_id: str, offset: int) -> tuple[str, int, bool]:
    text, new_offset, exited = bridge.read_output(process_id, offset)
    return (text or ""), int(new_offset), bool(exited)


# --------------------------------------------------------------- baro_plan


def _plan_result(values: dict, plan: BaroEvent) -> dict:
    return {
        "ok": True,
        "goal": values["goal"],
        "repo": values["repo"],
        "baro_version": BARO_VERSION,
        "stories": [
            {"id": s.id, "title": s.title, "depends_on": list(s.depends_on)} for s in plan.stories
        ],
        "edges": [{"from": dep, "to": s.id} for s in plan.stories for dep in s.depends_on],
        "native_plan_only": PLAN_ONLY_FLAG is not None,
        "stories_started": 0,
    }


def _execution_began(story_id: str | None) -> str:
    return (
        f"Baro began executing story {story_id or '?'} before the plan could be "
        "returned; the process was stopped. No plan is reported."
    )


def handle_baro_plan(bridge: Any, args: dict) -> dict:
    """Return Baro's plan without executing stories.

    Baro has no plan-only flag (core/command.py). With ``--headless`` it
    confirms the plan and goes on to execute it. So the handler runs Baro with
    progressive planning off (PLAN_ENV), reads output until the first plan
    event that holds stories, and kills the process at once. It stops
    consuming at the plan: lines after it, even in the same read, are never
    parsed, so no ``story_start`` is consumed and ``stories_started`` is 0. A
    ``story_start`` *before* any plan means execution began: that is an
    error, never a silent success.
    """
    if bridge.is_unattended():
        return error(UNATTENDED_CONTEXT, _UNATTENDED_MESSAGE.format(tool="baro_plan"))
    values, err = _goal_args(args, bridge)
    if err:
        return err
    native = PLAN_ONLY_FLAG is not None
    started_at = _monotonic()
    process_id, err = _start(bridge, _command(values, plan_only=True), values["repo"])
    if err:
        return err

    def stop(result: dict, exited: bool) -> dict:
        if exited:
            return result
        kill_err = _kill(bridge, process_id)
        return kill_err or result

    buffer = LineBuffer()
    offset = 0
    plan: BaroEvent | None = None
    while True:
        try:
            text, offset, exited = _read(bridge, process_id, offset)
        except Exception as exc:  # DispatchError or a malformed bridge reply
            return stop(error(DISPATCH_FAILED, f"Could not read Baro output: {exc}"), False)
        lines = buffer.feed(text)
        if exited:
            lines += buffer.flush()
        for line in lines:
            event = parse_line(line)
            if event is None:
                continue
            if event.kind == "story_start":
                return stop(error(DISPATCH_FAILED, _execution_began(event.story_id)), exited)
            if event.kind == "plan" and plan is None and not event.stories:
                # A progressive plan's first event: its stories come later.
                continue
            if event.kind == "plan" and plan is None:
                plan = event
                if not native:
                    # Stop consuming here: lines after the plan, even in this
                    # same read, are never parsed. Kill the process at once.
                    return stop(_plan_result(values, plan), exited)
        if exited:
            if plan is not None:
                return _plan_result(values, plan)
            return error(DISPATCH_FAILED, "Baro exited before it reported a plan")
        if _monotonic() - started_at >= PLAN_TIMEOUT_SECS:
            return stop(
                error(
                    PLAN_TIMEOUT,
                    f"Baro reported no plan within {int(PLAN_TIMEOUT_SECS)} seconds; "
                    "the process was stopped. To carry out the goal, use baro_run: it "
                    "plans as part of the run and the plan appears on the Baro board.",
                ),
                False,
            )
        if not text:
            _sleep(PLAN_POLL_SECS)


# ---------------------------------------------------------------- baro_run


def handle_baro_run(bridge: Any, args: dict) -> dict:
    """Start Baro in the background and return a run id at once (no output is read)."""
    if bridge.is_unattended():
        return error(UNATTENDED_CONTEXT, _UNATTENDED_MESSAGE.format(tool="baro_run"))
    values, err = _goal_args(args, bridge)
    if err:
        return err
    run_id = new_run_id()
    command = _command(values)
    events_file_of = getattr(bridge, "events_file", None)
    mirror = events_file_of(run_id) if callable(events_file_of) else None
    if mirror:
        # The board replays this copy of Baro's stdout (core/events_file.py);
        # pipefail keeps Baro's own exit status for Hermes's notification.
        command = f"set -o pipefail 2>/dev/null; {command} | tee {shlex.quote(mirror)}"
    process_id, err = _start(bridge, command, values["repo"], notify_on_complete=True)
    if err:
        return err
    board = Board(
        run_id=run_id,
        goal=values["goal"],
        repo=values["repo"],
        baro_version=BARO_VERSION,
        status="running",
        started_at=_now(),
    )
    runs = RunRepository(bridge.store())
    runs.save_board(board)
    runs.save_handle(run_id, {"process_id": process_id, "read_offset": 0})
    return {
        "ok": True,
        "run_id": run_id,
        "status": "running",
        "baro_version": BARO_VERSION,
        "command": command,
        "instruction": _RUN_ACTIVE,
    }


# ------------------------------------------------------------- baro_status


def _load(runs: RunRepository, run_id: str) -> tuple[Board | None, dict | None, dict | None]:
    board = runs.load_board(run_id)
    handle = runs.load_handle(run_id)
    if board is None or handle is None:
        return None, None, error(UNKNOWN_RUN, f"No Baro run with id {run_id}")
    return board, handle, None


def handle_baro_status(bridge: Any, args: dict) -> dict:
    """Read new output since the stored offset, apply it to the board and save both.

    The stored offset moves only past complete lines, so a line split across
    reads is read again, whole, on the next call. If the process exited
    without Baro's run-end event, the run is marked failed.
    """
    run_id, err = _run_id_arg(args)
    if err:
        return err
    runs = RunRepository(bridge.store())
    board, handle, err = _load(runs, run_id)
    if err:
        return err
    if board.status in TERMINAL_STATUSES:
        return _status_result(board)

    offset = handle.get("read_offset", 0)
    try:
        text, new_offset, exited = _read(bridge, handle["process_id"], offset)
    except Exception as exc:  # DispatchError or a malformed bridge reply
        return error(DISPATCH_FAILED, f"Could not read Baro output for {run_id}: {exc}")

    at = _now()
    cut = text.rfind("\n") + 1
    complete, remainder = text[:cut], text[cut:]
    lines = LineBuffer().feed(complete)
    if exited:
        # The process is gone, so an unterminated last line is complete.
        if remainder:
            lines.append(remainder.rstrip("\r"))
        remainder = ""
    for line in lines:
        board.apply_line(line, at=at)
    if exited and board.status not in TERMINAL_STATUSES:
        board.apply(BaroEvent(kind="run_end", raw={}, outcome="failed"), at=at)

    runs.save_board(board)
    runs.save_handle(
        run_id,
        {"process_id": handle["process_id"], "read_offset": max(offset, new_offset - len(remainder))},
    )
    return _status_result(board)


# ------------------------------------------------------------- baro_cancel


def handle_baro_cancel(bridge: Any, args: dict) -> dict:
    """Kill a live run's process tree and mark the board cancelled.

    A run that already ended is left alone: no kill, existing status returned.
    """
    if bridge.is_unattended():
        return error(UNATTENDED_CONTEXT, _UNATTENDED_MESSAGE.format(tool="baro_cancel"))
    run_id, err = _run_id_arg(args)
    if err:
        return err
    runs = RunRepository(bridge.store())
    board, handle, err = _load(runs, run_id)
    if err:
        return err
    if board.status in TERMINAL_STATUSES:
        return {"ok": True, "run_id": run_id, "status": board.status, "already_finished": True}
    kill_err = _kill(bridge, handle["process_id"])
    if kill_err:
        return kill_err
    board.mark_cancelled(at=_now())
    runs.save_board(board)
    return {"ok": True, "run_id": run_id, "status": "cancelled", "already_finished": False}


HANDLERS = {
    "baro_plan": handle_baro_plan,
    "baro_run": handle_baro_run,
    "baro_status": handle_baro_status,
    "baro_cancel": handle_baro_cancel,
}
