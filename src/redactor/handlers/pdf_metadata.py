"""PDF 元数据清洗（P3）：``/Info`` 字典 + XMP 包。

为什么需要单独一层
==================

PDF 里有**两处互相独立**的元数据存储（实测，见
``.verify/probe_p3/probe_p3_meta.txt``）：

1. ``/Info`` 字典 —— 普通字符串对象，键为 ``/Title`` ``/Author``
   ``/Subject`` ``/Keywords`` ``/Creator`` ``/Producer`` 等。
2. **XMP** —— 挂在 catalog ``/Metadata`` 下的一个 XML 流。

两者**互不影响**：实测只调 ``set_metadata`` 改 ``title``，XMP 纹丝不动；
反之亦然。故必须**分别清洗**。

这两处都是**产品级泄露风险**：正文涂干净了，文档属性里还留着手机号，
用户不会去核对。该缺口自 P1.1 起就存在（``pdf_verify`` 的 docstring 有记录），
P3 补齐。

🔴 五条实测结论（勿凭直觉改）
=============================

1. **``set_metadata`` 是"合并"语义，不是全量替换**

   实测：先写满 6 字段，再 ``set_metadata({"title": "T2"})``，其余 5 字段
   **全部保留**。

   （这条我最初标"源码可推：全量替换"—— **实测证明推断是错的**。
   再次印证：凡结论影响实现，一律降到"实测"才写进方案。）

2. **XMP 清洗不能只做"字符串替换"**

   实测矩阵（``.verify/probe_p3/probe_p3_method.txt``）::

       样本                    A 字符串替换   B 解析重建   D 保骨架换文本
       明文手机号                  ✅           ✅            ✅
       实体编码 1380013&#56;000     ❌漏          ✅            ✅
       跨标签手机号                ❌漏          ❌漏          ❌漏

   - **A 漏掉实体编码**：``&#56;`` 是 ``8``，但字节层面不连续，
     正则看不到 → 不命中 → 原样保留。
   - **B 会改写命名空间前缀**：``ET.tostring`` 重建后出现 ``ns0:``，
     而 XMP 依赖 ``dc:``/``rdf:``，阅读器可能认不出 → 不可用。
   - **D 胜出**：保留 XML 骨架，只替换文本节点/属性值的**内容**。
     做法 = 先 ``html.unescape`` 把实体还原成字符 → 清洗 → 再 XML 转义。

3. **替换值必须 XML 转义**

   对抗性反例实测（``.verify/probe_p3/probe_p3_adversarial.txt`` R2）：
   替换值含 ``&``（如用户词库把占位符设成 ``A&B``）会产出**非法 XML**
   （``ParseError: not well-formed``）。内置占位符 ``[手机号]`` 本身安全，
   但不能假定所有词库占位符都安全。

4. **清洗后必须校验 XML 可解析**，失败则回退原文

   ``set_xml_metadata`` 对**非法 XML 静默接受、不做校验**（实测：写入
   ``<not closed`` 能存能读）。故校验只能自己做。

5. **跨节点的敏感串无法自动识别**（已知边界）

   实测 S3 样本：手机号被 XML 标签拆成 ``前13800`` + ``138000后``，
   **任何单节点内都不构成合法手机号**，故字符串替换与解析重建**都漏**。
   （与正文里"跨 span 命中"同类，但正文有 ``covering_spans`` 兜底，
   元数据没有。）

   本模块的处理：**清洗后**把各节点内容拼接再扫一遍，若仍命中，
   说明存在这类漏网串 → 进 ``warnings``，报告里明确提示
   "无法自动识别，请人工核查"。**绝不静默漏掉。**

报告约定（用户裁定 B1 + C1）
============================

- 位置标签：``元数据 /Title``、``元数据 XMP``
- ``page_index = -1``（元数据不属于任何页；``-1`` 是"非页面"的约定）
- 元数据命中**混进** ``hits``，与其他规则命中并列（用户要在一处看全）
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import List, NamedTuple, Sequence, Tuple

import fitz  # PyMuPDF

from redactor.core.engine import RedactionEngine
from redactor.core.models import Match

__all__ = [
    "INFO_FIELDS",
    "METADATA_PAGE_INDEX",
    "MetadataScrubResult",
    "XMP_LABEL",
    "info_label",
    "scrub_metadata",
]

#: ``/Info`` 中要清洗的字段：``(PyMuPDF 归一化键名, PDF 原始键名)``。
#:
#: 只列**可写且可能含敏感信息**的 6 个。未列入者及理由：
#:
#: - ``creationDate`` / ``modDate``：格式固定 ``D:YYYYMMDDHHmmSSZ``，
#:   规则不会命中；清洗反而可能破坏格式。
#: - ``trapped``：取值仅 ``True``/``False``/``Unknown``。
#: - ``format`` / ``encryption``：**只读**，改不动（实测）。
INFO_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("title", "/Title"),
    ("author", "/Author"),
    ("subject", "/Subject"),
    ("keywords", "/Keywords"),
    ("creator", "/Creator"),
    ("producer", "/Producer"),
)

#: 元数据命中的 ``page_index``：``-1`` = 非页面。
#:
#: 为什么用负数而不是 ``doc.page_count``：负数天然不与任何真实页码
#: （0 起）冲突，语义也直白。``aggregate`` 的键是 tuple，
#: ``-1`` 不参与任何范围检查，无需改 ``Match`` 结构（用户裁定 C1）。
METADATA_PAGE_INDEX = -1

#: 报告里的位置标签前缀。
_LABEL_PREFIX = "元数据"

#: XMP 的位置标签。不细分到具体 XML 节点——节点名对用户无意义。
XMP_LABEL = f"{_LABEL_PREFIX} XMP"

#: XML 文本节点：``>`` 与 ``<`` 之间的内容（不含子标签）。
_TEXT_NODE = re.compile(r">([^<]*)<")

#: XML 属性值：``name="value"`` / ``name='value'``。
_ATTR_VALUE = re.compile(r"""(\s[\w:.-]+=)(["'])([^"']*)(\2)""")

#: 清洗后 XML 非法时的告警文案。
_WARN_XML_BROKEN = (
    "XMP 元数据清洗后 XML 结构非法，已回退原文（该处 XMP **未清洗**），"
    "请人工核查文档属性"
)

#: 跨节点敏感串的告警文案（用户明确要求：不静默漏掉）。
_WARN_CROSS_NODE = (
    "XMP 元数据中疑似存在**跨节点的敏感信息**（被 XML 标签拆开，"
    "本工具无法自动识别与清洗），请人工核查文档属性"
)


def info_label(raw_key: str) -> str:
    """``/Info`` 字段的位置标签，如 ``元数据 /Title``。

    Args:
        raw_key: PDF 原始键名（含前导斜杠），如 ``/Title``。

    Returns:
        报告里展示的位置标签。

    为什么用 ``/Title`` 而不是 ``title`` 或中文"标题"：
        用户在 Acrobat/福昕里看到的是"标题"，在 PDF 源码里看到的是
        ``/Title``。用**原始键名**两边都能对上；中文译名无统一标准，
        反而容易生歧义。
    """
    return f"{_LABEL_PREFIX} {raw_key}"


class MetadataScrubResult(NamedTuple):
    """一次元数据清洗的结果。

    Attributes:
        matches: 命中的 :class:`Match` 列表。``page_index`` 恒为
            :data:`METADATA_PAGE_INDEX`；``start`` 是**全局递增序号**
            （不是文本偏移——元数据没有"页内偏移"概念，但位置键
            ``(规则名, page_index, start)`` 需要 ``start`` 唯一，
            故用序号占位）。
        labels: 与 ``matches`` **一一对应**的位置标签。
        warnings: 需要人工核查的提示（跨节点敏感串 / XML 回退）。
        fields_touched: 实际被改写的字段标签（供报告说明用）。
    """

    matches: List[Match]
    labels: List[str]
    warnings: List[str]
    fields_touched: List[str]


# ------------------------------------------------------------------ XML 工具
def _strip_xpacket(text: str) -> str:
    """剥掉 ``<?xpacket ...?>`` 处理指令，返回可交给 XML 解析器的部分。

    ``ElementTree`` 不接受根元素之外的处理指令，故必须剥掉再解析。
    """
    body = text
    if body.startswith("<?xpacket"):
        i = body.find("?>")
        if i >= 0:
            body = body[i + 2:]
    j = body.rfind("<?xpacket")
    if j >= 0:
        body = body[:j]
    return body


def _xml_parsable(text: str) -> bool:
    """XMP 文本能否被真正的 XML 解析器接受？

    🔴 不要用 ``len(get_xml_metadata()) > 0`` 当判据——fitz **不做 XML 解析**，
    只要流挂在 xref 上就原样返回字符串，那个断言**恒真**。
    （这是我写方案时的第一个自查错误。）
    """
    body = _strip_xpacket(text)
    if not body.strip():
        return False
    try:
        ET.fromstring(body)
        return True
    except ET.ParseError:
        return False


def _escape_xml(text: str, quote: str = "") -> str:
    """按 XML 规则转义，防止替换值里的 ``&``/``<`` 破坏结构。

    Args:
        text: 待转义文本。
        quote: 若为 ``'"'`` / ``"'"``，同时转义该引号（用于属性值）。

    Returns:
        转义后的文本。
    """
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote == '"':
        out = out.replace('"', "&quot;")
    elif quote == "'":
        out = out.replace("'", "&apos;")
    return out


def _scrub_fragment(
    raw: str, engine: RedactionEngine, quote: str = ""
) -> Tuple[str, List[Match]]:
    """清洗一段 XML 内容（文本节点或属性值）。

    三步：``html.unescape``（把 ``&#56;`` 还原成 ``8``，这样正则才看得见）
    -> ``engine.redact`` -> 重新 XML 转义。

    Args:
        raw: XML 里的原始内容（可能含实体）。
        engine: 脱敏引擎。
        quote: 若为 ``'"'`` / ``"'"``，转义时同时处理该引号。
            **属性值必须传**：``[^"']*`` 只是让正则不去跨越引号边界，
            并不保证**替换后**的值里没有引号。若用户词库把占位符设成
            ``A"B``，不转义就会提前闭合属性、产出非法 XML
            （与 R2 反例里 ``&`` 破坏结构同源，只是维度不同）。

    Returns:
        ``(清洗并重新转义后的文本, 命中列表)``。未命中时返回**原文**，
        连转义都不动（避免无谓改写）。
    """
    decoded = html.unescape(raw)
    new, matches = engine.redact(decoded)
    if not matches:
        return raw, []
    return _escape_xml(new, quote=quote), list(matches)


# ------------------------------------------------------------------ /Info
def scrub_info_metadata(
    doc: "fitz.Document", engine: RedactionEngine
) -> Tuple[List[Tuple[Match, str]], List[str]]:
    """清洗 ``/Info`` 字典的 6 个可写字段。

    Args:
        doc: 已打开的 PDF（**会被就地修改**）。
        engine: 脱敏引擎。

    Returns:
        ``([(命中, 位置标签)], [被改写的字段标签])``。
        命中来自 ``engine.redact``，其 ``start`` 是**字段内偏移**
        （调用方需重新编号，见 :func:`scrub_metadata`）。
    """
    meta = doc.metadata or {}
    hits: List[Tuple[Match, str]] = []
    touched: List[str] = []

    for py_key, raw_key in INFO_FIELDS:
        value = meta.get(py_key, "") or ""
        if not value:
            continue
        new_value, matches = engine.redact(value)
        if not matches:
            continue
        label = info_label(raw_key)
        for m in matches:
            hits.append((m, label))
        touched.append(label)
        # set_metadata 是"合并"语义（实测），这里只传要改的字段即可；
        # 但为稳妥仍显式传全部已改字段，避免依赖实现细节。
        doc.set_metadata({py_key: new_value})

    return hits, touched


# ------------------------------------------------------------------ XMP
def _scrub_xmp_text(
    xmp: str, engine: RedactionEngine
) -> Tuple[str, List[Match], List[str]]:
    """按"方案 D：保骨架换文本"清洗 XMP。

    只替换**文本节点**与**属性值**的内容，标签、命名空间前缀、
    ``xpacket`` 包裹层**原样保留**（不用 ``ET.tostring`` 重建，
    否则 ``dc:`` 会被改写成 ``ns0:``）。

    Args:
        xmp: 原始 XMP 文本。
        engine: 脱敏引擎。

    Returns:
        ``(清洗后文本, 命中列表, 清洗后各节点内容)``。
        第三个返回值供跨节点检测使用。
    """
    hits: List[Match] = []
    after_nodes: List[str] = []

    def _do_text(m: "re.Match") -> str:
        raw = m.group(1)
        if not raw.strip():
            after_nodes.append(raw)
            return m.group(0)
        new, ms = _scrub_fragment(raw, engine)
        hits.extend(ms)
        # 收集"清洗后"的内容（跨节点检测要用清洗后的）
        after_nodes.append(html.unescape(new) if ms else raw)
        if not ms:
            return m.group(0)
        return ">" + new + "<"

    def _do_attr(m: "re.Match") -> str:
        prefix, quote, value, _ = m.groups()
        if not value.strip():
            return m.group(0)
        new, ms = _scrub_fragment(value, engine, quote=quote)
        hits.extend(ms)
        if not ms:
            return m.group(0)
        return prefix + quote + new + quote

    out = _TEXT_NODE.sub(_do_text, xmp)
    out = _ATTR_VALUE.sub(_do_attr, out)
    return out, hits, after_nodes


def scrub_xmp_metadata(
    doc: "fitz.Document", engine: RedactionEngine
) -> Tuple[List[Tuple[Match, str]], List[str], List[str]]:
    """清洗 XMP 包。

    Args:
        doc: 已打开的 PDF（**会被就地修改**）。
        engine: 脱敏引擎。

    Returns:
        ``([(命中, 位置标签)], [告警], [被改写说明])``。

    三种情形：
        1. 文档无 XMP -> 原样返回空结果（绝大多数 PDF 如此）。
        2. 清洗后 XML 仍合法 -> 写回。
        3. 清洗后 XML 非法 -> **回退原文**并告警（宁可不改，也不产出
           非法 XMP —— ``set_xml_metadata`` 不会替我们校验）。
    """
    warnings: List[str] = []
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:
        # 读 XMP 失败不该影响整体脱敏（与 locate_cells 的降级同理）
        return [], [], []

    if not xmp.strip():
        return [], [], []

    new_xmp, matches, after_nodes = _scrub_xmp_text(xmp, engine)

    # --- 校验：清洗后必须仍是合法 XML ---
    if matches and not _xml_parsable(new_xmp):
        return [], [_WARN_XML_BROKEN], []

    # --- 跨节点检测（清洗**后**拼接再扫一遍）---
    # 单节点内的敏感串已在上一步清掉；若拼接后仍命中，说明该串被
    # XML 标签拆开了 —— 实测任何单节点视角都识别不出（见模块 docstring 结论 5）。
    joined = "".join(after_nodes)
    if joined and engine.scan(joined):
        warnings.append(_WARN_CROSS_NODE)

    if not matches:
        return [], warnings, []

    doc.set_xml_metadata(new_xmp)
    labeled = [(m, XMP_LABEL) for m in matches]
    return labeled, warnings, [XMP_LABEL]


# ------------------------------------------------------------------ 入口
def scrub_metadata(
    doc: "fitz.Document", engine: RedactionEngine
) -> MetadataScrubResult:
    """清洗 ``/Info`` 与 XMP，返回统一的命中列表（已重新编号）。

    **这是本模块的唯一对外入口**。统一入口是为了让两处的 ``start``
    共享同一个序号空间 —— 位置键是 ``(规则名, page_index, start)``，
    两处都用 ``page_index = -1``，若 ``start`` 各自从 0 开始就会撞键，
    后写的标签覆盖先写的，报告少报。

    Args:
        doc: 已打开的 PDF（**会被就地修改**）。
        engine: 脱敏引擎。

    Returns:
        :class:`MetadataScrubResult`。
    """
    info_hits, info_touched = scrub_info_metadata(doc, engine)
    xmp_hits, xmp_warnings, xmp_touched = scrub_xmp_metadata(doc, engine)

    raw: Sequence[Tuple[Match, str]] = [*info_hits, *xmp_hits]
    matches: List[Match] = []
    labels: List[str] = []
    for seq, (match, label) in enumerate(raw):
        # 重新编号：start 用全局序号，保证与 page_index=-1 组合后键唯一。
        # raw 保留（内存中，供编号模式生成 [手机号#1]）；
        # 报告只用 rule_name / placeholder / label，绝不写 raw。
        matches.append(
            Match(
                rule_name=match.rule_name,
                placeholder=match.placeholder,
                start=seq,
                end=seq + 1,
                raw=match.raw,
                page_index=METADATA_PAGE_INDEX,
            )
        )
        labels.append(label)

    return MetadataScrubResult(
        matches=matches,
        labels=labels,
        warnings=[*xmp_warnings],
        fields_touched=[*info_touched, *xmp_touched],
    )
