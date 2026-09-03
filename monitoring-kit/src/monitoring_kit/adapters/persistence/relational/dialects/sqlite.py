"""SQLite 的私有事务与领取策略。"""

from __future__ import annotations


class SQLiteDialect:
    name = "sqlite"

    def begin_write(self, connection) -> None:
        # 把领取和历史提交放进 IMMEDIATE 事务，先获得写锁，避免两个进程
        # 都读到同一份可领取状态后再竞争更新。
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    def lock_rows(self, statement):
        return statement

    def lock_state(self, statement):
        return statement

    def lock_document(self, statement):
        return statement
