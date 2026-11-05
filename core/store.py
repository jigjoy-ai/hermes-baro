"""Board persistence over a minimal key/value protocol (ADR-006).

The plugin side (bridge.py) adapts the documented Hermes plugin storage
accessor to ``BoardStore``; tests use ``MemoryStore``. Keys:

- ``runs``: list of run ids, oldest first, capped at ``MAX_RUNS``.
- ``run:<run_id>``: the ``Board.to_dict()`` dict.
- ``handle:<run_id>``: ``{"process_id": str, "read_offset": int}``.

Values are JSON-serialisable dicts and lists only. Writing ``None`` to a key
means "absent": it is how evicted runs are deleted.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .board import Board

MAX_RUNS = 50
RUNS_KEY = "runs"


def board_key(run_id: str) -> str:
    return f"run:{run_id}"


def handle_key(run_id: str) -> str:
    return f"handle:{run_id}"


class BoardStore(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any) -> None: ...


class MemoryStore:
    """In-memory BoardStore that only accepts JSON-serialisable values.

    Values are stored as JSON text, so callers never share mutable state with
    the store and non-JSON values fail loudly, as a real store would.
    """

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> Any | None:
        text = self.data.get(key)
        return None if text is None else json.loads(text)

    def set(self, key: str, value: Any) -> None:
        if value is None:
            self.data.pop(key, None)
            return
        self.data[key] = json.dumps(value, allow_nan=False)


class RunRepository:
    def __init__(self, store: BoardStore) -> None:
        self._store = store

    def list_runs(self) -> list[str]:
        runs = self._store.get(RUNS_KEY)
        if not isinstance(runs, list):
            return []
        return [r for r in runs if isinstance(r, str)]

    def save_board(self, board: Board) -> None:
        self._store.set(board_key(board.run_id), board.to_dict())
        runs = self.list_runs()
        if board.run_id in runs:
            return
        runs.append(board.run_id)
        while len(runs) > MAX_RUNS:
            oldest = runs.pop(0)
            self._store.set(board_key(oldest), None)
            self._store.set(handle_key(oldest), None)
        self._store.set(RUNS_KEY, runs)

    def load_board(self, run_id: str) -> Board | None:
        data = self._store.get(board_key(run_id))
        if not isinstance(data, dict):
            return None
        return Board.from_dict(data)

    def save_handle(self, run_id: str, handle: dict) -> None:
        process_id = handle.get("process_id")
        read_offset = handle.get("read_offset")
        if not isinstance(process_id, str) or not process_id:
            raise ValueError("handle.process_id must be a non-empty string")
        if isinstance(read_offset, bool) or not isinstance(read_offset, int) or read_offset < 0:
            raise ValueError("handle.read_offset must be a non-negative integer")
        self._store.set(handle_key(run_id), {"process_id": process_id, "read_offset": read_offset})

    def load_handle(self, run_id: str) -> dict | None:
        data = self._store.get(handle_key(run_id))
        return data if isinstance(data, dict) else None
