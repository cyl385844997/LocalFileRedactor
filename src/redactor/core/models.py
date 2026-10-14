"""核心数据模型。

本模块只定义数据结构，不做任何 I/O、不依赖 GUI / python-docx / PyMuPDF，
确保 core 层可独立成库、可单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# re 标志名 -> re 标志值 的白名单映射。
# YAML 中只允许写这些名字，禁止直接写整数，避免歧义与安全问题。
FLAG_WHITELIST: Dict[str, int] = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "VERBOSE": re.VERBOSE,
    "UNICODE": re.UNICODE,
}


class RuleError(Exception):
    """规则相关错误的基类。

    ``message`` 已包含文件名与行号，可直接呈现给用户。
    """

    def __init__(self, message: str, *, path: Optional[str] = None, line: Optional[int] = None):
        self.path = path
        self.line = line
        super().__init__(message)


@dataclass(frozen=True)
class Rule:
    """一条脱敏规则。

    Attributes:
        name: 规则名，同时决定默认占位符文本（``[name]``）。
        pattern: 正则表达式源码。
        placeholder: 占位符文本，默认 ``"[<name>]"``。
        numbered: 是否启用编号（``[手机号#1]``）。默认 False，隐私性更好。
        enabled: 是否参与匹配。默认 True。
        validator: 校验函数名（``id_card`` / ``luhn``），用于降低误报。None 表示不校验。
        flags: 正则标志名列表，取值来自 :data:`FLAG_WHITELIST`。
        description: 规则说明，仅用于文档与报告。
        line: 该规则在 YAML 中的行号（从 1 开始），用于报错定位。
    """

    name: str
    pattern: str
    placeholder: str = ""
    numbered: bool = False
    enabled: bool = True
    validator: Optional[str] = None
    flags: List[str] = field(default_factory=list)
    description: str = ""
    line: int = 0

    @property
    def flag_value(self) -> int:
        """把白名单标志名列表转换为 re 标志位值。"""
        value = 0
        for name in self.flags:
            value |= FLAG_WHITELIST[name]
        return value


@dataclass(frozen=True)
class Match:
    """一处命中。

    注意：本对象含 ``raw``（原文），属于敏感数据，**禁止写入报告或日志**。
    报告中只能使用 ``rule_name`` / ``placeholder`` / 位置信息。
    """

    rule_name: str
    placeholder: str
    start: int
    end: int
    raw: str


@dataclass(frozen=True)
class HitStat:
    """按规则聚合的命中统计，**不含任何原文**，可安全写入报告。

    Attributes:
        rule_name: 规则名。
        count: 命中次数。
        locations: 位置描述列表（如 ``第 3 段`` / ``第 2 页``），由各 handler 生成。
    """

    rule_name: str
    count: int
    locations: Tuple[str, ...] = ()

    @property
    def placeholder(self) -> str:
        """占位符文本，由规则名推导。"""
        return f"[{self.rule_name}]"


@dataclass
class FileResult:
    """单个文件的处理结果。

    Attributes:
        source: 源文件路径。
        output: 输出文件路径；未生成输出时为 None。
        status: ``ok`` / ``skipped`` / ``failed``。
        hits: 命中统计（零敏感内容）。
        reason: 失败或跳过的原因码（如 ``encrypted_pdf``）。
        message: 面向用户的原因说明。
        elapsed_ms: 处理耗时（毫秒）。
    """

    source: str
    output: Optional[str] = None
    status: str = "ok"
    hits: List[HitStat] = field(default_factory=list)
    reason: Optional[str] = None
    message: Optional[str] = None
    elapsed_ms: int = 0

    @property
    def total_hits(self) -> int:
        """所有规则命中次数之和。"""
        return sum(h.count for h in self.hits)