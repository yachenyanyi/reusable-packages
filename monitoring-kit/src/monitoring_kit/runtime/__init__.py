"""运行装配、显式扩展注册、可靠投递和工作分配。"""

from .clock import SystemClock
from .dispatcher import OutboxDispatcher
from .model import (
    AllocationRequest,
    DeliveryClaim,
    DeliveryFailure,
    DeliveryGuarantee,
    DeliveryRetryPolicy,
    DeliveryStatus,
    DispatchRequest,
    DispatchSummary,
)
from .registry import ExtensionRegistry

__all__ = [
    "AllocationRequest",
    "DeliveryClaim",
    "DeliveryFailure",
    "DeliveryGuarantee",
    "DeliveryRetryPolicy",
    "DeliveryStatus",
    "DispatchRequest",
    "DispatchSummary",
    "ExtensionRegistry",
    "OutboxDispatcher",
    "SystemClock",
]
