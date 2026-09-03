"""monitoring-kit 对调用方承诺的错误类型。"""

from __future__ import annotations

from dataclasses import dataclass


class MonitoringError(Exception):
    """所有可由宿主处理的 monitoring-kit 错误。"""

    code = "MONITORING_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class InvalidRequestError(MonitoringError):
    code = "INVALID_REQUEST"


class UnsupportedCollectionTypeError(MonitoringError):
    code = "UNSUPPORTED_COLLECTION_TYPE"


class InvalidCollectionSpecError(MonitoringError):
    code = "INVALID_COLLECTION_SPEC"


class ScopeMismatchError(MonitoringError):
    code = "SCOPE_MISMATCH"


class ExecutionLimitRejectedError(MonitoringError):
    code = "EXECUTION_LIMIT_REJECTED"


class RunNotFoundError(MonitoringError):
    code = "RUN_NOT_FOUND"


class IdempotencyConflictError(MonitoringError):
    code = "IDEMPOTENCY_CONFLICT"


class ConfigurationError(MonitoringError):
    code = "INVALID_CONFIGURATION"


class HistoryInvariantError(MonitoringError):
    code = "HISTORY_INVARIANT_VIOLATION"


class UpstreamProtocolError(MonitoringError):
    code = "UPSTREAM_PROTOCOL_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """供运行摘要使用的稳定错误信息。"""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}
