"""Скорость работы за последние секунды: файлы в секунду, скачивание и перенос в МБ/с."""
from __future__ import annotations

import threading
import time
from collections import deque

FILES, DOWNLOAD, MOVE = "files", "download", "move"


class Speedometer:
    def __init__(self, window: float = 10.0, clock=time.monotonic):
        self.window = window
        self.clock = clock
        self._events: deque[tuple[float, str, float]] = deque()
        self._lock = threading.Lock()
        self._started = clock()

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._started = self.clock()

    def add(self, kind: str, amount: float = 1) -> None:
        now = self.clock()
        with self._lock:
            self._events.append((now, kind, amount))
            self._trim(now)

    def _trim(self, now: float) -> None:
        while self._events and self._events[0][0] < now - self.window:
            self._events.popleft()

    def rates(self) -> dict[str, float]:
        """Сколько в секунду по каждому виду; виды без событий в окне не попадают."""
        now = self.clock()
        with self._lock:
            self._trim(now)
            totals: dict[str, float] = {}
            for _, kind, amount in self._events:
                totals[kind] = totals.get(kind, 0) + amount
        span = max(min(self.window, now - self._started), 1.0)
        return {k: v / span for k, v in totals.items()}

    def text(self) -> str:
        r = self.rates()
        parts = []
        if FILES in r:
            parts.append(f"{r[FILES]:.1f} файл/с")
        if DOWNLOAD in r:
            parts.append(f"скачивание {r[DOWNLOAD] / 1024**2:.1f} МБ/с")
        if MOVE in r:
            parts.append(f"перенос {r[MOVE] / 1024**2:.1f} МБ/с")
        return " · ".join(parts)
