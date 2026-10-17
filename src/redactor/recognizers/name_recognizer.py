"""P8 人名识别器：基于 jieba 词性标注（nr / nrfg / nrt）。

为什么只取人名词性
----------------
jieba 的词性体系里能当"人"的只有三个：

    nr   人名（普通）        张三 / 李四
    nrfg 人名（复姓）        欧阳修
    nrt  人名（单姓）        （jieba 会把部分人名拆成单姓 + 名字）

本识别器**只**取这三个。组织名（nnt）、地名（ns）、量词（m）等一律不取 ——
调研报告明确限定了这个范围。

大字金额剔除（实测依据，见 .verify/probe_p8/probe_p8_output.log）
------------------------------------------------------------------
jieba 会把中文大写金额里的部分片段误判成人名。实测语料 12 条负例：

    已支付伍拾万元整
        全部分词: 已(d) 支付(v) **伍拾(nr)** 万元(m) 整(v)
        → 误判"伍拾"为人名

    本次付款金额为叁佰伍拾陆万元柒仟捌佰玖拾元
        全部分词: … 叁(m) 佰(m) **伍拾(nr)** **陆(nr)** 万元(m) 柒(m) 仟(m) 捌(m) 佰(m) 玖(m) 拾元(v)
        → 误判"伍拾"与"陆"

    剔除效果：误判词 3 个，含大字 3 个 → **可消除 100%**
             剔除后负例误伤 **0/12**

★关键实现细节：**必须按字符集判，不能按词长**
    jieba 把"柒仟捌佰玖拾"切成 5 个**单字**且标成 ``m``（不是 ``nr``），
    只误判了其中的"伍拾""陆"。也就是说误判片段的长度不固定
    （1 字、2 字都有），靠"长度 <= N"来剔除会漏；
    而靠"含大写数字字符"来判定则100% 命中（实测）。

偏移量约定
----------
jieba 的 :func:`pseg.cut` 在**纯文本**上给出 ``(word, flag)``，
但**不给偏移量**。本模块用 :meth:`jieba.tokenize` 拿到带偏移的 tokenize
结果（``(start, end, word, flag)``），从而保证产出的 ``Match.start/end``
是**原文偏移**，可直接用于切原文与替换 —— 与其他识别器保持同构。

    ⚠️ ``pseg.cut`` 与 ``jieba.tokenize`` 用的**不是**同一套算法路径，
    分词结果可能不同。故本模块统一用 ``tokenize``（带偏移那个），
    不混用 —— 混用会导致偏移与词边界对不上。
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Set, Tuple

from redactor.core.models import Match, Rule
from redactor.recognizers.base import Recognizer

#: 启用的人名词性（调研报告限定）
NAME_TAGS: Tuple[str, ...] = ("nr", "nrfg", "nrt")

#: 大写数字字符集 —— 中文大写金额专用
#: 包含"元角分圆"是为了覆盖"壹佰元"这种**整体被切成一个词**的情况。
DA_ZI_CHARS: frozenset = frozenset(
    "零壹贰叁肆伍陆柒捌玖"
    "拾佰仟万亿兆"
    "元角分圆"
)

#: 大字金额剔除开关（关掉它就是反验变异体）
STRIP_DA_ZI = True


def is_da_zi_amount(word: str) -> bool:
    """判断一个词是否是大字金额片段。

    判据：**含任意一个大写数字/金额量词字符**。

    为什么不是"长度 <= 2"：实测 jieba 把"柒仟捌佰玖拾"切成 5 个单字（标 ``m``），
    只把"伍拾""陆"误判成 ``nr``—— 误判片段长度在 1~2 字之间浮动，
    按长度剔除会漏掉长片段里未被误判的情况，且对"壹佰元"这种
    整体成词的长片段无效。含字符判则对两种长度都成立（实测 100%）。

    Args:
        word: 待判定的词。

    Returns:
        含大写金额字符返回 True。
    """
    return bool(set(word) & DA_ZI_CHARS)


class NameRecognizer(Recognizer):
    """人名识别器（jieba 词性）。

    一个实例服务全部人名词性 —— jieba 的分词结果里直接带 flag，
    按 flag 过滤即可，不需要"每个词性一个识别器"。

    **jieba 是可选依赖**：未安装时 :func:`build_name_recognizer` 返回 ``None``，
    调用方据此跳过（与词库"文件不存在返回 None"的处理方式一致）。
    绝不在缺依赖时抛 ImportError 让整个程序起不来 ——
    人名识别是**增强功能**，不该阻断主流程。
    """

    def __init__(
        self,
        *,
        tags: Sequence[str] = NAME_TAGS,
        strip_da_zi: bool = STRIP_DA_ZI,
        placeholder: str = "[人名]",
        rule_name: str = "人名",
    ) -> None:
        """初始化识别器。

        Args:
            tags: 启用的人名词性（默认 ``nr`` / ``nrfg`` / ``nrt``）。
            strip_da_zi: 是否剔除大字金额片段（反验开关）。
            placeholder: 占位符文本。
            rule_name: 规则名（报告分组键）。
        """
        # Recognizer 基类要求 .rule 属性；本识别器不依赖 Rule.pattern，
        # 用一个占位 Rule 满足契约（与词库识别器同构）。
        placeholder_rule = Rule(
            name=rule_name,
            pattern=r"(?!)",          # 永不匹配的真假 pattern
            placeholder=placeholder,
        )
        super().__init__(placeholder_rule)
        self._tags: Tuple[str, ...] = tuple(tags)
        self._tag_set: Set[str] = set(tags)
        self._strip_da_zi = strip_da_zi
        self._placeholder = placeholder

    @property
    def tags(self) -> Tuple[str, ...]:
        return self._tags

    @property
    def strip_da_zi(self) -> bool:
        return self._strip_da_zi

    def find(self, text: str) -> List[Match]:
        """在文本中查找全部人名。

        Args:
            text: **原文**。

        Returns:
            Match 列表；``start`` / ``end`` 为原文偏移，``raw`` 为原文切片。
        """
        if not text or not self._tag_set:
            return []
        try:
            import jieba
        except ImportError:
            # 可选依赖缺失 —— 静默返回空（安全状态：不脱敏而非崩溃）
            return []

        matches: List[Match] = []
        # ★用 tokenize（带偏移）而不是 pseg.cut（不带偏移）：
        #   两者分词路径不同，混用会导致偏移与词边界对不上。
        #   代价是拿不到 flag —— 故这里用 jieba 的另一入口取 flag。
        for start, end, word, flag in self._iter_tokens(text):
            if flag not in self._tag_set:
                continue
            if self._strip_da_zi and is_da_zi_amount(word):
                continue
            matches.append(
                Match(
                    rule_name=self.rule.name,
                    placeholder=self._placeholder,
                    start=start,
                    end=end,
                    raw=text[start:end],
                )
            )
        return matches

    @staticmethod
    def _iter_tokens(text: str) -> Iterable[Tuple[int, int, str, str]]:
        """产出 ``(start, end, word, flag)`` 四元组（坐标指向**原文**）。

        jieba 的两个入口各缺一半：
          - :func:`jieba.tokenize` 给偏移，但用**精确模式**分词，
            会把"张三在"切成一个词，且**不给**词性 flag
          - :func:`jieba.posseg.cut` 给 flag，但**不给**偏移

        ★这两者分词路径**确实不同**（实测："张三在重庆创锐公司工作"
        下``tokenize`` 得``[张三在, 重庆, 创锐, 公司, 工作]``，
        ``pseg.cut`` 得 ``[张三/nr, 在/p, 重庆/ns, 创锐/v, 公司/n, 工作/vn]``），
        所以"用 tokenize 的偏移 + pseg 的 flag 查表配对"是**错的** ——
        实测该方案会一个都匹配不上。

        正确做法：**只用一个入口**（``pseg.cut``，它同时给词与flag），
        自己按**顺序累加**偏移：

            pos = 0
            for word, flag in pseg.cut(text):
                yield pos, pos + len(word), word, flag
                pos += len(word)

        ★之所以偏移可靠，是因为 ``pseg.cut`` 的分词结果**无缝拼接回原文**
        （不丢字符、不插入字符）—— 这与本项目词库归一化的"1:1 替换"
        前提完全一致。若某个版本不满足该前提，
        下方 ``assert`` 会立刻炸出来而不是静默错位。
        """
        import jieba.posseg as pseg

        pos = 0
        for word, flag in pseg.cut(text):
            end = pos + len(word)
            # 地基断言：分词必须能无损拼回原文，否则偏移会静默错位
            assert text[pos:end] == word, (
                f"jieba 分词无法无损拼回原文：偏移 {pos}..{end} 处"
                f"期望 {word!r}，实际 {text[pos:end]!r}"
            )
            yield pos, end, word, flag
            pos = end

        assert pos == len(text), (
            f"jieba 分词丢失了尾部字符：累加到 {pos}，原文长度 {len(text)}"
        )


def build_name_recognizer(
    *,
    tags: Sequence[str] = NAME_TAGS,
    strip_da_zi: bool = STRIP_DA_ZI,
    placeholder: str = "[人名]",
    rule_name: str = "人名",
) -> Optional[NameRecognizer]:
    """工厂函数：构造人名识别器。

    Args:
        tags: 启用的人名词性。
        strip_da_zi: 是否剔除大字金额片段。
        placeholder: 占位符文本。
        rule_name: 规则名。

    Returns:
        :class:`NameRecognizer` 实例；**jieba 未安装时返回 ``None``**
        （调用方须跳过，不可当作异常抛）。
    """
    try:
        import jieba  # noqa: F401
    except ImportError:
        return None
    return NameRecognizer(
        tags=tags, strip_da_zi=strip_da_zi,
        placeholder=placeholder, rule_name=rule_name,
    )


def jieba_available() -> bool:
    """jieba 是否可用（GUI 开关的可用性判断用）。"""
    try:
        import jieba  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = [
    "DA_ZI_CHARS",
    "NAME_TAGS",
    "NameRecognizer",
    "STRIP_DA_ZI",
    "build_name_recognizer",
    "is_da_zi_amount",
    "jieba_available",
]