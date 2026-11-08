"""FakeBridge: the HermesBridge surface (ADR-009) scripted from recorded fixtures.

The process output is the fixture lines joined as stdout. It "arrives" one
scripted chunk per ``read_output`` call, and ``read_output`` returns the
cumulative output from the requested offset up to what has arrived. That is
how a real background process log behaves: re-reading an earlier offset gives
the same text again. Every call is recorded in ``calls``.
"""

from __future__ import annotations

from baro_plugin.core.store import MemoryStore
from baro_plugin.tools import ApprovalDenied, DispatchError


def stdout_of(lines: list[str]) -> str:
    """The stdout text of a recorded fixture: one line per event."""
    return "".join(line + "\n" for line in lines)


def chunks_by_line(lines: list[str]) -> list[str]:
    """One chunk per fixture line."""
    return [line + "\n" for line in lines]


def chunks_mid_line(lines: list[str]) -> list[str]:
    """Chunks that each end in the middle of a line (and one empty read)."""
    text = stdout_of(lines)
    out, prev, start = [], 0, 0
    for line in lines:
        cut = start + max(1, len(line) // 2)
        start += len(line) + 1
        out.append(text[prev:cut])
        prev = cut
    out.append("")  # a poll that finds no new output
    out.append(text[prev:])
    return out


class FakeBridge:
    def __init__(
        self,
        chunks: list[str] | None = None,
        *,
        exit_at_end: bool = False,
        unattended: bool = False,
        start_error: Exception | None = None,
        kill_error: Exception | None = None,
        read_error: Exception | None = None,
        process_id: str = "proc-1",
        store: MemoryStore | None = None,
        settings: dict | None = None,
        reachable: bool = True,
        events_dir: str | None = None,
    ) -> None:
        self._events_dir = events_dir
        self.notify_flags: list[bool] = []
        self._settings = dict(settings or {})
        self._reachable = reachable
        self.probed: list[str] = []
        self.chunks = list(chunks or [])
        self.exit_at_end = exit_at_end
        self.unattended = unattended
        self.start_error = start_error
        self.kill_error = kill_error
        self.read_error = read_error
        self.process_id = process_id
        self.memory = store if store is not None else MemoryStore()
        self.calls: list[tuple] = []
        self.output = "".join(self.chunks)
        self._next_chunk = 0
        self._arrived = 0
        self.killed = False

    # ---- the five bridge methods

    def is_unattended(self) -> bool:
        self.calls.append(("is_unattended",))
        return self.unattended

    def settings(self) -> dict:
        return dict(self._settings)

    def events_file(self, run_id: str) -> str | None:
        return f"{self._events_dir}/{run_id}.jsonl" if self._events_dir else None

    def endpoint_reachable(self, base_url: str) -> bool:
        self.probed.append(base_url)
        return self._reachable

    def start_background(self, command: str, cwd: str, notify_on_complete: bool = False) -> dict:
        self.calls.append(("start_background", command, cwd))
        self.notify_flags.append(notify_on_complete)
        if self.start_error is not None:
            raise self.start_error
        return {"process_id": self.process_id}

    def read_output(self, process_id: str, offset: int) -> tuple[str, int, bool]:
        self.calls.append(("read_output", process_id, offset))
        if self.read_error is not None:
            raise self.read_error
        if not self.killed and self._next_chunk < len(self.chunks):
            self._arrived += len(self.chunks[self._next_chunk])
            self._next_chunk += 1
        all_arrived = self._next_chunk >= len(self.chunks)
        exited = self.killed or (self.exit_at_end and all_arrived)
        return self.output[offset : self._arrived], self._arrived, exited

    def kill(self, process_id: str) -> None:
        self.calls.append(("kill", process_id))
        if self.kill_error is not None:
            raise self.kill_error
        self.killed = True

    def store(self) -> MemoryStore:
        return self.memory

    # ---- test helpers

    def calls_named(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    @property
    def dispatches(self) -> list[tuple]:
        """Calls that touch the process (start, read, kill)."""
        return [c for c in self.calls if c[0] in ("start_background", "read_output", "kill")]

    @property
    def delivered(self) -> str:
        """The output that has arrived so far."""
        return self.output[: self._arrived]


__all__ = [
    "ApprovalDenied",
    "DispatchError",
    "FakeBridge",
    "chunks_by_line",
    "chunks_mid_line",
    "stdout_of",
]
