"""关系型适配器的私有事务生命周期。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def write_transaction(engine: Any, dialect: Any) -> Iterator[Any]:
    connection = engine.connect()
    try:
        dialect.begin_write(connection)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
