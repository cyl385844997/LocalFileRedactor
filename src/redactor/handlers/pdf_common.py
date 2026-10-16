"""PDF 相关的公共设施：页面文本提取、矩形判定、坐标映射。

本模块只做**只读的分析与定位**，不做任何写入。
写入（redaction + 占位符）由 :mod:`redactor.handlers.pdf_handler` 负责。

为什么要单独抽一层：
    PDF 没有"段落/字符偏移"的概念，命中定位只能靠**矩形**。
    而现有 :class:`~redactor.core.models.Match` 用的是 ``start``/``end``
    字符偏移 —— 两者语义不同，不能混用。本模块负责把"矩形命中"
    翻译成引擎认得的 :class:`Match`（用**页内全局字符偏移**当 key，
    满足 :meth:`RedactionEngine.aggregate` 的 ``(rule_name, start)`` 契约），
    同时保留原始矩形供 redaction 使用。

实测得到的三条硬约束（代码里已按此设计，勿改）：

1. **一个 span 可能只含 1 个字**
   真实 PDF 里 ``span(text='年')`` 单独成块。因此不能用"整个页面拼成
   一个大字符串再跑find()" 的做法——那样算出的偏移无法映射回矩形。

2. **表格文字在 ``get_text()`` 里被串成连续文本**
   ``'科技研发部陈彦霖报销...差旅费\\n3,106.64\\n银行存款'``，
   没有单元格边界。要拿行列号必须用 :meth:`Page.find_tables`。

3. **多栏时 ``get_text()`` 按 Y 坐标交替串栏**
   左栏第 1 行、右栏第 1 行、左栏第 2 行……不是"先左后右"。
   因此**不能依赖阅读顺序**，只能按矩形处理（这恰好是我们的做法）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import fitz  # PyMuPDF

from redactor.core.models import Match

#: 坐标判定用的矩形别名（PyMuPDF 的 Rect）
Rect = fitz.Rect


@dataclass(frozen=True)
class SpanInfo:
    """页面上的一个文本 span（PyMuPDF 的最小文本单元）。

    Attributes:
        bbox: 矩形坐标 ``(x0, y0, x1, y1)``，单位 pt。
        text: span 文本。**可能只有 1 个字**（见模块 docstring 实测 1）。
        font: 字体名。
        size: 字号。
        offset: 该 span 文本在"页内全局字符串"中的 ``[start, end)``。
            用于生成 :class:`Match`，保证 ``aggregate`` 的 key 唯一。
    """

    bbox: Rect
    text: str
    font: str
    size: float
    offset: int

    @property
    def end(self) -> int:
        """span 文本在页内全局字符串中的结束偏移。"""
        return self.offset + len(self.text)


@dataclass(frozen=True)
class TextBoxHit:
    """span 矩形内的一个命中。

    Attributes:
        rule_name: 命中来源的规则名或词库条目名。
        placeholder: 占位符文本，如 ``[手机号]``。
        span: 命中起始所在的 span。
        start_in_span: 命中在 ``span.text`` 内的起始下标。
        end_in_span: 命中在 ``span.text`` 内的结束下标（不含）。
        match: 供引擎统计用的 :class:`Match`（``start`` 是页内全局偏移）。
        covering_spans: 与该命中的全局偏移区间有交叠的**全部** span。

            **这是 PDF 相对 docx 的核心差异**：PDF 里一个 span 可能只有
            1 个字（实测真实 PDF 中'年'/'月'/'日'各自独立成span），
            11 位手机号会横跨多个 span。若只涂起始 span，就只盖住 1/11 ——
            这是**漏脱敏**。故落盘时必须取所有 covering_spans 的并集矩形。
    """

    rule_name: str
    placeholder: str
    span: SpanInfo
    start_in_span: int
    end_in_span: int
    match: Match
    covering_spans: Tuple[SpanInfo, ...] = ()
    #: 所在页的**0 起**下标。
    #: 必须显式携带，**不能**从位置标签字符串反解——实测反解会在
    #: 第 10 页时被 ``"第 1 页第 1 处"`` 之类的截取逻辑误判成第 1 页，
    #: 导致多页文档只处理了部分页。
    page_index: int = 0

    @property
    def rect(self) -> Rect:
        """命中的矩形（按 span 内子串比例估算）。

        PyMuPDF 没有"子串级坐标"API，只能按字符数在 span 宽度上等分估算。
        估算偏差对 redaction 无影响——redaction 宁可**稍微多盖一点**
        （用整个 span 的 bbox），也不能少盖（漏脱敏）。
        因此本属性仅供报告/调试展示，落盘一律用
        :func:`rects_for_hits`（取 covering_spans 的并集）。
        """
        span_rect = self.span.bbox
        n = len(self.span.text) or 1
        x0 = span_rect.x0 + (span_rect.x1 - span_rect.x0) * self.start_in_span / n
        x1 = span_rect.x0 + (span_rect.x1 - span_rect.x0) * self.end_in_span / n
        return Rect(x0, span_rect.y0, x1, span_rect.y1)

    @property
    def span_rect_safe(self) -> Rect:
        """起始 span 的整个 bbox。

        仅在拿不到 covering_spans 时作为兜底。优先用
        :func:`rects_for_hits`，它会合并全部覆盖 span。
        """
        return self.span.bbox


def extract_spans(page: "fitz.Page") -> Tuple[List[SpanInfo], str]:
    """提取页面上所有文本 span，并构造"页内全局字符串"。

    构造全局字符串的目的**不是**拿它去匹配（PDF 的 span 可能只有 1 个字，
    拼起来的串与视觉排版毫无关系），而是给每个 span 分配一个**唯一且稳定**
    的偏移区间，使 :class:`Match` 的 ``(rule_name, start)`` 在页内不冲突。

    Args:
        page: PyMuPDF 页面对象（只读）。

    Returns:
        ``(span列表, 页内全局字符串)``。无文本层时 span 列表为空、字符串为 ""。
    """
    spans: List[SpanInfo] = []
    parts: List[str] = []
    cursor = 0
    data = page.get_text("dict")
    for block in data.get("blocks", ()):
        if block.get("type") != 0:      # 0=文本块，1=图像块
            continue
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                text = span.get("text", "")
                if not text:
                    continue
                bbox = Rect(span["bbox"])
                spans.append(
                    SpanInfo(
                        bbox=bbox,
                        text=text,
                        font=str(span.get("font", "")),
                        size=float(span.get("size", 0.0)),
                        offset=cursor,
                    )
                )
                parts.append(text)
                cursor += len(text)
    return spans, "".join(parts)


def page_has_text_layer(page: "fitz.Page") -> bool:
    """判断页面是否有文本层。

    扫描件页面没有文本层——图像就是图像，取不出任何字符。
    这类页面**必须显式报错**而不是静默通过：静默通过等于让用户以为
    已经脱敏，实际原文还在图像里。

    判定方式：``get_text()`` 去掉空白后是否为空。
    注意 PyMuPDF **没有** ``strip=True`` 这个参数（实测
    ``TypeError: got an unexpected keyword argument 'strip'``），
    只能自己 ``.strip()``。
    """
    return bool(page.get_text().strip())


def iter_pages(doc: "fitz.Document") -> Iterable["fitz.Page"]:
    """遍历文档所有页面。"""
    for index in range(doc.page_count):
        yield doc[index]


def offset_to_span(spans: Sequence[SpanInfo], offset: int) -> Optional[SpanInfo]:
    """按页内全局偏移反查所属 span。"""
    for span in spans:
        if span.offset <= offset < span.end:
            return span
    return None


def merge_overlapping_rects(rects: Sequence[Rect]) -> List[Rect]:
    """合并重叠/相邻的矩形，减少 redaction 次数。

    同一 span 内可能有多处命中（如 '甲甲公司' 中'甲' 命中两次），
    逐个add_redact_annot 会产生重叠标注，PyMuPDF 处理重叠标注时
    容易出现边界异常。合并后只盖一次，更稳。

    注意：合并只在**同一行**（y 范围高度重叠）内进行，跨行合并会
    连带盖掉不该涂的内容。
    """
    if not rects:
        return []
    ordered = sorted(rects, key=lambda r: (round(r.y0, 1), r.x0))
    merged: List[Rect] = [Rect(ordered[0])]
    for rect in ordered[1:]:
        last = merged[-1]
        same_line = rect.y0 < last.y1 and last.y0 < rect.y1
        touches = rect.x0 <= last.x1 + 0.5
        if same_line and touches:
            merged[-1] = Rect(
                min(last.x0, rect.x0),
                min(last.y0, rect.y0),
                max(last.x1, rect.x1),
                max(last.y1, rect.y1),
            )
        else:
            merged.append(Rect(rect))
    return merged


#: 占位符写入使用的内置 CJK 字体名。
#:实测：``'china-s'`` 内置可用，零配置、不增大文件；
#: ``'simsun'`` 会报 ``need font file or buffer``，需显式传字体文件。
CJK_FONT = "china-s"


def rects_for_hits(hits: Sequence["TextBoxHit"]) -> List[Rect]:
    """求出覆盖一批命中的矩形列表（每个命中一个矩形）。

    **跨 span 的关键处理**：一个命中（如 11 位手机号）可能横跨多个 span，
    若只涂它起始 span 的 bbox，就只盖住了1/11 的号码——**漏脱敏**。
    故这里把"与该命中的全局偏移区间有交叠"的**所有** span 的 bbox 合并，
    得到一个覆盖完整命中的矩形。

    Args:
        hits: 命中列表（:class:`TextBoxHit`）。

    Returns:
        矩形列表，已按行合并。
    """
    if not hits:
        return []
    rects: List[Rect] = []
    for hit in hits:
        start = hit.match.start
        end = hit.match.end
        covering = [
            s for s in hit.covering_spans
            if s.offset < end and start < s.end
        ]
        if covering:
            rects.extend(merge_overlapping_rects([s.bbox for s in covering]))
        else:
            # 找不到覆盖 span 时退回 span 自身的 bbox（不应发生，属防御）
            rects.append(hit.span_rect_safe)
    return merge_overlapping_rects(rects)


def placeholder_anchor(hit: "TextBoxHit") -> Rect:
    """占位符的**写入起点**——落在命中子串的实际位置上。

    为什么不能用 :func:`rects_for_hits` 的合并矩形当写入框：
        整行只有 1 个 span 时（实测真实 PDF 里很常见，如
        ``'重复出现：13800138000 与13800138000'``），两个手机号会命中
        **同一个 span**，合并出的矩形相同 —— 于是两个占位符写到
        **同一位置**，视觉上只剩一个（实测渲染图确认）。

    折中做法：矩形仍按 span 全宽涂（宁多勿少，保证删净），
    但占位符按**子串在 span 内的字符比例**定位，使其落在原位置附近。
    """
    span = hit.span
    n = len(span.text) or 1
    frac0 = hit.start_in_span / n
    span_width = span.bbox.x1 - span.bbox.x0
    x0 = span.bbox.x0 + span_width * frac0
    # 至少留出与占位符等宽的空间，避免贴到行尾
    return Rect(x0, span.bbox.y0, span.bbox.x1 + span_width * 0.5, span.bbox.y1)


def write_placeholder(
    page: "fitz.Page",
    rect: Rect,
    text: str,
    *,
    page_rect: Optional[Rect] = None,
) -> bool:
    """在指定矩形内写入占位符，**自动扩框直到放得下**。

    **必须检查 :meth:`Page.insert_textbox` 的返回值** —— 实测它在放不下时
    **不抛异常**，只返回负数（剩余空间不足量），静默不写。开发时因此
    出现过"占位符 0 处"的假成功。

    实测数据（11pt 宽的原框 + ``[甲方单位名称]``）：
        原尺寸      -> -83.41放不下
        加宽 2x     -> -36.30   仍放不下
        加宽加高    ->   8.03   成功

    Args:
        page: 目标页面。
        rect: 期望写入的位置。
        text: 占位符文本。
        page_rect: 页面边界，用于裁剪避免写���页面之外。

    Returns:
        True 表示写入成功；False 表示所有尝试都失败（**调用方必须处理**）。
    """
    if not text:
        return False
    bounds = page_rect if page_rect is not None else page.rect
    width = max(rect.x1 - rect.x0, 1.0)
    height = max(rect.y1 - rect.y0, 1.0)

    # 由窄到宽、由大到小逐级尝试：先保持原样式，再逐步放宽。
    attempts = [
        (0.0, 0.0, 0.0),# 1) 原尺寸
        (width * 2, 0.0, 0.0),         # 2) 加宽 2 倍
        (width * 4, height * 0.5, 0.0), # 3) 加宽加高
        (width * 6, height * 1.5, 0.0), # 4) 再放宽
    ]
    for grow_x, grow_y, _ in attempts:
        box = Rect(
            rect.x0,
            max(bounds.y0, rect.y0 - grow_y),
            rect.x1 + grow_x,
            min(bounds.y1, rect.y1 + grow_y),
        )
        box = box & bounds
        if box.is_empty:
            continue
        # 返回值 < 0 表示剩余空间不足（放不下）；>= 0 表示写入的字符数
        written = page.insert_textbox(box, text, fontsize=9, fontname=CJK_FONT)
        if written >= 0:
            return True
    return False


__all__ = [
    "Rect",
    "SpanInfo",
    "TextBoxHit",
    "extract_spans",
    "page_has_text_layer",
    "iter_pages",
    "offset_to_span",
    "merge_overlapping_rects",
    "rects_for_hits",
    "placeholder_anchor",
    "write_placeholder",
    "CJK_FONT",
]
