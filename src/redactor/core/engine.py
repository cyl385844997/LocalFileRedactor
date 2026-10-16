"""核心引擎：纯文本处理，不接触任何文件格式。

这是整个软件的中枢，也是唯一的"识别 + 替换"实现处。
handlers 层只负责把文件内容拆成文本块交给它，再把结果写回。

引擎本身完全离线、无 I/O、无第三方依赖。
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from redactor.core.lexicon import LexiconEntry
from redactor.core.models import HitStat, Match, Rule
from redactor.recognizers.base import Recognizer
from redactor.recognizers.lexicon_recognizer import build_lexicon_recognizer
from redactor.recognizers.regex_recognizer import build_recognizer
from redactor.redactors.replacer import PlaceholderReplacer, resolve_overlaps


class RedactionEngine:
    """脱敏引擎。"""

    def __init__(
        self,
        rules: Sequence[Rule],
        *,
        force_numbered: bool = False,
        lexicon_entries: Optional[Sequence[LexiconEntry]] = None,
    ):
        """初始化引擎。

        Args:
            rules: 已加载并校验过的规则列表（顺序即优先级）。
            force_numbered: True 时所有规则强制使用编号占位符。
            lexicon_entries: 自定义词库条目（来自 :mod:`redactor.core.lexicon`）。

                **首版仅存储，尚未参与匹配** —— 识别器将在后续模块接入
                （``_recognizers`` 的追加逻辑）。当前传入非空词库不会改变
                ``scan`` / ``redact`` 的行为，这���首版的已知限制。
        """
        if not rules:
            raise ValueError("规则列表为空，无法启动脱敏引擎")
        self._rules: List[Rule] = list(rules)
        self._lexicon_entries: List[LexiconEntry] = list(lexicon_entries or [])
        self._recognizers: List[Recognizer] = [build_recognizer(rule) for rule in self._rules]
        # 词库识别器**只造一个**，内部所有条目共用一台自动机、一次扫描产出全部命中。
        #
        # 切勿改成"每条目一个识别器"：那样文本仍会被扫 N 遍，
        # 只省了自动机构建，复杂度几乎不变，等于没改。
        #
        # 位置必须在【末尾】：priority 由规则在列表中的下标决定
        # （见 self._priority），末尾即最低优先级，从而保证词库在同起点
        # 冲突时不会压过内置正则规则。
        if any(entry.enabled for entry in self._lexicon_entries):
            self._recognizers.append(
                build_lexicon_recognizer(self._lexicon_entries)
            )
        # 编号分配器必须是"每次任务一个"，否则编号会跨文件延续
        self._replacer = PlaceholderReplacer(self._rules, force_numbered=force_numbered)
        # 词库条目名不进入 _priority：它们不在 PlaceholderReplacer 的规则表里，
        # 且 resolve_overlaps 查不到时返回默认值 9999（永远垫底），
        # 这正是"词库优先级低于内置正则"所要求的行为。
        self._priority: Dict[str, int] = {rule.name: i for i, rule in enumerate(self._rules)}

    @property
    def rules(self) -> List[Rule]:
        """当前生效的规则列表。"""
        return list(self._rules)

    @property
    def recognizers(self) -> List[Recognizer]:
        """当前生效的识别器列表（只读副本）。

        供 handler 逐载体调用 :meth:`Recognizer.find_in_span`。
        返回副本而非内部列表，避免外部改动引擎内部状态。
        """
        return list(self._recognizers)

    @property
    def lexicon_entries(self) -> List[LexiconEntry]:
        """当前已加载的词库条目（首版尚未参与匹配，见 :meth:`__init__`）。"""
        return list(self._lexicon_entries)

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
    def aggregate(
        matches: Iterable[Match],
        locations: Optional[Dict[Tuple, str]] = None,
        *,
        key_of: Optional[Callable[[Match], Tuple]] = None,
        strict: bool = True,
    ) -> List[HitStat]:
        """把命中聚合成"规则名 + 次数 + 位置"的统计，**不含任何原文**。

        ``count`` 与 ``locations`` 是**两套结构**，靠"键推导必须一致"这一
        隐性约定联结。约定一旦破坏就会**静默少报**，故此处有两道防线：

        1. **``count`` 取自命中本身，不取自标签个数**（见 Note 1）。
        2. **取不到标签即抛错**（``strict=True``，见 Note 2）。

        Args:
            matches: 生效的命中列表。
            locations: ``key -> 位置描述`` 映射；缺省则不记录位置。
            key_of: 由 :class:`Match` 生成 ``locations`` 键的函数。
                缺省用 ``(rule_name, start)``—— 这对**单一连续文本**成立，
                但对**分页载体**（如 PDF）会撞键：
                每页的 ``start`` 都是页内偏移，两页同布局就会产生相同的
                ``(rule_name, start)``，后者覆盖前者，导致报告少报
                （合成 3 页同布局样本复现：只报 1 处）。

                故 PDF handler 传入 :func:`redactor.handlers.pdf_handler.location_key`
                （命名函数，返回三元组 ``(rule_name, page_index, start)``）。
            strict: ``locations`` 非空时，若有命中取不到标签是否抛错。
                默认 ``True``。设为 ``False`` 只在确有"部分命中无需位置"
                的场景下使用，并应自行承担少报风险。

        Returns:
            按规则名排序的 :class:`HitStat` 列表。

        Raises:
            ValueError: ``strict=True`` 且 ``locations`` 非空，但有命中
                取不到标签——这几乎总是 ``key_of`` 与 ``locations`` 的键
                不一致。此时**必须报错而不是静默丢命中**：曾实测「传三元键
                ``locations`` 但忘传 ``key_of``」，24 处命中的报告直接变成
                0 处，全程无异常、无告警。

        Note:
            1. **``count`` 是命中数，不是标签数**。
               早期实现把 ``count`` 写成去重后的标签个数，于是"同一位置标签
               对应多处命中"时会少报。合成样本复现：一行内两个手机号，
               标签都是 ``第 1 行``，``count`` 被压成 1（实际删了 2 处）。
               位置标签只负责"告诉人去哪儿看"，不该反过来决定计数。

            2. **键用 tuple 而非打包整数**（如 ``page * 10**7 + start``）：
               打包整数依赖"单页文本不超过 10^7 字符"这一隐含假设，
               一旦遇到异常大的页面就会静默撞键、报告再次少报且无任何报错。
               tuple 键无此假设，多一个元素的成本可忽略。
        """
        make_key = key_of if key_of is not None else (
            lambda m: (m.rule_name, m.start)
        )
        ordered = list(matches)

        # count 的唯一来源：命中本身。locations 只影响"位置"这一展示字段。
        counts: Dict[str, int] = {}
        buckets: Dict[str, List[str]] = {}
        unlabeled: List[Tuple[str, Tuple]] = []

        for match in ordered:
            counts[match.rule_name] = counts.get(match.rule_name, 0) + 1
            if locations is None:
                continue
            key = make_key(match)
            label = locations.get(key)
            if not label:
                unlabeled.append((match.rule_name, key))
                continue
            bucket = buckets.setdefault(match.rule_name, [])
            if label not in bucket:      # 去重后按出现顺序保留
                bucket.append(label)

        if unlabeled and strict:
            shown = "、".join(f"{name}{key}" for name, key in unlabeled[:5])
            more = f" 等 {len(unlabeled)} 处" if len(unlabeled) > 5 else ""
            raise ValueError(
                f"aggregate 收到 locations，但有 {len(unlabeled)} 处命中"
                f"取不到位置标签（{shown}{more}）。"
                f"这几乎总是 key_of 与 locations 的键不一致——两者必须由"
                f"同一个键函数生成。若放任不管，这些命中会被静默丢弃、"
                f"报告少报（曾实测 24 处变成 0 处，无任何报错）。"
                f"请检查调用方：PDF 必须传 key_of=location_key（三元键含页码）。"
            )

        stats: List[HitStat] = []
        for rule_name, count in counts.items():
            stats.append(
                HitStat(
                    rule_name=rule_name,
                    count=count,
                    locations=tuple(buckets.get(rule_name, ())),
                )
            )
        stats.sort(key=lambda s: s.rule_name)
        return stats