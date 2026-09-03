"""关系型适配器私有数据库方言。"""

from .mysql import MySQLDialect
from .sqlite import SQLiteDialect

__all__ = ["MySQLDialect", "SQLiteDialect"]
