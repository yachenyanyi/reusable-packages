"""内存事件接收器，便于测试至少一次事件语义。"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from ...contracts.change import ChangeEvent
from ...contracts.audit import AuditEvent


class InMemoryEventSink:
    def __init__(self) -> None:
        self._events: list[ChangeEvent] = []
        self._lock = threading.RLock()

    def publish(self, events: Sequence[ChangeEvent]) -> None:
        with self._lock:
            self._events.extend(events)

    @property
    def events(self) -> tuple[ChangeEvent, ...]:
        with self._lock:
            return tuple(self._events)


class InMemoryAuditSink:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.RLock()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)


class InMemoryTelemetrySink:
    def __init__(self) -> None:
        self._measurements: list[tuple[str, float, dict[str, str]]] = []
        self._lock = threading.RLock()

    def emit(self, name: str, value: float, attributes: dict[str, str]) -> None:
        with self._lock:
            self._measurements.append((name, value, dict(attributes)))

    @property
    def measurements(self) -> tuple[tuple[str, float, dict[str, str]], ...]:
        with self._lock:
            return tuple(self._measurements)
