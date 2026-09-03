"""审计和可观测性钩子。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from ..contracts.audit import AuditEvent
from ..contracts.change import ChangeEvent
from .model import AllocationRequest, DeliveryClaim, DeliveryFailure, DispatchRequest


class DeliveryLeaseLostError(Exception):
    """投递租约已经被其它 Worker 取代，当前副本不得再确认。"""


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None:
        ...


class TelemetrySink(Protocol):
    def emit(self, name: str, value: float, attributes: Mapping[str, str]) -> None:
        ...


class EventPublisher(Protocol):
    """只负责把已经提交的领域事件传给外部传输介质。"""

    def publish(self, events: Sequence[ChangeEvent]) -> None:
        ...


class EventDeliveryStore(Protocol):
    """事件投递运行时使用的持久化能力；不暴露表或事务。"""

    def claim_pending(self, request: DispatchRequest) -> tuple[DeliveryClaim, ...]:
        ...

    def mark_delivered(self, claim: DeliveryClaim, now: datetime) -> None:
        ...

    def reschedule(
        self,
        claim: DeliveryClaim,
        failure: DeliveryFailure,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        ...

    def block(self, claim: DeliveryClaim, failure: DeliveryFailure, now: datetime) -> None:
        ...


class WorkAllocator(Protocol):
    """跨 Worker 原子分配可运行 Run 的深模块端口。"""

    def allocate(self, request: AllocationRequest):
        ...
