"""自定义词库加载与严格校验。

设计要点：

1. **不与内置正则规则混用**
   词库条目在本模块里是独立的 :class:`LexiconEntry`，**不是**
   :class:`~redactor.core.models.Rule`。原因是词库走Aho-Corasick 自动机匹配，
   不需要正则；而 ``Rule.pattern`` 对词库没有意义，硬塞alternation 只会让
   ``build_recognizer`` 把它当正则规则处理，反而绕开了词库自己的匹配器。

2. **绝不静默跳过**
   任何一条词库有问题（字段缺失、未知字段、条目内重复词……）都直接抛
   :class:`RuleError` 并指出行号。跳过一个词条等于漏脱敏，必须让用户立刻看见。

3. **归一化在加载期完成**
   词条在加载时归一化一次并存入 :attr:`LexiconEntry.normalized`，
   匹配器直接使用它，不在每次扫描时重复计算。
   归一化只做两条映射（见 :func:`normalize_word`），都是 1:1 替换，
   因此**归一化文本的偏移量等于原文偏移量**，可直接用于定位替换。

4. **复用 core/yaml_loader.py**
   双树解析、行号索引、字段类型校验等能力与 ``rules.py`` 共用同一份实现
   （``core/yaml_loader.py``），不另造一套 YAML 解析，避免两份行号定位逻辑漂移。
   早期版本直接 import ``rules._compose_and_load`` 这类私有函数，导致报错文案里
   出现"规则 '词库条目 'A''"这种双层引号嵌套与领域词错位，现已解除。
   未知字段白名单不复用——``rules._ALLOWED_KEYS`` 是该模块私有常量，
   且语义不同（词库有 ``words``/``note``，没有 ``pattern``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from redactor.core.models import RuleError
from redactor.core.yaml_loader import (
    PathKey,
    compose_and_load,
    index_lines,
    read_text,
    require_bool,
    require_str,
)

# 词库条目中允许出现的字段（白名单，出现未知字段即报错，防止拼写错误被忽略）
_ALLOWED_KEYS = {
    "name",
    "words",
    "enabled",
    "numbered",
    "note",
}

# 顶层唯一允许的键
_TOP_KEY = "lexicon"


@dataclass(frozen=True)
class LexiconEntry:
    """一条词库条目（一个"类别"）。

    注意：本对象含 ``words``（用户自维护的敏感词清单），属于敏感配置，
    **禁止写入报告或日志**。报告中只能使用 ``name`` / 占位符 / 位置信息。

    Attributes:
        name: 类别名，同时决定占位符文本（``[name]``）与报告分组键。
        words: 归一化前的原始词条，保留是为了让报告/导出能还原用户写法。
        normalized: 归一化后的词条，**匹配时使用这一份**。
        enabled: 是否参与匹配。为 False 时条目仍会被加载，但调用方应跳过。
        numbered: 首版**不生效**，仅为兼容字段而保留（见 README 说明）。
        note: 备注，仅用于文档，不参与匹配。
        line: 该条目在 YAML 中的行号（从 1 开始），用于报错定位。
    """

    name: str
    words: Tuple[str, ...] = ()
    normalized: Tuple[str, ...] = ()
    enabled: bool = True
    numbered: bool = False
    note: str = ""
    line: int = 0

    @property
    def placeholder(self) -> str:
        """报告中展示的占位符文本。

        与 :attr:`redactor.core.models.Rule.placeholder` 保持同构，
        这样报告层无需区分命中来自词库还是内置正则。
        """
        return f"[{self.name}]"

    @property
    def active_words(self) -> Tuple[str, ...]:
        """实际用于匹配的词条：启用条目返回归一化结果，禁用条目返回空。"""
        return self.normalized if self.enabled else ()


# ---------------------------------------------------------------- 归一化
def normalize_word(text: str) -> str:
    """把全角字符转为半角，用于匹配前的词条归一化。

    只做两条映射，都是 1:1 逐字符替换：

    - ``U+FF01- U+FF5E``（全角 ASCII） -> 减去 ``0xFEE0`` 得半角
    - ``U+3000``（表意空格）-> ``U+0020``（半角空格）

    **为什么不用 :func:`unicodedata.normalize` 的完整 NFKC**：
    NFKC 会把 ``Ⅳ`` 展开成 ``IV``、``㍿`` 展开成 ``株式会社``、
    ``⑴`` 展开成 ``(1)`` —— 这些展开会让字符串**变长**，
    导致在归一化文本上算出的偏移量无法映射回原文，替换会错位。
    本函数的两条映射都保持长度不变，故归一化偏移 == 原文偏移。

    **为什么不做简繁互转**：Unicode 规范化不是简繁转换，需要 opencc/zhconv
    等额外依赖，与本项目"最小依赖"原则冲突。请用户在词库里两种写法都写。

    Args:
        text: 待归一化的字符串。

    Returns:
        归一化后的字符串，长度与输入相同。
    """
    out: List[str] = []
    for ch in text:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            out.append(chr(cp - 0xFEE0))
        elif cp == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- 内部工具
def _build_entry(
    data: Any,
    prefix: PathKey,
    lines: Dict[PathKey, int],
    path: Path,
    index: int,
) -> LexiconEntry:
    """把一条词库条目的数据（safe_load）与位置索引（compose）合成 LexiconEntry。

    Raises:
        RuleError: 字段非法、条目内重复词等一切问题。
    """
    fallback_line = lines.get(prefix, lines.get(prefix[:-1], 1))
    label = f"#{index + 1}"

    if not isinstance(data, dict):
        raise RuleError(
            f"第 {index + 1} 条词库条目必须是键值映射（{path} 第 {fallback_line} 行）",
            path=str(path),
            line=fallback_line,
        )

    def line_of(field: str) -> int:
        """取某字段的行号；缺省时回退到条目所在行。"""
        return lines.get(prefix + (field,), fallback_line)

    # 未知字段检查
    for field in data:
        if field not in _ALLOWED_KEYS:
            raise RuleError(
                f"词库条目含未知字段 '{field}'，允许的字段为 {sorted(_ALLOWED_KEYS)}"
                f"（{path} 第 {line_of(field)} 行）",
                path=str(path),
                line=line_of(field),
            )

    if "name" not in data:
        raise RuleError(
            f"词库条目缺少必填字段 'name'（{path} 第 {fallback_line} 行）",
            path=str(path),
            line=fallback_line,
        )
    name = require_str(data.get("name"), line_of("name"), path, "name", label)

    label = f"词库条目 '{name}'"

    if "words" not in data:
        raise RuleError(
            f"{label} 缺少必填字段 'words'（{path} 第 {fallback_line} 行）",
            path=str(path),
            line=fallback_line,
        )

    words_field_line = line_of("words")
    words_value = data.get("words")
    if not isinstance(words_value, list):
        raise RuleError(
            f"{label} 的字段 'words' 必须是列表（{path} 第 {words_field_line} 行）",
            path=str(path),
            line=words_field_line,
        )
    if not words_value:
        raise RuleError(
            f"{label} 的 'words' 为空，没有任何词条（{path} 第 {words_field_line} 行）",
            path=str(path),
            line=words_field_line,
        )

    #逐个词条：取行号 -> 校验非空 -> 归一化 -> 条目内去重
    raw_words: List[str] = []
    normalized_words: List[str] = []
    seen: Dict[str, int] = {}          # 归一化后的词 -> 首次出现的行号
    for position, item in enumerate(words_value):
        item_line = lines.get(prefix + ("words", position), words_field_line)
        if not isinstance(item, str) or not item.strip():
            raise RuleError(
                f"{label} 的第 {position + 1} 个词条必须是非空字符串"
                f"（{path} 第 {item_line} 行）",
                path=str(path),
                line=item_line,
            )
        word = item.strip()
        norm = normalize_word(word)
        if not norm:
            raise RuleError(
                f"{label} 的第 {position + 1} 个词条归一化后为空"
                f"（{path} 第 {item_line} 行）",
                path=str(path),
                line=item_line,
            )
        # 条目内重复词 = 用户笔误，必须报错；跨条目重复是正常用法，只去重。
        if norm in seen:
            # 两种情形要给不同的说明，否则报错信息会指向本行、失去意义：
            #   a) 不同行：前面已有等价词，直接指出首次出现的行号
            #   b) 同行（行内列表）：seen 里存的就是本行，报"与第 N 行相同"
            #      会指向自己。典型触发：['ＡＢＣ', ABC] —— 全角形态与半角形态
            #      归一化后相同，且都在同一行。
            if seen[norm] == item_line:
                detail = "该行与列表中已有词归一化后相同"
            else:
                detail = "归一化后与第 %d 行相同" % seen[norm]
            raise RuleError(
                f"{label} 含重复词 '{word}'"
                f"（{path} 第 {item_line} 行，{detail}）",
                path=str(path),
                line=item_line,
            )
        seen[norm] = item_line
        raw_words.append(word)
        normalized_words.append(norm)

    note = ""
    if data.get("note") is not None:
        note = str(data["note"])

    return LexiconEntry(
        name=name,
        words=tuple(raw_words),
        normalized=tuple(normalized_words),
        enabled=require_bool(
            data.get("enabled"), line_of("enabled"), path, "enabled", label, default=True
        ),
        numbered=require_bool(
            data.get("numbered"), line_of("numbered"), path, "numbered", label, default=False
        ),
        note=note,
        line=fallback_line,
    )


# ---------------------------------------------------------------- 公开 API
def load_lexicon_from_text(
    text: str,
    *,
    path: Path,
    reserved_names: Optional[Iterable[str]] = None,
) -> List[LexiconEntry]:
    """从 YAML 文本加载词库条目。

    Args:
        text: YAML 文本内容。
        path: 用于报错的文件路径（可以是虚拟名，便于测试）。
        reserved_names: 已被占用的名称集合（通常是内置正则规则名）。
            词库条目名与之冲突时**直接报错** —— 两者会产生完全相同的占位符
            （``[手机号]``），报告里无法分辨命中来自哪一方，且priority
            字典会出现同名项而语义不明。

    Returns:
        全部条目（含 ``enabled: false`` 的），保持 YAML 中的顺序。
        是否过滤由调用方决定。

    Raises:
        RuleError: YAML 语法错误、字段非法、条目内重复词、
            条目名与内置规则冲突等一切问题。
    """
    root, data = compose_and_load(text, path, kind="词库")

    if not isinstance(data, dict):
        line = 1
        raise RuleError(
            f"词库文件顶层必须是键值映射（{path} 第 {line} 行）",
            path=str(path),
            line=line,
        )

    if _TOP_KEY not in data:
        raise RuleError(
            f"词库文件缺少顶层 '{_TOP_KEY}' 列表（{path}）",
            path=str(path),
        )

    lines = index_lines(root)
    top_line = lines.get((_TOP_KEY,), 1)

    entries_data = data[_TOP_KEY]
    if not isinstance(entries_data, list):
        raise RuleError(
            f"'{_TOP_KEY}' 必须是列表（{path} 第 {top_line} 行）",
            path=str(path),
            line=top_line,
        )
    if not entries_data:
        raise RuleError(
            f"'{_TOP_KEY}' 列表为空，没有任何词库条目（{path}）",
            path=str(path),
        )

    entries: List[LexiconEntry] = []
    seen_names: Dict[str, int] = {}
    reserved = set(reserved_names or ())
    # 跨条目去重：同一词挂到两个类别下是正常用法，静默保留首次出现。
    global_seen: Dict[str, str] = {}
    for index, item in enumerate(entries_data):
        entry = _build_entry(item, (_TOP_KEY, index), lines, path, index)
        if entry.name in seen_names:
            raise RuleError(
                f"词库条目名 '{entry.name}' 重复（{path} 第 {entry.line} 行，"
                f"首次出现于第 {seen_names[entry.name]} 行）",
                path=str(path),
                line=entry.line,
            )
        seen_names[entry.name] = entry.line

        # ① 条目名不得与内置正则规则同名：
        #    两者占位符完全相同（[name] 由name 决定），报告里无法分辨来源；
        #    且引擎 priority 字典以规则名为键，同名会让优先级语义塌陷。
        if reserved and entry.name in reserved:
            raise RuleError(
                f"词库条目名 '{entry.name}' 与内置规则同名（{path} 第 {entry.line} 行）。"
                f"两者占位符都是 [{entry.name}]，报告会无法分辨命中来源。"
                f"请换一个条目名。",
                path=str(path),
                line=entry.line,
            )

        # 跨条目去重：剔除已在更早条目出现过的词
        kept: List[str] = []
        kept_norm: List[str] = []
        for word, norm in zip(entry.words, entry.normalized):
            if norm in global_seen:
                continue
            global_seen[norm] = entry.name
            kept.append(word)
            kept_norm.append(norm)
        if len(kept) != len(entry.words):
            entry = LexiconEntry(
                name=entry.name,
                words=tuple(kept),
                normalized=tuple(kept_norm),
                enabled=entry.enabled,
                numbered=entry.numbered,
                note=entry.note,
                line=entry.line,
            )
        entries.append(entry)

    return entries


def load_lexicon(
    path: str | Path,
    *,
    reserved_names: Optional[Iterable[str]] = None,
) -> List[LexiconEntry]:
    """从 YAML 文件加载词库条目（**不过滤** ``enabled``）。

    Args:
        path: 词库文件路径。
        reserved_names: 已被占用的名称集合（通常传内置规则名），
            用于阻止词库条目名与内置规则冲突。

    Returns:
        词库条目列表（保持 YAML 顺序）。

    Raises:
        RuleError: 文件不存在、编码错误、YAML 语法错误、字段非法等。
    """
    text, lex_path = read_text(path, kind="词库")

    return load_lexicon_from_text(text, path=lex_path, reserved_names=reserved_names)


def default_lexicon_path() -> Optional[Path]:
    """返回默认词库路径（``~/.redactor/lexicon.yaml``）。

    文件不存在时返回 ``None``——**是否报错由调用方决定**：
    CLI 显式指定 ``--lexicon`` 却找不到文件必须报错，
    而未指定时用默认路径且文件不存在属于正常情况。
    """
    candidate = Path.home() / ".redactor" / "lexicon.yaml"
    return candidate if candidate.is_file() else None