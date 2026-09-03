"""SQLite/MySQL 关系型持久化适配器。"""

from .bundle import PersistenceBundle, PersistenceConfig, open_persistence
from .errors import (
    PersistenceConfigurationError,
    PersistenceError,
    PersistenceInvariantError,
    PersistenceUnavailableError,
)

__all__ = [
    "PersistenceBundle",
    "PersistenceConfig",
    "PersistenceConfigurationError",
    "PersistenceError",
    "PersistenceInvariantError",
    "PersistenceUnavailableError",
    "open_persistence",
]
