"""不依赖 pytest fixture 的测试常量。"""

from datetime import datetime, timezone

TEST_TIME = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
