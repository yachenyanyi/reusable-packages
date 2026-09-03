"""运行装配、显式扩展注册和唤醒支持。"""

from .clock import SystemClock
from .registry import ExtensionRegistry

__all__ = ["ExtensionRegistry", "SystemClock"]
