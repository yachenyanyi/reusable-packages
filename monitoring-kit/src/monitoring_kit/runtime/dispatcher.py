"""可靠事件投递运行时。"""

from __future__ import annotations

import math
from datetime import timedelta

from .model import (
    DeliveryFailure,
    DeliveryRetryPolicy,
    DispatchRequest,
    DispatchSummary,
)
from .ports import DeliveryLeaseLostError, EventDeliveryStore, EventPublisher


class OutboxDispatcher:
    """隐藏事件领取、传输、确认、退避和租约恢复的深模块。"""

    def __init__(
        self,
        store: EventDeliveryStore,
        publisher: EventPublisher,
        *,
        retry_policy: DeliveryRetryPolicy | None = None,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._retry_policy = retry_policy or DeliveryRetryPolicy()

    def dispatch_once(self, request: DispatchRequest) -> DispatchSummary:
        claims = self._store.claim_pending(request)
        delivered = retried = blocked = lease_lost = 0
        for claim in claims:
            try:
                self._publisher.publish((claim.event,))
            except Exception as exc:
                failure = _failure_from_exception(exc)
                try:
                    if claim.attempt_count >= self._retry_policy.max_attempts:
                        self._store.block(claim, failure, request.now)
                        blocked += 1
                    else:
                        delay = self._retry_delay(claim.attempt_count)
                        self._store.reschedule(
                            claim,
                            failure,
                            request.now + timedelta(seconds=delay),
                            request.now,
                        )
                        retried += 1
                except Exception as state_error:
                    if _is_lease_lost(state_error):
                        lease_lost += 1
                    else:
                        raise
                continue

            try:
                self._store.mark_delivered(claim, request.now)
                delivered += 1
            except Exception as exc:
                if _is_lease_lost(exc):
                    lease_lost += 1
                else:
                    raise
        return DispatchSummary(
            claimed=len(claims),
            delivered=delivered,
            retried=retried,
            blocked=blocked,
            lease_lost=lease_lost,
        )

    def _retry_delay(self, attempt_count: int) -> float:
        delay = self._retry_policy.base_delay_seconds * math.pow(2, attempt_count - 1)
        return min(delay, self._retry_policy.max_delay_seconds)


def _failure_from_exception(exc: Exception) -> DeliveryFailure:
    code = getattr(exc, "code", "EVENT_PUBLISH_FAILED")
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if not message:
        message = "事件传输失败"
    return DeliveryFailure(str(code)[:64], message[:500])


def _is_lease_lost(exc: Exception) -> bool:
    return isinstance(exc, DeliveryLeaseLostError)
