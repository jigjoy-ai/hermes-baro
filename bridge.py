"""The only module that touches the Hermes plugin context (ADR-009), plus ``register(ctx)``.

Every Hermes contract used here was read from the reference checkout first
(paths are relative to ``reference-hermes-agent/``):

- Registration. website/docs/developer-guide/plugins/index.md:895-930 has ``register(ctx)``
  call ``ctx.register_tool(name=, toolset=, schema=, handler=)``, and the real
  signature is hermes_cli/plugins.py:463-466. Handlers follow
  developer-guide/plugins/index.md:885-892: ``handler(args, **kwargs) -> str``,
  always JSON, and they never raise. Built-in toolsets are registered the same
  way, one name per toolset: plugins/kanban/ is the Desktop half of the kanban
  feature, and its tools sit in toolset ``kanban`` (tools/kanban_tools.py:1314).
- Desktop surface. Nothing is registered in code for it. Hermes and
  ``hermes plugins validate`` find ``desktop/plugin.js`` by its path
  (developer-guide/plugins/index.md:700-703;
  hermes_cli/plugin_validate_desktop.py:1-8). Its read-only backend is
  ``dashboard/``, as in plugins/kanban/dashboard/manifest.json:13.
- Gated commands. developer-guide/plugins/index.md:934 and :938-955 say
  ``ctx.dispatch_tool("terminal", {...})`` "goes through the normal approval,
  redaction, and budget pipelines". The signature is in
  developer-guide/plugins/index.md:1886 and hermes_cli/plugins.py:676-684. The
  terminal tool's ``background``/``workdir`` parameters are at
  tools/terminal_tool.py:1624-1637, and its approval guard runs before any spawn,
  at tools/terminal_tool.py:1562 and :1114-1138.
- Process output and kill. These go through the ``process_manage`` tool
  (tools/process_registry.py:2567-2612, :2747), dispatched the same way.
- Storage. ``ctx.state`` is described in developer-guide/plugins/index.md:935
  and :972-985, and implemented at hermes_cli/plugins.py:282-285.
- Unattended contexts. developer-guide/plugins/index.md:1936-1938 gives
  ``ctx.current_cron_execution()``, and :1822 gives
  ``gateway.session_context.get_session_env("HERMES_SESSION_PLATFORM")``.

Nothing here approves on the user's behalf. No tool argument skips or relaxes a
guard. Process control goes only through the dispatched Hermes tools, never
through this process. No Hermes module is imported at import time.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .tools import DISPATCH_FAILED, HANDLERS, TOOL_SCHEMAS, ApprovalDenied, DispatchError, error

TOOLSET = "baro"
TOOL_NAMES = ("baro_plan", "baro_run", "baro_status", "baro_cancel")

# Terminal results whose ``status`` means Hermes refused to run the command: the user
# declined, the guard blocked it, or the approval is still unanswered.
# tools/terminal_tool.py:1122-1138 ("pending_approval", "blocked"),
# tools/approval.py:593 ("approval_required").
_NOT_APPROVED = frozenset({"blocked", "pending_approval", "approval_required"})

# Programmatic platforms with nobody to answer an approval (tools/approval_context.py:175).
_UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})
# Session surfaces that are not a human messaging channel. This mirrors
# gateway/session_context.py:200-203. Any other non-empty platform or source is a
# messaging gateway session (that file's default-deny rule, :206-212).
_NON_MESSAGING = frozenset({
    "", "api_server", "cli", "codex", "desktop", "gateway", "kanban", "local",
    "msgraph_webhook", "tool", "tui", "webhook",
})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# process_manage "log" pages by line (tools/process_registry.py:2017-2047). The
# registry keeps a rolling 200,000-character tail (tools/process_registry.py:59),
# so this limit always covers the whole retained output.
_LOG_LINE_LIMIT = 1_000_000
# Characters of the last-seen output kept to find our place again after that tail rolls.
_ANCHOR_CHARS = 512


def _session_env(name: str) -> str:
    """A session-scoped Hermes variable, read the way developer-guide/plugins/index.md:1822 says.

    Hermes's own approval code falls back to the process value the same way when
    the gateway package is absent (tools/approval_context.py:151-159).
    """
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return os.getenv(name, "") or ""
    return get_session_env(name, "") or ""


class HermesBridge:
    """The bridge interface that tools.py handlers call (see the tools.py docstring)."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        # process_id -> (virtual offset of log char 0, virtual end at last read, tail)
        self._logs: dict[str, tuple[int, int, str]] = {}

    # ------------------------------------------------------------ context

    def is_unattended(self) -> bool:
        """True in a cron run or a messaging-gateway session: nobody can answer an approval prompt.

        Cron: ``ctx.current_cron_execution()`` (developer-guide/plugins/index.md:1938,
        hermes_cli/plugins.py:411-414). The ``HERMES_CRON_SESSION`` session flag that
        Hermes's approval gate uses (tools/approval_context.py:167-169) is checked as
        well. It also covers code inside a cron run that is not the run itself.
        Gateway: the session platform or source (developer-guide/plugins/index.md:1802,
        :1822), classified as in gateway/session_context.py:206-212, plus the
        unattended programmatic platforms (tools/approval_context.py:175-190).
        """
        try:
            if self._ctx.current_cron_execution() is not None:
                return True
        except Exception:  # no cron identity here; fall through to the session flags
            pass
        if _session_env("HERMES_CRON_SESSION").strip().lower() in _TRUTHY:
            return True
        platform = (os.getenv("HERMES_PLATFORM") or _session_env("HERMES_SESSION_PLATFORM"))
        platform = platform.strip().lower()
        if platform in _UNATTENDED_PLATFORMS:
            return True
        source = _session_env("HERMES_SESSION_SOURCE").strip().lower()
        return any(ident not in _NON_MESSAGING for ident in (platform, source))

    def store(self) -> "PluginStateStore":
        """The BoardStore over ``ctx.state`` (developer-guide/plugins/index.md:972-985)."""
        return PluginStateStore(self._ctx.state)

    # ---------------------------------------------------------- dispatch

    def _dispatch(self, tool: str, args: dict) -> dict:
        try:
            raw = self._ctx.dispatch_tool(tool, args)
        except Exception as exc:  # dispatch normally reports errors as JSON instead
            raise DispatchError(f"{tool} dispatch failed: {exc}") from exc
        try:
            reply = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise DispatchError(f"{tool} returned a non-JSON result") from exc
        if not isinstance(reply, dict):
            raise DispatchError(f"{tool} returned an unexpected result")
        return reply

    def start_background(self, command: str, cwd: str) -> dict:
        """Start ``command`` in ``cwd`` as a Hermes background process. Returns ``{process_id}``.

        Dispatches the ``terminal`` tool with ``background: true`` and ``workdir``
        (tools/terminal_tool.py:1624-1637). The approval guard runs on the command
        before anything is spawned (tools/terminal_tool.py:1562). The reply carries
        ``session_id`` (tools/terminal_tool_background.py:184), which identifies the
        process to ``process_manage``.
        """
        reply = self._dispatch("terminal", {"command": command, "background": True, "workdir": cwd})
        message = str(reply.get("error") or "")
        if reply.get("status") in _NOT_APPROVED:
            raise ApprovalDenied(message or f"Hermes did not run the command ({reply.get('status')})")
        if message:
            raise DispatchError(message)
        session_id = reply.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise DispatchError("the terminal tool returned no background session_id")
        return {"process_id": session_id}

    def read_output(self, process_id: str, offset: int) -> tuple[str, int, bool]:
        """Return the output from character ``offset`` on, the new offset and whether the process exited.

        Uses ``process_manage`` action ``log`` from line 0
        (tools/process_registry.py:2017-2047). That returns the retained output
        with ANSI stripped, joined by "\\n", and ``status`` "exited" or "running"
        (:1988-1989). The text has no trailing newline, so tools.py holds the last
        line back until more output arrives or the process exits.

        Offsets are virtual. When the 200,000-character tail rolls
        (tools/process_registry.py:639-640), the last-read tail is found again in
        the new text so the same offsets keep pointing at the same characters.
        """
        reply = self._dispatch(
            "process_manage",
            {"action": "log", "session_id": process_id, "offset": 0, "limit": _LOG_LINE_LIMIT},
        )
        if reply.get("status") == "not_found" or reply.get("error"):
            raise DispatchError(str(reply.get("error") or f"no Hermes process {process_id}"))
        full = reply.get("output") or ""
        if not isinstance(full, str):
            raise DispatchError("process_manage log returned no text output")
        base = self._log_base(process_id, full)
        start = offset - base
        if start < 0:  # that output already rolled out of Hermes's buffer: it is gone
            base, start = offset, 0
        start = min(start, len(full))
        self._logs[process_id] = (base, base + len(full), full[-_ANCHOR_CHARS:])
        text = full[start:]
        return text, offset + len(text), reply.get("status") == "exited"

    def _log_base(self, process_id: str, full: str) -> int:
        """Virtual offset of ``full[0]``: unchanged unless the buffer rolled since the last read."""
        seen = self._logs.get(process_id)
        if seen is None:
            return 0
        base, end, tail = seen
        at = end - base
        if at <= len(full) and full[at - len(tail):at] == tail:
            return base
        found = full.rfind(tail) if tail else -1
        if found >= 0:
            return end - (found + len(tail))
        return end  # rolled past everything seen: what is left is all new

    def kill(self, process_id: str) -> None:
        """Kill the background process and its whole tree via ``process_manage`` action ``kill``.

        Process-tree termination happens in Hermes, cited here:
        - The process starts in its own session (tools/process_registry.py:1221-1225,
          ``start_new_session``).
        - Kill takes a snapshot of every descendant, SIGTERMs the root, and then
          SIGTERMs and SIGKILLs the survivors
          (tools/process_registry_termination.py:33-42, called from
          tools/process_registry.py:2241-2244). It also stops the systemd scope that
          holds double-forked descendants (:2160-2162).
        - It then checks that no process in the tree survived, and reports
          "Kill incomplete" if any did (tools/process_registry.py:2163-2198,
          tools/process_registry_termination.py:149-161).
        Any error status is raised, so baro_cancel never reports a live tree as stopped.
        "already_exited" counts as success: the kill still stops the scope's survivors
        (tools/process_registry.py:2136-2155).
        """
        reply = self._dispatch("process_manage", {"action": "kill", "session_id": process_id})
        status = reply.get("status")
        if status in _NOT_APPROVED:
            raise ApprovalDenied(str(reply.get("error") or "Hermes did not run the kill"))
        if status in ("error", "not_found") or reply.get("error"):
            raise DispatchError(str(reply.get("error") or f"kill of {process_id} failed ({status})"))


