# -*- coding: utf-8 -*-
"""任务消息、取消与可中断等待。"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any


class TaskCancelled(Exception):
    """用户主动取消当前任务。"""


@dataclass(slots=True)
class TaskMessage:
    kind: str
    payload: Any = None
    extra: Any = None

    def __iter__(self):
        yield self.kind
        yield self.payload
        yield self.extra


def ensure_not_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelled("任务已取消")


def interruptible_wait(seconds: float, cancel_event: threading.Event | None) -> None:
    """等待指定时长，并能在 0.1 秒内响应取消。"""
    if seconds <= 0:
        ensure_not_cancelled(cancel_event)
        return
    deadline = time.monotonic() + seconds
    while True:
        ensure_not_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if cancel_event is not None:
            if cancel_event.wait(min(0.1, remaining)):
                raise TaskCancelled("任务已取消")
        else:
            time.sleep(min(0.1, remaining))
