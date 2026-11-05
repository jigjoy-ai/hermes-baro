"""Parse Baro's headless event stream into a normalised event model.

``baro --headless`` writes one JSON object per line on stdout. The orchestrator
emits them through ``emit()`` (packages/baro-orchestrator/src/tui-protocol.ts:237-238,
which also stamps ``ts``), and the Rust host re-echoes each line unchanged in
content (crates/baro-tui/src/orchestrator_client.rs:204-217). The wire shapes
are the ``BaroEvent`` union at packages/baro-orchestrator/src/tui-protocol.ts:88-230.

This is the only module that knows Baro's field names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

# Baro wire ``type`` -> how to read it. Keys of each spec:
#   kind      the normalised EventKind
#   story_id  field holding the story id
#   message   field holding free text (progress line / replan reason)
#   error     field holding the error text
#   stories   field holding a list of {id, title, depends_on}
#   rewired   field holding a list of {id, depends_on} (full replacement deps)
#   removed   field holding a list of removed story ids
#   success   field holding the run's boolean success flag
_TYPE_MAP: dict[str, dict[str, str]] = {
    # packages/baro-orchestrator/src/tui-protocol.ts:93 (emitted at
    # packages/baro-orchestrator/src/orchestrate.ts:1906 with the PRD's stories)
    "init": {"kind": "plan", "stories": "stories"},
    # packages/baro-orchestrator/src/tui-protocol.ts:98 (emitted at
    # packages/baro-orchestrator/src/execution/forwarders/story-lifecycle.ts:198;
    # its ``title`` is the agent id, so it is deliberately not mapped)
    "story_start": {"kind": "story_start", "story_id": "id"},
    # packages/baro-orchestrator/src/tui-protocol.ts:100 (raw log line per story;
    # ids also include non-story lanes such as "plan", "_git", "_finalizer")
    "story_log": {"kind": "story_progress", "story_id": "id", "message": "line"},
    # packages/baro-orchestrator/src/tui-protocol.ts:164-174 (condensed activity line)
    "activity": {"kind": "story_progress", "story_id": "id", "message": "text"},
    # packages/baro-orchestrator/src/tui-protocol.ts:101-107 (emitted at
    # packages/baro-orchestrator/src/execution/forwarders/story-lifecycle.ts:258)
    "story_complete": {"kind": "story_complete", "story_id": "id"},
    # packages/baro-orchestrator/src/tui-protocol.ts:108-114 (terminal failure after
    # retries, emitted at packages/baro-orchestrator/src/execution/forwarders/story-lifecycle.ts:237)
    "story_error": {"kind": "story_failed", "story_id": "id", "error": "error"},
    # packages/baro-orchestrator/src/tui-protocol.ts:177-184 (emitted at
    # packages/baro-orchestrator/src/execution/forwarders/dag.ts:134-148)
    "replan": {
        "kind": "replan",
        "message": "reason",
        "stories": "added",
        "rewired": "rewired",
        "removed": "removed",
    },
    # packages/baro-orchestrator/src/tui-protocol.ts:120-131 (emitted at
    # packages/baro-orchestrator/src/orchestrate.ts:2167-2172)
    "done": {"kind": "run_end", "success": "success", "error": "abort_reason"},
}

# packages/baro-orchestrator/src/tui-protocol.ts:238 (every line carries ``ts``)
_TS_FIELD = "ts"
# packages/baro-orchestrator/src/tui-protocol.ts:19-23 (StoryInfo)
_STORY_ID, _STORY_TITLE, _STORY_DEPS = "id", "title", "depends_on"

EventKind = Literal[
    "plan",
    "story_start",
    "story_progress",
    "story_complete",
    "story_failed",
    "replan",
    "run_end",
    "unknown",
]


@dataclass(frozen=True)
class PlannedStory:
    id: str
    # "" when Baro did not restate the title (replan ``rewired`` entries):
    # consumers keep the title they already know.
    title: str
    # Prerequisite story ids. For replans this is the full replacement list.
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class BaroEvent:
    kind: EventKind
    raw: dict
    story_id: str | None = None
    # plan: the whole story set. replan: added stories followed by rewired ones.
    stories: tuple[PlannedStory, ...] = ()
    message: str | None = None
    error: str | None = None
    # run_end only: "completed" or "failed".
    outcome: str | None = None
    # replan only: ids Baro removed from the graph.
    removed: tuple[str, ...] = ()
    # ISO-8601 emission time, when the line carries one.
    ts: str | None = None


def parse_line(line: str) -> BaroEvent | None:
    """Parse one stdout line. Never raises.

    Returns None for blank, non-JSON or non-object lines, and an event of kind
    "unknown" for JSON objects whose type is unmapped or malformed.
    """
    try:
        if not isinstance(line, str) or not line.strip():
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        try:
            return _to_event(obj)
        except Exception:  # defensive: never let one odd line break a run
            return BaroEvent(kind="unknown", raw=obj, ts=_str(obj.get(_TS_FIELD)))
    except Exception:
        return None


def _to_event(obj: dict) -> BaroEvent:
    ts = _str(obj.get(_TS_FIELD))
    spec = _TYPE_MAP.get(obj.get("type")) if isinstance(obj.get("type"), str) else None
    if spec is None:
        return BaroEvent(kind="unknown", raw=obj, ts=ts)
    kind = spec["kind"]

    story_id = None
    if "story_id" in spec:
        story_id = _str(obj.get(spec["story_id"]))
        if not story_id:
            return BaroEvent(kind="unknown", raw=obj, ts=ts)

    stories: list[PlannedStory] = []
    if "stories" in spec:
        stories.extend(_stories(obj.get(spec["stories"]), with_title=True))
    if "rewired" in spec:
        stories.extend(_stories(obj.get(spec["rewired"]), with_title=False))

    removed: tuple[str, ...] = ()
    if "removed" in spec:
        removed = _ids(obj.get(spec["removed"]))

    message = _str(obj.get(spec["message"])) if "message" in spec else None
    error = _str(obj.get(spec["error"])) if "error" in spec else None

    outcome = None
    if "success" in spec:
        success = obj.get(spec["success"])
        # ``success`` is optional on the wire; without it an abort reason
        # is the only failure signal.
        failed = success is False or (success is None and bool(error))
        outcome = "failed" if failed else "completed"

    return BaroEvent(
        kind=kind,
        raw=obj,
        story_id=story_id,
        stories=tuple(stories),
        message=message,
        error=error,
        outcome=outcome,
        removed=removed,
        ts=ts,
    )


def _stories(value: Any, *, with_title: bool) -> list[PlannedStory]:
    out: list[PlannedStory] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        sid = _str(item.get(_STORY_ID))
        if not sid:
            continue
        title = (_str(item.get(_STORY_TITLE)) or "") if with_title else ""
        out.append(PlannedStory(id=sid, title=title, depends_on=_ids(item.get(_STORY_DEPS))))
    return out


def _ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


class LineBuffer:
    """Reassemble complete lines from arbitrarily split stdout chunks."""

    def __init__(self) -> None:
        self._partial = ""

    def feed(self, text: str) -> list[str]:
        """Return the lines completed by ``text``; keep a trailing partial line."""
        if not text:
            return []
        data = self._partial + text
        parts = data.split("\n")
        self._partial = parts.pop()
        return [p[:-1] if p.endswith("\r") else p for p in parts]

    def flush(self) -> list[str]:
        """Return the held-back partial line (once the stream has ended)."""
        rest, self._partial = self._partial, ""
        return [rest.rstrip("\r")] if rest else []