class PluginStateStore:
    """core.store.BoardStore over ``ctx.state`` (hermes_cli/plugins_state.py:173-195).

    PluginState stores JSON values natively, so values pass through unchanged.
    RunRepository's eviction writes ``None``, which is stored as JSON null and
    read back as absent.
    """

    def __init__(self, state: Any) -> None:
        self._state = state

    def get(self, key: str) -> Any | None:
        return self._state.get(key, None)

    def set(self, key: str, value: Any) -> None:
        self._state.set(key, value)


# ---------------------------------------------------------------- register


def _tool_handler(name: str, bridge: Callable[[], HermesBridge]) -> Callable[..., str]:
    """Adapt ``tools.handle_<name>(bridge, args) -> dict`` to developer-guide/plugins/index.md:885-892."""
    handle = HANDLERS[name]

    def handler(args: Any = None, **kwargs: Any) -> str:
        try:
            result = handle(bridge(), args if args is not None else {})
        except Exception as exc:  # a handler must never raise into Hermes
            result = error(DISPATCH_FAILED, f"{name} failed unexpectedly: {exc}")
        return json.dumps(result, ensure_ascii=False)

    handler.__name__ = f"{name}_handler"
    return handler


def register(ctx: Any) -> None:
    """Register the four ``baro`` tools (developer-guide/plugins/index.md:917-930).

    The bridge is built on the first tool call, not here.
    ``hermes plugins validate`` runs ``register()`` against a recording stub with
    no ``state`` (hermes_cli/plugin_validate.py:227-268), so register touches
    nothing but ``register_tool``.
    """
    holder: list[HermesBridge] = []

    def bridge() -> HermesBridge:
        if not holder:
            holder.append(HermesBridge(ctx))
        return holder[0]

    for name in TOOL_NAMES:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=TOOL_SCHEMAS[name],
            handler=_tool_handler(name, bridge),
        )
