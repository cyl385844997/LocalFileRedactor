"""词库识别器：基于 Aho-Corasick 自动机做字面量多模式匹配。

为什么用 AC 而不是逐词``str.find``：
    逐词扫描的复杂度是 O(词条数 × 文本长度)。实测（见 .verify 探针记录）：
    1000 词条时 ``str.find`` 约 4.6ms、AC 约 7.6ms（AC 慢，因为构建有固定开销）；
    5000 词条时 ``str.find`` 约 96ms、AC 约 34ms（AC 反超近3 倍）。
    词库会随使用累积（甲方名单几百条很常见），因此选AC。

**实现中曾踩过的两个坑（务必勿改）**：

1. **收集 outs 与状态转移的顺序**
   正确顺序是「先沿 fail 回退并转移到 ``children[ch]``，**再**收集 outs」。
   若先收集 outs 再转移，读到的是**上一字符**走到的节点，命中区间会整体滞后一拍。

2. **失配到根时的continue**
   ``ifcur is None: cur = root; continue`` 会把本字符处本应有一次的
   outs 收集整段吃掉。正确写法是回落到 root 后**照常收集**
   （root.outs 恒为空，故行为安全，但代码路径不能吞掉收集动作）。

此外 fail 指针构建时，计算``child.fail`` 必须从 ``node.fail`` 出发
（不是 ``parent.fail``）——要算的是"child 的后缀能匹配到谁"，参照系是 node 自己。

**偏移量约定（关键）**：
    :meth:`LexiconRecognizer.find` 接收的是**原文**，内部归一化后扫描。
    归一化只做 1:1 替换（见 :func:`~redactor.core.lexicon.normalize_word`），
    长度与位置均不变，故归一化文本上的偏移量**可直接用于切原文**。
    但 ``Match.raw`` 必须从**原文**切取，否则报告与编号会带上归一化文本。
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from redactor.core.lexicon import LexiconEntry, normalize_word
from redactor.core.models import Match
from redactor.recognizers.base import Recognizer


class _TrieNode:
    """自动机节点。

    Attributes:
        children: 字符 -> 子节点。
        fail: 失配后回退到的节点；根节点的 fail 为 None。
        outs: 在该节点结束的词条 ``(词长, 占位符名)`` 列表。
    """

    __slots__ = ("children", "fail", "outs")

    def __init__(self) -> None:
        self.children: Dict[str, "_TrieNode"] = {}
        self.fail: "_TrieNode | None" = None
        self.outs: List[Tuple[int, str]] = []


def build_automaton(entries: Sequence[LexiconEntry]) -> _TrieNode:
    """由词库条目构建 Aho-Corasick 自动机。

    只收录 ``enabled`` 条目的归一化词条；空词条集合会得到一棵只有根的树，
    扫描任何文本都无命中（这是安全状态，不会误脱敏）。

    Args:
        entries: 词库条目。

    Returns:
        自动机根节点。
    """
    root = _TrieNode()
    for entry in entries:
        if not entry.enabled:
            continue
        for word in entry.normalized:
            if not word:
                continue
            cur = root
            for ch in word:
                nxt = cur.children.get(ch)
                if nxt is None:
                    nxt = _TrieNode()
                    cur.children[ch] = nxt
                cur = nxt
            # 同一节点可能对应多个条目（理论上已被去重挡掉，这里仍用列表保底）
            if not any(name == entry.name for _len, name in cur.outs):
                cur.outs.append((len(word), entry.name))

    _build_fail_links(root)
    return root


def _build_fail_links(root: _TrieNode) -> None:
    """广度优先构建 fail 指针，并把后缀节点的 outs 继承下来。

    修正点：计算 ``child.fail`` 时必须从 **node.fail** 出发，
    不是 ``parent.fail``。要算的是"child 的后缀能匹配到谁"，
    参照系是 node 自身所处的自动机状态。
    """
    queue: List[_TrieNode] = []
    for child in root.children.values():
        child.fail = root
        queue.append(child)

    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for ch, child in node.children.items():
            queue.append(child)
            # 关键：从 node.fail 出发（不是 parent.fail）
            fallback = node.fail
            while fallback is not None and ch not in fallback.children:
                fallback = fallback.fail
            target = fallback.children.get(ch) if fallback is not None else None
            child.fail = root if target is None else target
            # 后缀继承：child.fail 上的词条在child 处也应匹配
            if target is not None:
                child.outs = child.outs + target.outs


def scan(automaton: _TrieNode, normalized_text: str) -> List[Tuple[int, int, str]]:
    """在**已归一化**的文本上扫描，返回全部命中 ``(start, end, entry_name)``。

    顺序要点（见模块 docstring）：
        1. 沿 fail 链回退，找到能接住当前字符的节点
        2. **先转移**到 ``children[ch]``
        3. **再收集**该节点的 outs

    Args:
        automaton: 自动机根节点。
        normalized_text: 已归一化的文本。

    Returns:
        全部命中，``[start, end)`` 左闭右开，顺序不保证。
    """
    hits: List[Tuple[int, int, str]] = []
    cur: _TrieNode | None = automaton
    for index, ch in enumerate(normalized_text):
        while cur is not None and ch not in cur.children:
            cur = cur.fail
        if cur is None:
            cur = automaton
        nxt = cur.children.get(ch)
        if nxt is not None:
            cur = nxt
        # cur 现已指向"读完当前字符后"所处的节点，此时收集 outs 才是对的。
        # 失配到根且根无该字符时 cur 为 root，root.outs 为空，安全。
        for length, name in cur.outs:
            hits.append((index + 1 - length, index + 1, name))
    return hits


class MultiEntryLexiconRecognizer(Recognizer):
    """**所有词库条目共用一台自动机**，一次扫描产出全部命中。

    这是词库匹配的正确架构。逐词扫描的复杂度是
    O(总词数 × 文本长度)，且**每条目各扫一遍文本**——
    条目越多越慢。共用自动机后文本只扫一遍，复杂度
    O(总词数 + 文本长度)。

    实测（.verify/lexicon_ac_arch.txt）：20 条目 × 15 词时
    逐词 ~47ms、共用 AC ~6ms；在 100~6000 总词数区间AC 始终快 6~8 倍。

    **实现要点（务必勿改）**：
        本类一次扫描即产出**所有条目**的命中，靠 ``Match.rule_name`` 分组。
        若改成"每个条目造一个识别器、共享同一台自动机"，
        文本仍会被扫 N 遍，只省了构建，复杂度几乎不变——**等于没改**。

    每个节点记录 ``(词长, 条目名)``，所以扫描时天然知道命中属于哪个条目，
    不需要额外的映射表。
    """

    def __init__(self, entries: Sequence[LexiconEntry]):
        """初始化识别器并构建自动机（一次性）。

        Args:
            entries: 全部词库条目；``enabled: False`` 的条目不入自动机。
        """
        self._entries = [e for e in entries if e.enabled]
        self._automaton = build_automaton(self._entries)
        # 条目名 -> 占位符，供产出 Match 时使用
        self._placeholders: Dict[str, str] = {e.name: e.placeholder for e in self._entries}
        # Recognizer 基类要求 rule 属性；词库匹配不依赖 Rule.pattern，
        # 用首个条目占位即可（仅满足基类契约）。
        super().__init__(self._entries[0] if self._entries else None)  # type: ignore[arg-type]

    @property
    def entry_names(self) -> Tuple[str, ...]:
        """本识别器覆盖的条目名（全部启用条目）。"""
        return tuple(self._placeholders)

    def find(self, text: str) -> List[Match]:
        """一次扫描产出全部条目的命中。

        Args:
            text: **原文**（未归一化）。本方法内部完成归一化与偏移对齐。

        Returns:
            命中列表，``start``/``end`` 指向**原文**位置，``raw`` 为**原文**切片。
        """
        if not text or not self._entries:
            return []

        normalized = normalize_word(text)
        # 长度一致性是本设计的地基：归一化必须逐字符 1:1，
        # 否则扫描出的偏移无法直接用于切原文。
        assert len(normalized) == len(text), "归一化改变了文本长度，偏移将失效"

        matches: List[Match] = []
        for start, end, entry_name in scan(self._automaton, normalized):
            # raw 必须取自【原文】，不能用归一化文本
            matches.append(
                Match(
                    rule_name=entry_name,
                    placeholder=self._placeholders[entry_name],
                    start=start,
                    end=end,
                    raw=text[start:end],
                )
            )
        return matches


def build_lexicon_recognizer(entries: Sequence[LexiconEntry]) -> MultiEntryLexiconRecognizer:
    """工厂函数：由词库条目构造识别器（**所有条目共用一台自动机**）。

    与 ``build_recognizer`` 同构，便于按类型注入不同实现。

    Args:
        entries: 词库条目列表（通常来自 :func:`~redactor.core.lexicon.load_lexicon`）。

    Returns:
        一个 :class:`MultiEntryLexiconRecognizer`；无启用条目时返回的实例
        扫描任何文本都无命中（安全状态，不会误脱敏）。
    """
    return MultiEntryLexiconRecognizer(entries)


# ---------------------------------------------------------------------------
# 旧实现保留：单条目逐词扫描。
#
# 仅用于**测试对照**——它与共用自动机实现是两条独立代码路径，
# 互为交叉验证：两者的命中集合必须完全一致，否则说明其中一条有bug。
# 生产路径不要用它（见 :class:`MultiEntryLexiconRecognizer` 的复杂度说明）。
# ---------------------------------------------------------------------------
class LexiconRecognizer(Recognizer):
    """单条目逐词 ``str.find`` 扫描（**仅供测试对照**）。

    一个实例服务一条 :class:`LexiconEntry`。
    """

    def __init__(self, entry: LexiconEntry):
        """初始化识别器。

        Args:
            entry: 词库条目；其 ``name`` 决定占位符与报告分组。
        """
        # Recognizer 基类要求一个 Rule；此处用 entry 本身占位，
        # 因为词库匹配不依赖 Rule.pattern。base 契约只要有 .rule 属性即可。
        super().__init__(entry)  # type: ignore[arg-type]
        self._entry = entry
        self._placeholder = entry.placeholder

    @property
    def entry(self) -> LexiconEntry:
        """本识别器对应的词库条目。"""
        return self._entry

    def find(self, text: str) -> List[Match]:
        """查找本条目全部词条的命中。

        Args:
            text: **原文**（未归一化）。本方法内部完成归一化与偏移对齐。

        Returns:
            命中列表，``start``/``end`` 指向**原文**位置，``raw`` 为**原文**切片。
        """
        if not text or not self._entry.enabled:
            return []
        words = self._entry.normalized
        if not words:
            return []

        normalized = normalize_word(text)
        assert len(normalized) == len(text), "归一化改变了文本长度，偏移将失效"

        matches: List[Match] = []
        for word in words:
            if not word:
                continue
            start = 0
            while True:
                found = normalized.find(word, start)
                if found < 0:
                    break
                end = found + len(word)
                matches.append(
                    Match(
                        rule_name=self._entry.name,
                        placeholder=self._placeholder,
                        start=found,
                        end=end,
                        raw=text[found:end],
                    )
                )
                # +1 以支持重叠命中（如 '甲公司' 与 '甲公司分公司' 同时命中）
                start = found + 1
        return matches
