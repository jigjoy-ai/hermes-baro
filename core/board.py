"""Dependency-aware run board built from normalised Baro events (ADR-005).

The board keeps a few raw facts per story (title, prerequisites, whether it
started, completed or failed) and derives everything else from them after
every change: ``state``, ``waits_on``, ``blocks`` and ``blocked_by_failure``.
``to_dict()`` is the single shape consumed by baro_status, storage and the
desktop panel. Edges are always ``{"from": prerequisite_id, "to": dependent_id}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import BaroEvent, parse_line

SCHEMA = 1
# Baro reports a dependency suspension as a story_error with this text.
_SUSPENDED_PREFIX = "suspended on dependency block"

RUN_STATUSES = ("planning", "running", "completed", "failed", "cancelled")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
STORY_STATES = ("blocked", "ready", "running", "done", "failed")


@dataclass
class _Story:
    id: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    started: bool = False
    completed: bool = False
    failed: bool = False
    last_progress: str | None = None
    error: str | None = None


class Board:
    """Live board for one Baro run."""

    def __init__(
        self,
        run_id: str,
        goal: str,
        repo: str,
        baro_version: str,
        status: str = "planning",
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"unknown run status {status!r}")
        self.run_id = run_id
        self.goal = goal
        self.repo = repo
        self.baro_version = baro_version
        self.status = status
        self.started_at = started_at
        self.ended_at = ended_at
        self.replans: list[dict] = []
        self.events_applied = 0
        self.unparsed_lines = 0
        # Insertion order is first appearance; removed stories are dropped.
        self._stories: dict[str, _Story] = {}

    # ------------------------------------------------------------------ input

    def apply_line(self, line: str, at: str | None = None) -> BaroEvent | None:
        """Parse one stdout line and apply it.

        Non-blank lines that are not Baro events count in ``unparsed_lines``.
        """
        event = parse_line(line)
        if event is None:
            if isinstance(line, str) and line.strip():
                self.unparsed_lines += 1
            return None
        self.apply(event, at=at)
        return event

    def apply(self, event: BaroEvent, at: str | None = None) -> None:
        """Apply one event. ``at`` is the fallback time when the event has no ``ts``."""
        self.events_applied += 1
        when = event.ts or at
        kind = event.kind
        if kind == "plan":
            for planned in event.stories:
                self._upsert(planned.id, planned.title, planned.depends_on)
            self._promote_to_running()
        elif kind == "replan":
            self._apply_replan(event, when)
        elif kind == "run_end":
            if self.status not in TERMINAL_STATUSES:
                self.status = "failed" if event.outcome == "failed" else "completed"
                self.ended_at = when
        elif kind in ("story_start", "story_progress", "story_complete", "story_failed"):
            # Baro also logs on non-story lanes ("plan", "_git", "_finalizer"):
            # events for ids not on the board are ignored.
            story = self._stories.get(event.story_id or "")
            if story is None:
                return
            if kind == "story_start":
                # A retry or a resumed suspension starts the story again.
                story.started = True
                story.failed = False
                story.error = None
                self._promote_to_running()
            elif kind == "story_progress":
                if event.message is not None:
                    story.last_progress = event.message
            elif kind == "story_complete":
                story.completed = True
            elif (event.error or "").startswith(_SUSPENDED_PREFIX):
                # Not a failure: the story paused until stories it found it
                # needs are done (a runtime replan adds those edges), then
                # starts again. Shown as waiting, not as Failed.
                story.started = False
                story.last_progress = "waiting for the stories it depends on"
            else:
                story.failed = True
                story.error = event.error

    def mark_cancelled(self, at: str | None = None) -> bool:
        """End a live run as cancelled. A run that already ended is left as is.

        Returns True when the status changed.
        """
        if self.status in TERMINAL_STATUSES:
            return False
        self.status = "cancelled"
        self.ended_at = at
        return True

    # ---------------------------------------------------------------- helpers

    def _promote_to_running(self) -> None:
        if self.status == "planning":
            self.status = "running"

    def _upsert(self, story_id: str, title: str, depends_on: tuple[str, ...]) -> None:
        deps = _dedupe(d for d in depends_on if d != story_id)
        story = self._stories.get(story_id)
        if story is None:
            self._stories[story_id] = _Story(id=story_id, title=title, depends_on=deps)
            return
        if title:  # "" means Baro did not restate the title
            story.title = title
        story.depends_on = deps

    def _apply_replan(self, event: BaroEvent, when: str | None) -> None:
        before_ids = set(self._stories)
        before_edges = self._edge_set()

        removed = [sid for sid in _dedupe(event.removed) if sid in self._stories]
        for sid in removed:
            del self._stories[sid]
        added: list[str] = []
        for planned in event.stories:
            if planned.id not in self._stories and planned.id not in added:
                added.append(planned.id)
            self._upsert(planned.id, planned.title, planned.depends_on)
        # Drop edges that point at stories which left the graph.
        gone = set(removed) - set(self._stories)
        if gone:
            for story in self._stories.values():
                story.depends_on = [d for d in story.depends_on if d not in gone]

        new_edges = [e for e in self._edges() if (e["from"], e["to"]) not in before_edges]
        self.replans.append(
            {
                "index": len(self.replans) + 1,
                "at": when,
                # Stories new to the board (Baro's rewired entries for existing
                # stories are not additions).
                "added": [sid for sid in added if sid not in before_ids],
                # Ids actually removed; removing an unknown id is a no-op.
                "removed": removed,
                # Dependency edges introduced by this replan.
                "edges_changed": new_edges,
            }
        )

    def _edges(self) -> list[dict]:
        return [
            {"from": dep, "to": story.id}
            for story in self._stories.values()
            for dep in story.depends_on
        ]

    def _edge_set(self) -> set[tuple[str, str]]:
        return {(e["from"], e["to"]) for e in self._edges()}

    # -------------------------------------------------------------- derivation

    def _state(self, story: _Story) -> str:
        if story.completed:
            return "done"
        if story.failed:
            return "failed"
        if story.started:
            return "running"
        return "ready" if not self._waits_on(story) else "blocked"

    def _is_done(self, story_id: str) -> bool:
        story = self._stories.get(story_id)
        return story is not None and story.completed

    def _waits_on(self, story: _Story) -> list[str]:
        # A prerequisite unknown to the board can never complete, so it waits.
        return [d for d in story.depends_on if not self._is_done(d)]

    def _blocks(self, story: _Story) -> list[str]:
        return [
            other.id
            for other in self._stories.values()
            if story.id in other.depends_on and not other.completed
        ]

    def _blocked_by_failure(self, story: _Story) -> bool:
        seen: set[str] = set()
        stack = list(story.depends_on)
        while stack:
            sid = stack.pop()
            if sid in seen:
                continue
            seen.add(sid)
            prereq = self._stories.get(sid)
            if prereq is None:
                continue
            if prereq.failed and not prereq.completed:
                return True
            stack.extend(prereq.depends_on)
        return False

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "goal": self.goal,
            "repo": self.repo,
            "baro_version": self.baro_version,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stories": [
                {
                    "id": s.id,
                    "title": s.title,
                    "state": self._state(s),
                    "depends_on": list(s.depends_on),
                    "waits_on": self._waits_on(s),
                    "blocks": self._blocks(s),
                    "blocked_by_failure": self._blocked_by_failure(s),
                    "last_progress": s.last_progress,
                    "error": s.error,
                }
                for s in self._stories.values()
            ],
            "replans": [
                {
                    "index": r["index"],
                    "at": r["at"],
                    "added": list(r["added"]),
                    "removed": list(r["removed"]),
                    "edges_changed": [dict(e) for e in r["edges_changed"]],
                }
                for r in self.replans
            ],
            "events_applied": self.events_applied,
            "unparsed_lines": self.unparsed_lines,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Board:
        if d.get("schema") != SCHEMA:
            raise ValueError(f"unsupported board schema {d.get('schema')!r}")
        board = cls(
            run_id=d["run_id"],
            goal=d["goal"],
            repo=d["repo"],
            baro_version=d["baro_version"],
            status=d["status"],
            started_at=d.get("started_at"),
            ended_at=d.get("ended_at"),
        )
        for s in d.get("stories", []):
            state = s.get("state")
            if state not in STORY_STATES:
                raise ValueError(f"unknown story state {state!r}")
            board._stories[s["id"]] = _Story(
                id=s["id"],
                title=s.get("title", ""),
                depends_on=list(s.get("depends_on", [])),
                # The raw flags are recoverable from the derived state.
                started=state == "running",
                completed=state == "done",
                failed=state == "failed",
                last_progress=s.get("last_progress"),
                error=s.get("error"),
            )
        board.replans = [
            {
                "index": r["index"],
                "at": r.get("at"),
                "added": list(r.get("added", [])),
                "removed": list(r.get("removed", [])),
                "edges_changed": [
                    {"from": e["from"], "to": e["to"]} for e in r.get("edges_changed", [])
                ],
            }
            for r in d.get("replans", [])
        ]
        board.events_applied = int(d.get("events_applied", 0))
        board.unparsed_lines = int(d.get("unparsed_lines", 0))
        return board


def _dedupe(ids: Any) -> list[str]:
    out: list[str] = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out
