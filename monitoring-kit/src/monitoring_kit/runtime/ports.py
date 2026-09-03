"""审计和可观测性钩子。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from ..contracts.audit import AuditEvent


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None:
        ...


class TelemetrySink(Protocol):
    def emit(self, name: str, value: float, attributes: Mapping[str, str]) -> None:
        ...
