"""Baro's event stream, mirrored to a file the Desktop board reads directly.

``baro_run`` pipes Baro's stdout through ``tee`` into the plugin's own data
directory. The board backend (dashboard/plugin_api.py) replays that file, so
the kanban moves while Baro works; before this it moved only when the model
called ``baro_status``, and a model that waits for Hermes's exit notification
calls it once, at the end.
"""

from __future__ import annotations

from pathlib import Path

from .board import TERMINAL_STATUSES, Board
from .events import LineBuffer


def events_path(data_dir: str | Path, run_id: str) -> Path:
    return Path(data_dir) / "events" / f"{run_id}.jsonl"


def live_board(stored: Board, text: str) -> Board:
    """``stored`` replayed from Baro's own event text; a finished board stays as stored."""
    if stored.status in TERMINAL_STATUSES or not text:
        return stored
    board = Board(
        run_id=stored.run_id,
        goal=stored.goal,
        repo=stored.repo,
        baro_version=stored.baro_version,
        status="running",
        started_at=stored.started_at,
    )
    buffer = LineBuffer()
    for line in buffer.feed(text):
        board.apply_line(line)
    # A replay that has seen nothing yet must not hide what the store knows.
    return board if board.events_applied else stored
