"""关系型持久化适配器对外可处理的稳定错误。"""

from __future__ import annotations


class PersistenceError(Exception):
    """数据库不可用或无法满足持久化契约时抛出的适配器错误。"""

    code = "PERSISTENCE_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PersistenceConfigurationError(PersistenceError):
    code = "PERSISTENCE_CONFIGURATION_ERROR"


class PersistenceUnavailableError(PersistenceError):
    code = "PERSISTENCE_UNAVAILABLE"


class PersistenceInvariantError(PersistenceError):
    code = "PERSISTENCE_INVARIANT_VIOLATION"
