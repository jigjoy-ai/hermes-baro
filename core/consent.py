"""The human approval a Baro launch needs, decided before the tool runs.

Hermes approves shell commands one at a time, and its default ``smart`` mode
may approve a command on its own. A Baro run is not one command: once started,
Baro's coding agents run many shell commands in the target repository without
asking again. So the person approves the whole run up front, knowing what it
does, through the ``pre_tool_call`` hook directive ``{"action": "approve"}``,
which sends the call to Hermes's human-approval gate whatever the approval
mode is (website/docs/user-guide/features/hooks.md, ``pre_tool_call``).
Denial, timeout or a gate error fails closed, and cron and unattended sessions
are denied by Hermes's own ``cron_mode`` / ``unattended_mode`` defaults.

Pure Python with no Hermes imports, like the rest of ``core``.
"""

from __future__ import annotations

from typing import Any

#: The tools that start Baro, and so start coding agents.
LAUNCH_TOOLS = frozenset({"baro_run", "baro_plan"})

#: Scopes an "always approve" answer to Baro launches only.
RULE_KEY = "baro:launch"

_GOAL_PREVIEW = 200


def launch_approval(tool_name: str, args: Any, backend: dict | None = None) -> dict | None:
    """The ``pre_tool_call`` directive for ``tool_name``, or None for other tools.

    ``backend`` is what the plugin's settings name (core/settings.py
    ``backend_from_settings``), or None when they name none.
    """
    if tool_name not in LAUNCH_TOOLS:
        return None
    args = args if isinstance(args, dict) else {}
    return {
        "action": "approve",
        "message": approval_message(tool_name, args.get("goal"), args.get("repo"), backend),
        "rule_key": RULE_KEY,
    }


def approval_message(tool_name: str, goal: Any, repo: Any, backend: dict | None = None) -> str:
    repo_text = repo if isinstance(repo, str) and repo.strip() else "the repository you named"
    goal_text = _preview(goal)
    if tool_name == "baro_plan":
        opening = (
            f"Baro will plan {goal_text} for {repo_text}. Its planning agents read the "
            "repository and may run shell commands there to explore it, without asking "
            "you again; the run is stopped as soon as the plan is ready."
        )
    else:
        opening = (
            f"Baro will start autonomous coding agents on {repo_text} to do {goal_text}. "
            "They edit files and run shell commands without asking you again, for as long "
            "as the run lasts."
        )
    return " ".join(
        [
            opening,
            "Each agent works in its own git worktree of that repository and its shell "
            "commands are confined to it; Baro integrates the work on a local branch and "
            "does not push or open pull requests (--local-only).",
            _destination(backend),
            "Approve only if you would let a developer run commands in this repository.",
        ]
    )


def _destination(backend: dict | None) -> str:
    """Where the run's model calls go. A tool call cannot choose it (tools.py), so this is the whole answer."""
    if not backend:
        return (
            "Model calls go to the backend Baro is configured with on this machine; "
            "this request cannot change that."
        )
    return (
        f"Model calls go to the Hermes subscription proxy on this machine "
        f"({backend.get('openai_base_url')}, model {backend.get('model')}), as set in this "
        "plugin's settings; this request cannot change that."
    )


def _preview(goal: Any) -> str:
    if not isinstance(goal, str) or not goal.strip():
        return "the goal you gave"
    text = " ".join(goal.split())
    if len(text) > _GOAL_PREVIEW:
        text = text[: _GOAL_PREVIEW - 1].rstrip() + "…"
    return f'"{text}"'
