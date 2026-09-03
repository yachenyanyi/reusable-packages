"""运行时可靠投递与工作分配使用的稳定值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..contracts.change import ChangeEvent
from ..contracts.primitives import ensure_utc, require_text


class DeliveryGuarantee(str, Enum):
    """历史事件的投递保证等级。"""

    NONE = "none"
    AT_LEAST_ONCE = "at_least_once"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class DeliveryRetryPolicy:
    """事件投递的内部退避策略。"""

    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 300.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("事件退避时间不能为负数")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds 不能小于 base_delay_seconds")


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    """一次有限事件投递批次的请求。"""

    worker_id: str
    now: datetime
    batch_size: int = 100
    lease_seconds: float = 60.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_text(self.worker_id, "worker_id"))
        object.__setattr__(self, "now", ensure_utc(self.now, "dispatch.now"))
        if isinstance(self.batch_size, bool) or self.batch_size < 1 or self.batch_size > 1000:
            raise ValueError("batch_size 必须在 1 到 1000 之间")
        if self.lease_seconds <= 0:
            raise ValueError("事件投递租约时间必须大于 0")


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    """传输边界可安全保存的失败摘要。"""

    code: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "code",
            require_text(self.code, "delivery_failure.code", max_length=64),
        )
        object.__setattr__(
            self,
            "message",
            require_text(self.message, "delivery_failure.message", max_length=500),
        )


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    """一次投递租约；版本只用于适配器内部 fencing。"""

    event: ChangeEvent
    attempt_count: int
    lease_owner: str
    lease_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.event, ChangeEvent):
            raise ValueError("DeliveryClaim.event 必须是 ChangeEvent")
        if isinstance(self.attempt_count, bool) or self.attempt_count < 1:
            raise ValueError("attempt_count 必须大于 0")
        object.__setattr__(self, "lease_owner", require_text(self.lease_owner, "lease_owner"))
        if isinstance(self.lease_version, bool) or self.lease_version < 1:
            raise ValueError("lease_version 必须大于 0")


@dataclass(frozen=True, slots=True)
class DispatchSummary:
    claimed: int
    delivered: int
    retried: int
    blocked: int
    lease_lost: int = 0

    def __post_init__(self) -> None:
        for name in ("claimed", "delivered", "retried", "blocked", "lease_lost"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    """一次工作分配请求；数据库锁和公平游标不进入该契约。"""

    worker_id: str
    now: datetime
    batch_size: int = 10
    lease_seconds: float = 60.0
    global_concurrency_limit: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_text(self.worker_id, "worker_id"))
        object.__setattr__(self, "now", ensure_utc(self.now, "allocation.now"))
        if isinstance(self.batch_size, bool) or self.batch_size < 1 or self.batch_size > 1000:
            raise ValueError("batch_size 必须在 1 到 1000 之间")
        if self.lease_seconds <= 0:
            raise ValueError("工作租约时间必须大于 0")
        if self.global_concurrency_limit is not None:
            if isinstance(self.global_concurrency_limit, bool) or self.global_concurrency_limit < 1:
                raise ValueError("global_concurrency_limit 必须大于 0")
