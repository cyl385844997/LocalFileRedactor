"""识别器抽象接口。

设计目的：core 层只依赖"把文本变成 Match 列表"这一契约，
将来若要接入词典匹配、NER 等，只需新增 Recognizer 实现，不改 engine。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from redactor.core.models import Match, Rule


class Recognizer(ABC):
    """识别器基类。"""

    def __init__(self, rule: Rule):
        self.rule = rule

    @abstractmethod
    def find(self, text: str) -> List[Match]:
        """在文本中查找该规则的全部命中。

        Args:
            text: 待检文本。

        Returns:
            Match 列表，``start``/``end`` 为 ``[start, end)`` 左闭右开区间。
        """
        raise NotImplementedError