"""采集核心：隐藏一次 Run 的可靠推进。"""

from .engine import CollectionEngine, RetryPolicy

__all__ = ["CollectionEngine", "RetryPolicy"]
