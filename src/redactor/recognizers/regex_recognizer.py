"""正则识别器：core 的默认实现。

职责：按规则正则匹配文本，必要时用校验函数过滤误报。
不做任何替换——替换由 redactors.replacer 负责。
"""

from __future__ import annotations

import re
from typing import List, Optional

from redactor.core.models import Match
from redactor.recognizers.base import Recognizer
from redactor.recognizers.validators import VALIDATORS


class RegexRecognizer(Recognizer):
    """基于正则的识别器。"""

    def __init__(self, rule, validator_func=None):
        """初始化识别器。

        Args:
            rule: 规则对象（其正则已在加载阶段编译验证过）。
            validator_func: 可选的校验函数；为 None 时按规则名从注册表取，
                取不到则视为不校验（加载阶段已保证 validator 名合法）。
        """
        super().__init__(rule)
        self._regex = re.compile(rule.pattern, rule.flag_value)
        if validator_func is None and rule.validator:
            validator_func = VALIDATORS.get(rule.validator)
        self._validator = validator_func

    def find(self, text: str) -> List[Match]:
        """查找全部命中，并按需执行校验过滤。"""
        if not text:
            return []

        matches: List[Match] = []
        for found in self._regex.finditer(text):
            raw = found.group(0)
            # 校验不通过 -> 视为误报，丢弃
            if self._validator is not None and not self._validator(raw):
                continue
            matches.append(
                Match(
                    rule_name=self.rule.name,
                    placeholder=self.rule.placeholder,
                    start=found.start(),
                    end=found.end(),
                    raw=raw,
                )
            )
        return matches


def build_recognizer(rule) -> RegexRecognizer:
    """按规则构造识别器的工厂函数（便于后续按 validator 名注入不同实现）。"""
    return RegexRecognizer(rule, VALIDATORS.get(rule.validator or ""))