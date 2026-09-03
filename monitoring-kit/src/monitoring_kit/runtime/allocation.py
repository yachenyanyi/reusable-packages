"""工作分配策略的共享决策，不包含数据库读取或锁实现。"""

from __future__ import annotations

from typing import Any

from .model import AllocationRequest


def candidate_gateway(record: Any) -> str | None:
    if record.upstream_job is not None:
        return record.upstream_job.gateway_key
    return record.upstream_request.gateway_hint


def candidate_policy(record: Any):
    return record.context.runtime_policy


def candidate_sort_key(record: Any):
    policy = candidate_policy(record)
    priority = policy.scheduling_priority if policy is not None else 0
    return (-priority, record.accepted_at, record.run_id)


def rotate_scopes(scopes: list[str], last_scope: str | None) -> list[str]:
    if not scopes or last_scope is None:
        return scopes
    for index, scope in enumerate(scopes):
        if scope > last_scope:
            return scopes[index:] + scopes[:index]
    return scopes


def allocation_allowed(
    record: Any,
    request: AllocationRequest,
    active_scope: dict[str, int],
    active_gateway: dict[str, int],
    active_total: int,
) -> bool:
    policy = candidate_policy(record)
    active_lease = record.lease_until is not None and record.lease_until > request.now
    same_owner = active_lease and record.lease_owner == request.worker_id
    if request.global_concurrency_limit is not None and active_total >= request.global_concurrency_limit and not same_owner:
        return False
    if policy is None:
        return True
    if (
        policy.max_concurrent_runs is not None
        and active_scope.get(record.scope_key, 0) >= policy.max_concurrent_runs
        and not same_owner
    ):
        return False
    gateway_key = candidate_gateway(record)
    gateway_limit = policy.gateway_limits.get(gateway_key) if gateway_key else None
    if gateway_limit is not None and active_gateway.get(gateway_key, 0) >= gateway_limit and not same_owner:
        return False
    return True


def reserve_counts(
    record: Any,
    request: AllocationRequest,
    active_scope: dict[str, int],
    active_gateway: dict[str, int],
) -> None:
    active_lease = record.lease_until is not None and record.lease_until > request.now
    same_owner = active_lease and record.lease_owner == request.worker_id
    if same_owner:
        return
    active_scope[record.scope_key] = active_scope.get(record.scope_key, 0) + 1
    gateway_key = candidate_gateway(record)
    if gateway_key is not None:
        active_gateway[gateway_key] = active_gateway.get(gateway_key, 0) + 1
