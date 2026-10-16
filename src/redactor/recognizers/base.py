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

    def find_in_span(self, span_text: str, offset: int = 0) -> List[Match]:
        """在**一个文本片段**内查找命中，返回**全局坐标**的 Match。

        这是为 PDF 等"无段落概念"的载体准备的接口：那边拿到的是
        PyMuPDF 的 span（实测可能只有 1 个字），不存在跨 span 的连续文本，
        因此只能逐 span 匹配，再用 ``offset`` 换算回全局坐标。

        **默认实现就是 :meth:`find`**：正则与词库识别器对纯文本的处理
        与片段边界无关，只是基准点不同，把片段内偏移加上 ``offset`` 即可。
        故无需各子类重复实现——真要覆盖时再override。

        与 :meth:`find` 的区别**仅在于返回坐标的基准**：
            find(text)       -> start/end 是 text 内的偏移
            find_in_span(s,o)-> start/end 是 (o + 片段内偏移)

        Args:
            span_text: 片段文本。
            offset: 该片段在全局字符串中的起始偏移（默认 0，等同 find）。

        Returns:
            Match 列表，``start``/``end`` 已换算到全局坐标。
        """
        if not span_text:
            return []
        local = self.find(span_text)
        if offset == 0:
            return local
        return [
            Match(
                rule_name=m.rule_name,
                placeholder=m.placeholder,
                start=m.start + offset,
                end=m.end + offset,
                raw=m.raw,
            )
            for m in local
        ]