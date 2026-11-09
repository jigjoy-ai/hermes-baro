"""What each Baro agent is doing, read from the mirrored event stream.

The board (core/board.py) says where each story stands; this says what its
agent is doing right now: the commands it runs, what it says, the files it
changes, its tests and the Critic's verdict, plus how many agents are at
work. It is computed on every read from core/events_file.py's mirror and is
never stored, so plugin state stays small.

Event shapes follow packages/baro-orchestrator/src/tui-protocol.ts: ``activity``
lines carry ``{id, kind, text}`` with kind ``tool_call``, ``tool_result``,
``agent_msg``, ``file_change``, ``test`` or ``verdict``; ``story_log`` carries
``{id, line}``; ``routed`` carries ``{id, backend, model}``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

MAX_STEPS = 60
MAX_TEXT = 300

# Kinds worth a row in the step list; tool output is noise at a glance.
_STEP_KINDS = {
    "tool_call": "run",
    "agent_msg": "say",
    "file_change": "file",
    "test": "test",
    "verdict": "verdict",
}
# .../baro-worktrees/<run>/<story>/<repo>/path -> path
_WORKTREE_PATH = re.compile(r"\S*/baro-worktrees/[^/\s]+/[^/\s]+/[^/\s]+/")
_LEASE = re.compile(r"lease granted to \S+ → (\S+)")
# Baro forwards Mozaik coordination to the story's log lane (execution/forwarders/coordination.ts).
_PEER = re.compile(r"^\[peer/(note|help|discovered)\] (.*)$", re.S)
_PEER_LABEL = {"note": "told the team", "help": "asked for help", "discovered": "found follow-up work"}
MAX_TEAM = 40


def _short(text: str) -> str:
    line = _WORKTREE_PATH.sub("", str(text)).strip()
    return line if len(line) <= MAX_TEXT else line[: MAX_TEXT - 1].rstrip() + "…"


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _story(stories: dict, sid: str) -> dict:
    return stories.setdefault(
        sid,
        {
            "model": None,
            "attempt": 0,
            "started_at": None,
            "ended_at": None,
            "status": "waiting",
            "steps": [],
            "files": [],
            "verdict": None,
            "last_step": None,
        },
    )


def _step(story: dict, at: str | None, kind: str, text: str) -> None:
    entry = {"at": at, "kind": kind, "text": _short(text)}
    story["steps"].append(entry)
    del story["steps"][:-MAX_STEPS]
    story["last_step"] = entry


# What the plan lane says, put plainly for the planning view.
_PLAN_PHRASES = (
    (re.compile(r"^sizing the goal"), "Sizing the goal"),
    (re.compile(r"^validating the candidate goal"), "Architect: checking the goal against the repository"),
    (re.compile(r"^scout dispatched: (.*)"), "Scout asked: {0}"),
    (re.compile(r"^scout answered: (.*?)(?: — .*)?$"), "Scout answered: {0}"),
    (re.compile(r"^scout FAILED: (.*)"), "Scout could not answer: {0}"),
    (re.compile(r"^Architect is still validating"), None),
    (re.compile(r"^architecture obligations: batch started \(batch=(\d+)"), "Writing acceptance checks, part {0}"),
    (re.compile(r"^architecture obligations: batch completed \(batch=(\d+).*obligations=(\d+)"),
     "Acceptance checks part {0} done ({1})"),
)
_PHASES = ("Understanding the goal", "Architect", "Acceptance checks", "Planning stories")


def _plan_phrase(line: str) -> str | None:
    for pattern, template in _PLAN_PHRASES:
        found = pattern.search(line)
        if found:
            return template.format(*found.groups()) if template else None
    return line


def summarize(text: str) -> dict:
    """Agents and their recent steps, from the mirrored stdout of one Baro run."""
    stories: dict[str, dict] = {}
    plan_note = None
    plan_steps: list[dict] = []
    team: list[dict] = []
    phase = 0
    first = last = None
    for raw in text.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        at = event.get("ts") if isinstance(event.get("ts"), str) else None
        stamp = _ts(at)
        if stamp:
            first = first or stamp
            last = stamp
        kind = event.get("type")
        sid = event.get("id") if isinstance(event.get("id"), str) else ""
        story_lane = bool(re.fullmatch(r"S\d+", sid))
        if kind == "story_log" and sid == "plan":
            line = str(event.get("line", ""))
            plan_note = _short(line)
            phrase = _plan_phrase(line)
            if phrase:
                plan_steps.append({"at": at, "text": _short(phrase)})
                del plan_steps[:-MAX_STEPS]
            if line.startswith(("validating the candidate goal", "scout ")):
                phase = max(phase, 1)
            elif line.startswith("architecture obligations"):
                phase = max(phase, 2)
        elif kind == "architect_complete":
            phase = max(phase, 3)
            plan_steps.append({"at": at, "text": "Architect done: design decisions written; planning the stories"})
        elif kind == "init" and isinstance(event.get("stories"), list) and event["stories"]:
            plan_steps.append({"at": at, "text": f"Plan ready: {len(event['stories'])} stories"})
        if kind == "replan" and stories:
            added = [str(x.get("id")) for x in event.get("added") or [] if isinstance(x, dict)]
            rewired = [str(x.get("id")) for x in event.get("rewired") or [] if isinstance(x, dict)]
            what = ", ".join(filter(None, [
                f"added {', '.join(added)}" if added else "",
                f"rewired {', '.join(rewired)}" if rewired else "",
            ]))
            team.append({"at": at, "from": "plan", "to": [], "kind": "replan",
                         "label": "the plan changed while the agents worked",
                         "text": _short(what if str(event.get("reason") or "").startswith("progressive planner") or not event.get("reason") else str(event.get("reason")))})
        if not story_lane:
            continue
        story = _story(stories, sid)
        if kind == "story_start":
            story["attempt"] += 1
            story["status"] = "working"
            story["started_at"] = at
            story["ended_at"] = None
            _step(story, at, "start", f"attempt {story['attempt']} started")
        elif kind == "story_complete":
            story["status"] = "done"
            story["ended_at"] = at
            _step(story, at, "done", "merged")
        elif kind == "story_error":
            error = str(event.get("error") or "")
            paused = error.startswith("suspended on dependency block")
            story["status"] = "waiting" if paused else "failed"
            story["ended_at"] = at
            if paused:
                team.append({"at": at, "from": sid, "to": [], "kind": "blocked",
                             "label": "paused until the stories it needs are done", "text": ""})
            _step(story, at, "pause" if paused else "error",
                  "waiting for the stories it depends on" if paused else error)
        elif kind == "routed":
            backend, model = event.get("backend"), event.get("model")
            if model:
                story["model"] = f"{backend}:{model}" if backend else str(model)
        elif kind == "story_log":
            line = str(event.get("line", ""))
            lease = _LEASE.search(line)
            if lease:
                story["model"] = lease.group(1)
            peer = _PEER.match(line)
            if peer:
                peer_kind, body = peer.groups()
                # Delivered live to every other agent at work at that moment.
                to = [other for other, s in stories.items() if other != sid and s["status"] == "working"]
                team.append({"at": at, "from": sid, "to": to, "kind": peer_kind,
                             "label": _PEER_LABEL[peer_kind], "text": _short(body)})
                del team[:-MAX_TEAM]
                story["notes_sent"] = story.get("notes_sent", 0) + 1
                for other in to:
                    stories[other]["notes_received"] = stories[other].get("notes_received", 0) + 1
                _step(story, at, "team", f"{_PEER_LABEL[peer_kind]}: {body}")
        elif kind == "activity":
            label = _STEP_KINDS.get(str(event.get("kind")))
            if label:
                body = str(event.get("text") or "")
                _step(story, at, label, body)
                if label == "file":
                    path = _short(body)
                    if path and path not in story["files"]:
                        story["files"].append(path)
                if label == "verdict":
                    story["verdict"] = _short(body)
        elif kind == "turn_review":
            status = event.get("status") or event.get("verdict")
            reasoning = str(event.get("reasoning") or "")
            story["verdict"] = _short(f"{status}: {reasoning}" if status else reasoning)
    working = sum(1 for s in stories.values() if s["status"] == "working")
    done = sum(1 for s in stories.values() if s["status"] == "done")
    return {
        "agents": {"working": working, "done": done, "total": len(stories)},
        "plan_note": plan_note,
        "plan_steps": plan_steps,
        "team": team,
        "phase": {"index": phase, "names": list(_PHASES)},
        "elapsed_secs": int((last - first).total_seconds()) if first and last else 0,
        "stories": stories,
    }
