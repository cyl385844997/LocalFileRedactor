"""P7 范围脱敏识别器：**后缀锚点 + 反向逐字符扫描**。

为什么不用正则（完整实测见 :mod:`redactor.core.scopes` 模块 docstring）
--------------------------------------------------------------------------
关键事实（本项目的实测数据，不是推断）：

    朴素贪婪正则 ``[一-龥]{2,15}(公司|大学|项目)``
        正例 12/12   误伤 8/22   吞内置规则 0 处
    加负向前瞻 ``(?<!去|到|在)``
        正例 12/12   误伤 7/22   ← **与朴素版同级，负向前瞻无效**
    轻量字符类排除
        正例 12/12   误伤 13/22  ← **比朴素版更差**   吞内置规则 **3 处**
    **本模块（后缀锚点 + 反向扫描）**
        正例 12/12   误伤 2/22   吞内置规则 **0 处**

**负向前瞻为什么无效**：``[一-龥]{2,15}`` 是贪婪的，会先把"去"吞进实体名，
负向前瞻检查的位置变成"他"的前一个字，压根看不到"去"。

**"排除字符"为什么更差**：排掉"在"之后，引擎会跳过"在"继续往后吞，
区间反而变长，误伤从 8涨到 13。

**正则为什么必须否决（致命）**：``[^...A-Za-z0-9]{1,14}`` 实测把
``电话13812345678，公司`` 整段吞掉（span 6..22）。按
:func:`redactor.redactors.replacer.resolve_overlaps` 的"更长者优先"，
范围规则胜出 → **手机号被吃掉，且以残缺形式泄露**。脱敏工具这样等于没脱敏。

本模块的解法
------------
**不让正则参与实体名匹配**，只让它（或直接字符串查找）定位**后缀**，
然后从后缀位置**向前逐字符扫描**，遇到"停止字符"即停：

    停止字符 = 数字 / 字母 / 标点 / 空白 / 高频功能字

数字天然是停止字符 → 命中区间**绝不可能包含数字**，
Q4（吞掉内置手机号规则）从架构上不可能发生。

**根本原因**（语言学歧义，非工程问题）：
``重庆大学`` 的"重庆"是地名，``去大学`` 的"去"是动词，
两者都是"2 个汉字 + 后缀"，字形完全无法区分。
剩余 2/22 误伤（``这家公司实力不错`` / ``这个项目很难做``）属**真歧义，
无法在正则层面消除**，故提供"保守 / 平衡 / 激进"三档让用户取舍。

偏移量约定
----------
本识别器**不做任何文本变换**（无归一化），扫描直接在原文上进行，
因此产出的 ``start`` / ``end`` 直接就是原文偏移，可直接用于切原文与替换。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from redactor.core.models import Match, Rule
from redactor.core.scopes import (
    LEAD_STOP_CHARS,
    MAX_NAME_LEN,
    MIN_NAME_LEN,
    ScopeEntry,
    expand_suffixes,
)
from redactor.recognizers.base import Recognizer


# ================================================================ 字符集
#: 物理上不可能出现在中文实体名里的字符 —— 数字 / 字母 / 标点 / 空白。
#: **这是 Q4 的防线**：反向扫描遇之即停，命中区间绝不含数字。
STOP_CHARS: frozenset = frozenset(
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    " \t\r\n\v\f"
    "，。！？；：、（）()《》【】「」『』〔〕"
    "\"'“”‘’"
    "—…·.,!?;:\\/-_=+*&^%$#@~`|"
    "　"
)

#: 高频功能字 —— 出现在实体名里多半是动词/助词而非字号。
#: 用作"提前停止"信号，把 ``他今天去公司`` 截断成 ``公司`` 前1 字（不达标）→ 不命中。
#:
#: 注意：**只列高频功能字，不列代词/指示词**。实测把"这那每某"也列进来
#: 会把"重庆水务集团""西南大学"误杀（首字回退到别的路径）。
#: 指示字另由 ``LEAD_STOP_CHARS`` 在激进档单独管（只看首字）。
FUNC_CHARS: frozenset = frozenset(
    # 助词 / 语气词
    "了着过是很也就都还要会能可把被让给对"
    # 介词
    "在到去从关于对于但而且或"
    # ★ 常用动词 / 系词 —— 实测缺这组会让"甲方为重庆某某公司"里的
    #   "为"被吞进实体名，产出 (0,15) 而不是 (3,15)，
    #   进而在 resolve_overlaps 的"更长者优先"下压过词库同区间命中
    #   （违反裁定 6 的"词库 > 范围"，已实测）。
    "为是由致经与跟同和及以于而则且"
    # ★ 数量修饰字 —— 实测缺这组会让默认平衡档多误伤 3 处：
    #   "每个项目都要评审" / "多家公司参与投标" / "本次项目验收合格"
    # 这些字在任何位置都不可能是字号（字号不会以"每/多/次/本"起头，
    # 也不会含"本次""多个"），故放 FUNC_CHARS 而非首字黑名单——
    # 后者只在激进档生效，会让平衡档漏掉真实体。
    "每多次本"
)

#: 激进档的首字黑名单（只作用于实体名**首字符**）
LEAD_STOP: frozenset = frozenset(LEAD_STOP_CHARS)


def scan_scope(
    text: str,
    suffixes: Sequence[Tuple[str, str, str]],
    *,
    min_name_len: int = MIN_NAME_LEN,
    max_name_len: int = MAX_NAME_LEN,
    strict_lead: bool = False,
) -> List[Tuple[int, int, str, str]]:
    """反向扫描算法：找后缀 → 向前扫实体名。

    这是**纯函数**，不含 Qt / GUI 依赖，可直接单测。

    流程::

        1. 从左到右找后缀（按长度降序，保证"有限公司"先于"公司"命中）
        2. 从后缀首字符的前一位开始**向前**扫：
           遇到停止字符 / 功能字 / 长度越界 / 串首 → 停
        3. 实体名长度落在 [min_name_len, max_name_len] 且首字合规 → 产出命中
        4. 跳到本次命中的末尾继续（不重复产出嵌套后缀）

    Args:
        text: **原文**（不做任何变换，故偏移即原文偏移）。
        suffixes: ``(suffix, placeholder, group_name)`` 三元组序列，
            **必须已按后缀长度降序排列**（:func:`expand_suffixes` 保证）。
        min_name_len: 实体名最小长度（不含后缀）。
        max_name_len: 实体名最大长度（不含后缀）。
        strict_lead: True 时启用首字黑名单（激进档）。

    Returns:
        ``(start, end, matched_text, suffix)`` 列表，区间为 ``[start, end)``。

    Examples:
        >>> scan_scope("重庆创锐电气技术有限公司", [("公司", "[公司]", "单位")])
        [(0, 12, '重庆创锐电气技术有限公司', '公司')]

        >>> scan_scope("电话13812345678，公司", [("公司", "[公司]", "单位")])
        []   # "，" 是停止字符，反向扫描被挡住 → 不会吞掉手机号
    """
    if not text or not suffixes:
        return []

    results: List[Tuple[int, int, str, str]] = []
    i = 0
    n = len(text)
    # 反向扫描的**下界**：下一个后缀的搜索起点。
    #
    # ★ 没有它就会产出重叠命中（已实测）："重庆大学与西南政法大学"
    #   第一个后缀"大学"在 2，实体名 [0,4)；
    #   第二个后缀"大学"在 9，若反向扫描可越过第一个实体名的起点 0，
    #   会把"重庆大学与西南政法"整段吞进来 → 产出 (0,4) 与 (0,11)
    #   两个**互相重叠**的命中。
    #
    #   下界取"上一个已消耗区间的**末尾**"（即上一个 suffix_end）而不是
    #   实体名起点：实体名起点可能是 0，下界就等于没有。
    #   真正的约束是"反向扫到的字符必须属于**本次**的后缀前区"，
    #   而上一次产出已经把 [name_start, prev_end) 整段吃掉了。
    scan_floor = 0

    while i < n:
        # ---- 1. 定位后缀（按给定顺序，即长度降序）----
        hit_suffix: Optional[str] = None
        for suf, _placeholder, _group in suffixes:
            if suf and text.startswith(suf, i):
                hit_suffix = suf
                break
        if hit_suffix is None:
            i += 1
            continue

        # ---- 2. 从后缀往前扫实体名（不得越过 scan_floor）----
        name_end = i                      # 实体名右开边界 = 后缀起点
        j = name_end - 1
        name_len = 0
        while j >= scan_floor and name_len < max_name_len:
            ch = text[j]
            if ch in STOP_CHARS or ch in FUNC_CHARS:
                # ★ 已知代价：功能字无条件停止会砍掉"华为技术有限公司"里的
                #   "为"，产出"技术有限公司"（少脱 2 字）。
                #   试过"仅在未达最小长度时停止"，反而让"甲方为重庆某某公司"
                #   吞掉"甲方为"—— 两难都是**语言学歧义**：
                #   "为"既可能是字号内部（华为）也可能是引导词（甲方为）。
                #   取"宁可少脱不可多吞"：少脱的部分仍在报告里可见，
                #   而多吞会把无关文字（如"我们联系了"）写进占位符，
                #   改变语义。后者更危险。
                break
            j -= 1
            name_len += 1

        name_start = j + 1
        suffix_end = name_end + len(hit_suffix)

        # ---- 3. 校验 ----
        if min_name_len <= name_len <= max_name_len:
            first = text[name_start]
            if strict_lead and first in LEAD_STOP:
                # 激进档：首字是指示字 → 放弃本次，但仍要推进下界
                i = suffix_end
                scan_floor = suffix_end
                continue
            results.append((name_start, suffix_end,
                            text[name_start:suffix_end], hit_suffix))
            # 跳整段 + 抬下界：既避免嵌套后缀重复命中，也保证产出不重叠
            i = suffix_end
            scan_floor = suffix_end
            continue

        # 实体名不达标（太短）：后缀本身不产出，继续从 name_end 之后找
        i = suffix_end
        scan_floor = suffix_end

    return results


class ScopeRecognizer(Recognizer):
    """范围脱敏识别器（一个实例覆盖全部范围条目）。

    与 :class:`MultiEntryLexiconRecognizer` 一样：**所有条目共用一次扫描**，
    靠 ``suffixes`` 里的 ``group_name`` 分组。不做成"每条目一个识别器"
    —— 那样文本会被扫 N 遍，复杂度退化成 O(条目数 × 文本长度)。
    """

    def __init__(
        self,
        entries: Sequence[ScopeEntry],
        *,
        tier: Optional[str] = None,
        strict_lead: bool = False,
    ) -> None:
        """初始化识别器。

        Args:
            entries: 范围条目列表（来自 :func:`~redactor.core.scopes.load_scopes`）。
            tier: 全局严格度覆盖；None 时逐条用 ``entry.tier``。
            strict_lead: True 时启用首字黑名单（激进档）。
        """
        self._entries = [e for e in entries if e.enabled]
        # 展开成 (suffix, placeholder, group) 并按后缀长度降序
        self._suffixes = expand_suffixes(self._entries, tier=tier)
        self._strict_lead = strict_lead
        self._tier = tier
        # Recognizer 基类要求 rule 属性；范围匹配不依赖 Rule.pattern，
        # 用首个条目占位即可（仅满足基类契约），与词库识别器同构。
        super().__init__(self._entries[0] if self._entries else None)  # type: ignore[arg-type]

    # ----------------------------------------------------- 只读属性
    @property
    def group_names(self) -> Tuple[str, ...]:
        """本识别器覆盖的组名（按后缀顺序去重）。"""
        seen: List[str] = []
        for _suf, _ph, group in self._suffixes:
            if group not in seen:
                seen.append(group)
        return tuple(seen)

    @property
    def active_suffixes(self) -> Tuple[str, ...]:
        """当前启用的后缀（已按长度降序）。"""
        return tuple(suf for suf, _ph, _g in self._suffixes)

    @property
    def strict_lead(self) -> bool:
        return self._strict_lead

    @property
    def tier(self) -> Optional[str]:
        return self._tier

    def is_empty(self) -> bool:
        """是否没有启用任何后缀（空扫描器，命中恒为空）。"""
        return not self._suffixes

    # ----------------------------------------------------- 核心
    def find(self, text: str) -> List[Match]:
        """反向扫描产出全部范围命中。

        Args:
            text: **原文**（不做变换，偏移即原文偏移）。

        Returns:
            Match 列表；``placeholder`` 按**后缀**决定（裁定 4），
            如 ``重庆大学`` → ``[大学]``。
        """
        if not text or not self._suffixes:
            return []

        placeholder_by_suffix: Dict[str, str] = {
            suf: ph for suf, ph, _g in self._suffixes
        }

        matches: List[Match] = []
        for start, end, raw, suffix in scan_scope(
            text, self._suffixes, strict_lead=self._strict_lead
        ):
            matches.append(
                Match(
                    rule_name=suffix,          # ★ 按后缀分组（裁定 4）
                    placeholder=placeholder_by_suffix.get(
                        suffix, f"[{suffix}]"
                    ),
                    start=start,
                    end=end,
                    raw=raw,
                )
            )
        return matches


def build_scope_recognizer(
    entries: Sequence[ScopeEntry],
    *,
    tier: Optional[str] = None,
    strict_lead: bool = False,
) -> ScopeRecognizer:
    """工厂函数：由范围条目构造识别器。

    与 :func:`~redactor.recognizers.lexicon_recognizer.build_lexicon_recognizer`
    同构，便于按类型注入不同实现。

    Args:
        entries: 范围条目列表。
        tier: 全局严格度覆盖。
        strict_lead: True 时启用首字黑名单。

    Returns:
        一个 :class:`ScopeRecognizer`；无启用条目时返回的实例扫描任何文本都无命中
        （**安全状态**，不会误脱敏）。
    """
    return ScopeRecognizer(entries, tier=tier, strict_lead=strict_lead)


__all__ = [
    "FUNC_CHARS",
    "LEAD_STOP",
    "STOP_CHARS",
    "ScopeRecognizer",
    "build_scope_recognizer",
    "scan_scope",
]