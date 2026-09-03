"""假统一采集 API 的可脚本化场景模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ResponsePlan:
    """一次 HTTP 响应计划；body 为 None 表示使用端点默认响应。"""

    status_code: int = 200
    body: Mapping[str, Any] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status_code < 100 or self.status_code > 599:
            raise ValueError("status_code 必须是有效 HTTP 状态码")
        if self.body is not None and not isinstance(self.body, Mapping):
            raise ValueError("ResponsePlan.body 必须是对象")
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)) if self.body is not None else None)
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class ApiScenario:
    """描述一个任务型统一采集 API 在测试中的可观察行为。"""

    records: tuple[Mapping[str, Any], ...] = ()
    page_size: int = 100
    status_timeline: tuple[str, ...] = ("completed",)
    submission_script: tuple[ResponsePlan, ...] = ()
    status_script: tuple[ResponsePlan, ...] = ()
    result_script: tuple[ResponsePlan, ...] = ()
    cancellation_script: tuple[ResponsePlan, ...] = ()
    auth_token: str | None = None
    job_id_prefix: str = "fake-job"
    result_schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if self.page_size < 1:
            raise ValueError("page_size 必须大于 0")
        if not self.status_timeline:
            raise ValueError("status_timeline 不能为空")
        valid_states = {"queued", "running", "completed", "completed_with_errors", "failed", "cancelled"}
        if any(state not in valid_states for state in self.status_timeline):
            raise ValueError("status_timeline 含有未知任务状态")
        for name in ("records", "submission_script", "status_script", "result_script", "cancellation_script"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "records", tuple(MappingProxyType(dict(item)) for item in self.records))
        if self.auth_token is not None and not isinstance(self.auth_token, str):
            raise ValueError("auth_token 必须是字符串或 None")
        if not self.job_id_prefix.strip():
            raise ValueError("job_id_prefix 不能为空")


def json_time(value: Any, default: datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default.isoformat().replace("+00:00", "Z")
