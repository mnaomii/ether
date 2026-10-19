from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Coroutine

log = logging.getLogger("ether_gui")

MAX_NICK_BYTES = 31
MAX_PASS_BYTES = 64
MAX_FILE_BYTES = 50 * 1024 * 1024
TYPING_TTL = 3.0            # seconds a TYPING_EVT stays visible
TYPING_SEND_INTERVAL = 1.0  # GUI-side throttle; core throttles too

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine, on_error: Callable[[BaseException], None] | None = None) -> asyncio.Task:
    """Fire-and-forget a coroutine from a Qt slot, keeping a reference and surfacing errors."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        if on_error:
            on_error(exc)
        else:
            log.error("background task failed", exc_info=exc)

    task.add_done_callback(_done)
    return task


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def initials(nick: str) -> str:
    parts = [p for p in re.split(r"[\s._\-]+", nick) if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).lower()
    return nick[:2].lower()
