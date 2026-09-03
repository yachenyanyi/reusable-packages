"""MySQL 的私有事务与领取策略。"""

from __future__ import annotations


class MySQLDialect:
    name = "mysql"

    def begin_write(self, connection) -> None:
        connection.begin()

    def lock_rows(self, statement):
        # MySQL 8+ + InnoDB 是关系型适配器的目标运行假设。
        return statement.with_for_update(skip_locked=True)

    def lock_state(self, statement):
        # 分配器状态只有一行，竞争时必须等待，不能因为 SKIP LOCKED
        # 跳过公平游标而把一次分配误判为空。
        return statement.with_for_update()

    def lock_document(self, statement):
        # 历史提交不能跳过已存在的 Document，否则两个新版本可能同时
        # 根据旧快照计算 revision/sequence。
        return statement.with_for_update()
