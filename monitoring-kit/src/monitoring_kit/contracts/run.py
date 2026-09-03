"""运行相关的稳定契约。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any

from .envelope import TypedEnvelope
from .primitives import (
    CORE_CONTRACT_VERSION,
    ensure_utc,
    json_clone,
    new_id,
    require_mapping,
    require_text,
    parse_datetime,
    stable_json,
    validate_contract_version,
)


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RequestedWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = ensure_utc(self.start, "requested_window.start")
        end = ensure_utc(self.end, "requested_window.end")
        if end <= start:
            raise ValueError("requested_window.end 必须晚于 start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RequestedWindow":
        return cls(parse_datetime(value["start"], "requested_window.start"), parse_datetime(value["end"], "requested_window.end"))


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """宿主已经计算出的执行上限，不暴露套餐或权限模型。"""

    max_records: int | None = None
    deadline: datetime | None = None

    def __post_init__(self) -> None:
        if self.max_records is not None and self.max_records < 0:
            raise ValueError("max_records 不能为负数")
        if self.deadline is not None:
            object.__setattr__(self, "deadline", ensure_utc(self.deadline, "deadline"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_records": self.max_records,
            "deadline": self.deadline.isoformat() if self.deadline else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExecutionLimits":
        deadline = value.get("deadline")
        return cls(value.get("max_records"), parse_datetime(deadline, "deadline") if deadline else None)


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """宿主计算出的通用运行约束快照。

    该对象只表达运行时事实，不解释限制来自哪个用户、套餐或合同。
    策略随 Run 保存，确保排队期间配置变化不会让一个 Run 的语义漂移。
    """

    max_concurrent_runs: int | None = None
    scheduling_priority: int = 0
    gateway_limits: Mapping[str, int] = field(default_factory=dict)
    policy_version: str = "default"

    def __post_init__(self) -> None:
        if self.max_concurrent_runs is not None:
            if isinstance(self.max_concurrent_runs, bool) or not isinstance(self.max_concurrent_runs, int):
                raise ValueError("max_concurrent_runs 必须是整数或 None")
            if self.max_concurrent_runs < 1:
                raise ValueError("max_concurrent_runs 必须大于 0")
        if isinstance(self.scheduling_priority, bool) or not isinstance(self.scheduling_priority, int):
            raise ValueError("scheduling_priority 必须是整数")
        if not isinstance(self.gateway_limits, Mapping):
            raise ValueError("gateway_limits 必须是对象")
        normalized: dict[str, int] = {}
        for gateway_key, limit in self.gateway_limits.items():
            gateway_key = require_text(gateway_key, "gateway_limits.gateway_key")
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("gateway_limits 的值必须是大于 0 的整数")
            normalized[gateway_key] = limit
        object.__setattr__(self, "gateway_limits", MappingProxyType(normalized))
        object.__setattr__(self, "policy_version", require_text(self.policy_version, "policy_version"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_concurrent_runs": self.max_concurrent_runs,
            "scheduling_priority": self.scheduling_priority,
            "gateway_limits": dict(self.gateway_limits),
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimePolicy":
        return cls(
            max_concurrent_runs=value.get("max_concurrent_runs"),
            scheduling_priority=value.get("scheduling_priority", 0),
            gateway_limits=value.get("gateway_limits", {}),
            policy_version=value.get("policy_version", "default"),
        )


@dataclass(frozen=True, slots=True)
class RunRequest:
    collection: TypedEnvelope
    source_ref: str | None = None
    requested_window: RequestedWindow | None = None
    correlation_refs: dict[str, Any] = field(default_factory=dict)
    contract_version: str = CORE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.collection, TypedEnvelope):
            raise ValueError("collection 必须是 TypedEnvelope")
        object.__setattr__(self, "contract_version", validate_contract_version(self.contract_version))
        if self.source_ref is not None:
            object.__setattr__(self, "source_ref", require_text(self.source_ref, "source_ref"))
        object.__setattr__(self, "correlation_refs", require_mapping(self.correlation_refs, "correlation_refs"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "collection": self.collection.to_dict(),
            "source_ref": self.source_ref,
            "requested_window": self.requested_window.to_dict() if self.requested_window else None,
            "correlation_refs": json_clone(self.correlation_refs),
        }

    def fingerprint(self) -> str:
        """返回可安全放入索引列的稳定请求摘要。"""

        return hashlib.sha256(stable_json(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunRequest":
        window = value.get("requested_window")
        return cls(
            collection=TypedEnvelope.from_dict(value["collection"]),
            source_ref=value.get("source_ref"),
            requested_window=RequestedWindow.from_dict(window) if window else None,
            correlation_refs=value.get("correlation_refs", {}),
            contract_version=value.get("contract_version", CORE_CONTRACT_VERSION),
        )


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    scope_key: str
    actor_ref: str
    idempotency_key: str
    limits: ExecutionLimits | None = None
    trace_ref: str | None = None
    runtime_policy: RuntimePolicy | None = None

    def __post_init__(self) -> None:
        for field_name in ("scope_key", "actor_ref", "idempotency_key"):
            object.__setattr__(self, field_name, require_text(getattr(self, field_name), field_name))
        if self.trace_ref is not None:
            object.__setattr__(self, "trace_ref", require_text(self.trace_ref, "trace_ref"))
        if self.limits is not None and not isinstance(self.limits, ExecutionLimits):
            raise ValueError("limits 必须是 ExecutionLimits")
        if self.runtime_policy is not None and not isinstance(self.runtime_policy, RuntimePolicy):
            raise ValueError("runtime_policy 必须是 RuntimePolicy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "actor_ref": self.actor_ref,
            "idempotency_key": self.idempotency_key,
            "limits": self.limits.to_dict() if self.limits else None,
            "trace_ref": self.trace_ref,
            "runtime_policy": self.runtime_policy.to_dict() if self.runtime_policy else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExecutionContext":
        limits = value.get("limits")
        runtime_policy = value.get("runtime_policy")
        return cls(
            scope_key=value["scope_key"],
            actor_ref=value["actor_ref"],
            idempotency_key=value["idempotency_key"],
            limits=ExecutionLimits.from_dict(limits) if limits else None,
            trace_ref=value.get("trace_ref"),
            runtime_policy=RuntimePolicy.from_dict(runtime_policy) if runtime_policy else None,
        )


@dataclass(frozen=True, slots=True)
class RunRef:
    run_id: str
    accepted_at: datetime
    status: RunStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", require_text(self.run_id, "run_id"))
        object.__setattr__(self, "accepted_at", ensure_utc(self.accepted_at, "accepted_at"))
        object.__setattr__(self, "status", RunStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "accepted_at": self.accepted_at.isoformat(), "status": self.status.value}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunRef":
        return cls(
            run_id=value["run_id"],
            accepted_at=parse_datetime(value["accepted_at"], "accepted_at"),
            status=RunStatus(value["status"]),
        )


@dataclass(frozen=True, slots=True)
class RunError:
    code: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_text(self.code, "error.code"))
        object.__setattr__(self, "message", require_text(self.message, "error.message"))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunError":
        return cls(value["code"], value["message"])


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    scope_key: str
    status: RunStatus
    processed_count: int
    failed_count: int
    change_count: int
    attempt_count: int
    accepted_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: RunError | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", require_text(self.run_id, "run_id"))
        object.__setattr__(self, "scope_key", require_text(self.scope_key, "scope_key"))
        object.__setattr__(self, "status", RunStatus(self.status))
        for name in ("processed_count", "failed_count", "change_count", "attempt_count"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} 不能为负数")
        object.__setattr__(self, "accepted_at", ensure_utc(self.accepted_at, "accepted_at"))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at, "updated_at"))
        if self.started_at is not None:
            object.__setattr__(self, "started_at", ensure_utc(self.started_at, "started_at"))
        if self.finished_at is not None:
            object.__setattr__(self, "finished_at", ensure_utc(self.finished_at, "finished_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scope_key": self.scope_key,
            "status": self.status.value,
            "processed_count": self.processed_count,
            "failed_count": self.failed_count,
            "change_count": self.change_count,
            "attempt_count": self.attempt_count,
            "accepted_at": self.accepted_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error.to_dict() if self.error else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunSummary":
        return cls(
            run_id=value["run_id"],
            scope_key=value["scope_key"],
            status=RunStatus(value["status"]),
            processed_count=value["processed_count"],
            failed_count=value["failed_count"],
            change_count=value["change_count"],
            attempt_count=value["attempt_count"],
            accepted_at=parse_datetime(value["accepted_at"], "accepted_at"),
            updated_at=parse_datetime(value["updated_at"], "updated_at"),
            started_at=parse_datetime(value["started_at"], "started_at") if value.get("started_at") else None,
            finished_at=parse_datetime(value["finished_at"], "finished_at") if value.get("finished_at") else None,
            error=RunError.from_dict(value["error"]) if value.get("error") else None,
        )


@dataclass(frozen=True, slots=True)
class CancellationResult:
    run_id: str
    status: RunStatus
    accepted: bool


@dataclass(frozen=True, slots=True)
class WorkSummary:
    inspected: int
    progressed: int
    completed: int
    failed: int
    deferred: int


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    recovered: int


def new_run_id() -> str:
    return new_id()
