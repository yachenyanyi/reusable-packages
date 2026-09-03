"""测试专用的假统一采集 API。"""

from .scenario import ApiScenario, ResponsePlan
from .server import FakeUnifiedApi

__all__ = ["ApiScenario", "FakeUnifiedApi", "ResponsePlan"]
