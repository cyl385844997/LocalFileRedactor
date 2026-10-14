"""脱敏核心包：数据模型、规则加载、纯文本处理引擎。

本包不依赖任何 GUI 或第三方文档库，可独立成库使用。
"""

from redactor.core.models import (
    FLAG_WHITELIST,
    FileResult,
    HitStat,
    Match,
    Rule,
    RuleError,
)

__all__ = [
    "FLAG_WHITELIST",
    "FileResult",
    "HitStat",
    "Match",
    "Rule",
    "RuleError",
]