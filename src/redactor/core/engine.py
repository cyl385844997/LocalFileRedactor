"""核心引擎：纯文本处理，不接触任何文件格式。

这是整个软件的中枢，也是唯一的"识别 + 替换"实现处。
handlers 层只负责把文件内容拆成文本块交给它，再把结果写回。

引擎本身完全离线、无 I/O、无第三方依赖。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from redactor.core.models import HitStat, Match, Rule
from redactor.recognizers.base import Recognizer
from redactor.recognizers.regex_recognizer import build_recognizer
from redactor.redactors.replacer import PlaceholderReplacer, resolve_overlaps


class RedactionEngine:
    """脱敏引擎。"""

    def __init__(self, rules: Sequence[Rule], *, force_numbered: bool = False):
        """初始化引擎。

        Args:
            rules: 已加载并校验过的规则列表（顺序即优先级）。
            force_numbered: True 时所有规则强制使用编号占位符。
        """
        if not rules:
            raise ValueError("规则列表为空，无法启动脱敏引擎")
        self._rules: List[Rule] = list(rules)
        self._recognizers: List[Recognizer] = [build_recognizer(rule) for rule in self._rules]
        # 编号分配器必须是"每次任务一个"，否则编号会跨文件延续
        self._replacer = PlaceholderReplacer(self._rules, force_numbered=force_numbered)
        self._priority: Dict[str, int] = {rule.name: i for i, rule in enumerate(self._rules)}

    @property
    def rules(self) -> List[Rule]:
        """当前生效的规则列表。"""
        return list(self._rules)

    def scan(self, text: str) -> List[Match]:
        """只识别不替换，用于 --dry-run 与自检。

        Args:
            text: 待检文本。

        Returns:
            消解重叠后的命中列表（按 start 升序）。
        """
        found: List[Match] = []
        for recognizer in self._recognizers:
            found.extend(recognizer.find(text))
        return resolve_overlaps(found, self._priority)

    def redact(self, text: str) -> Tuple[str, List[Match]]:
        """识别并替换。

        Args:
            text: 原始文本。

        Returns:
            ``(脱敏后文本, 实际生效的命中列表)``。
        """
        found: List[Match] = []
        for recognizer in self._recognizers:
            found.extend(recognizer.find(text))
        return self._replacer.apply(text, found)

    def render_placeholder(self, match: Match) -> str:
        """取某处命中的占位符文本。

        供handlers 层使用：当handler 需要自行控制替换落点时（例如 docx 要把
        占位符写进特定的run），仍要复用同一套编号分配逻辑，
        否则编号模式下会出现编号不一致。

        Args:
            match: 命中对象（通常来自 :meth:`scan`）。

        Returns:
            ``[手机号]`` 或编号模式下的 ``[手机号#1]``。
        """
        return self._replacer.placeholder_for(match)

    @staticmethod
    def aggregate(matches: Iterable[Match], locations: Optional[Dict[Tuple[str, int], str]] = None) -> List[HitStat]:
        """把命中聚合成"规则名 + 次数 + 位置"的统计，**不含任何原文**。

        Args:
            matches: 生效的命中列表。
            locations: ``(规则名, start) -> 位置描述`` 映射；缺省则不记录位置。

        Returns:
            按规则名排序的 :class:`HitStat` 列表。
        """
        buckets: Dict[str, List[str]] = {}
        for match in matches:
            bucket = buckets.setdefault(match.rule_name, [])
            if locations is not None:
                label = locations.get((match.rule_name, match.start))
                if label and label not in bucket:
                    bucket.append(label)
            else:
                # 没有位置信息时，用"共 N 处"的占位说明，报告中不留原文
                bucket.append("")

        stats: List[HitStat] = []
        for rule_name, locs in buckets.items():
            if locations is None:
                stats.append(HitStat(rule_name=rule_name, count=len(locs), locations=()))
            else:
                # 去重后按出现顺序保留
                unique = tuple(dict.fromkeys(x for x in locs if x))
                stats.append(HitStat(rule_name=rule_name, count=len(locs), locations=unique))
        stats.sort(key=lambda s: s.rule_name)
        return stats