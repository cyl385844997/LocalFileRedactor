"""占位符替换器：把 Match 列表应用到文本，生成脱敏后的文本。

两个关键设计：
1. **冲突消解**：多条规则可能命中同一段文本（如 19 位数字既是"银行卡号"格式
   也可能被别的规则覆盖）。按"区间更长者优先；长度相同则 YAML 中靠前的规则优先"
   的规则去重叠，保证替换结果确定、可复现。
2. **编号分配**：numbered 规则下，同一原文复用同一编号（手机号#1 出现两次仍是 #1），
   这也是唯一需要在内存里保留原文映射的地方——映射表随对象销毁，不落盘。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from redactor.core.models import Match, Rule


def resolve_overlaps(matches: List[Match], priority: Dict[str, int]) -> List[Match]:
    """消解重叠命中，返回按 start 升序排列的、互不重叠的命中列表。

    Args:
        matches: 原始命中列表。
        priority: 规则名 -> 优先级序号（越小越优先），来自 YAML 中的规则顺序。

    Returns:
        消解后的命中列表。
    """
    if not matches:
        return []

    # 排序规则：起点升序 -> 区间更长优先 -> 规则优先级更高优先
    ordered = sorted(
        matches,
        key=lambda m: (m.start, -(m.end - m.start), priority.get(m.rule_name, 9999)),
    )

    kept: List[Match] = []
    last_end = -1
    for match in ordered:
        # 与已保留的命中有交集则丢弃（保证替换区间不重叠，避免二次替换错位）
        if match.start < last_end:
            continue
        kept.append(match)
        last_end = match.end
    return kept


class PlaceholderReplacer:
    """把命中替换为占位符。"""

    def __init__(self, rules: List[Rule], *, force_numbered: bool = False):
        """初始化替换器。

        Args:
            rules: 规则列表，用于判断哪些规则启用编号。
            force_numbered: True 时忽略规则自身的 numbered 设置，全部启用编号
                （对应 CLI 的 ``--numbered`` 开关）。
        """
        self._rules = {r.name: r for r in rules}
        self._force_numbered = force_numbered
        # 编号映射：(规则名, 原文) -> 编号。仅存在于内存，绝不写盘。
        self._counters: Dict[Tuple[str, str], int] = {}
        self._next_index: Dict[str, int] = {}

    def is_numbered(self, rule_name: str) -> bool:
        """判断某条规则是否使用编号占位符。"""
        if self._force_numbered:
            return True
        rule = self._rules.get(rule_name)
        return bool(rule and rule.numbered)

    def placeholder_for(self, match: Match) -> str:
        """生成某处命中的占位符文本。

        Args:
            match: 命中对象。

        Returns:
            统一占位符（如 ``[手机号]``）或编号占位符（如 ``[手机号#1]``）。
        """
        if not self.is_numbered(match.rule_name):
            return match.placeholder
        key = (match.rule_name, match.raw)
        if key not in self._counters:
            index = self._next_index.get(match.rule_name, 0) + 1
            self._counters[key] = index
            self._next_index[match.rule_name] = index
        return f"[{match.rule_name}#{self._counters[key]}]"

    def apply(
        self,
        text: str,
        matches: List[Match],
        *,
        priority: Optional[Dict[str, int]] = None,
    ) -> Tuple[str, List[Match]]:
        """把命中应用到文本。

        Args:
            text: 原始文本。
            matches: 命中列表（默认会先做一次重叠消解）。
            priority: 重叠消解用的优先级表。**传``None`` 时用内置规则的
                下标**（历史行为，词库与范围条目名因此拿不到优先级）；
                调用方若已用完整优先级表消解过（如
                :meth:`RedactionEngine.redact`），应把同一张表传进来，
                否则会**二次消解**并把词库/范围的登记丢掉（已实测：
                词库"甲方单位"被范围"有限公司"顶掉）。

        Returns:
            ``(脱敏后文本, 实际生效的命中列表)``。
            第二个返回值供调用方生成报告中的位置信息。
        """
        table = (priority if priority is not None
                 else {name: i for i, name in enumerate(self._rules)})
        kept = resolve_overlaps(matches, table)
        if not kept:
            return text, []

        # 从后往前替换，避免前面的替换影响后面的偏移量
        pieces: List[str] = []
        cursor = 0
        for match in kept:
            pieces.append(text[cursor : match.start])
            pieces.append(self.placeholder_for(match))
            cursor = match.end
        pieces.append(text[cursor:])
        return "".join(pieces), kept