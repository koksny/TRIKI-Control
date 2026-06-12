"""Session logging: a non-blocking JSONL recorder for full-pipeline replay.

Captures everything needed to reconstruct a real session after the fact:
connection/status changes, every motion sample with its 6 axes, every gesture
decision (including the ones the detector dropped, WITH the reason), and every
key-output event (down / extend / up / tap). One JSON object per line.

Writes happen on a dedicated daemon thread fed by a queue, so callers on the BLE
notify thread (samples, decisions) never block on disk I/O.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path


class SessionLogger:
    def __init__(self, path: Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._queue: "queue.Queue[dict | None]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="triki-session-log", daemon=True
        )
        self._closed = False
        self._thread.start()

    def log(self, event_type: str, payload: dict | None = None) -> None:
        if self._closed:
            return
        record = {"ts": round(self._clock(), 3), "type": event_type}
        if payload:
            record.update(payload)
        self._queue.put(record)

    def _run(self) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            while True:
                record = self._queue.get()
                if record is None:
                    handle.flush()
                    return
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                if self._queue.empty():
                    handle.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=2.0)
