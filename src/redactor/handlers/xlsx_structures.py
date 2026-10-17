"""xlsx 结构清洗：7 类容器遍历与清洗。

**唯一入口**：:func:`scrub_structures`。

与 docx 的关系（**别被"xlsx 比 docx 简单"误导**）::

    docx 的难点                xlsx 的对应情况
    ------------------------  ----------------------------------------
    敏感串拆进多个 <w:r>      ❌ 不存在（单元格值是完整字符串）
    改写要处理 run 拆分/合并   ❌ 不存在（直接赋 cell.value）
    样式保真                 ✅ 无样式概念
    嵌套表格递归              ❌ 不存在
    ------------------------  ----------------------------------------
    但 xlsx 有 7 类**同样容易漏**的容器：
      1. 内联字符串 / 共享字符串（**两种底层形态**）
      2. 数值（**≤15 位可清 / >15 位精度已丢**）
      3. 公式（**字符串常量含敏感串**；共享/数组公式不可强改）
      4. 批注（**正文与作者是两个独立字段**）
      5. 页眉页脚（**没有独立 zip 条目**，写在 sheet XML 里，18 个字段）
      6. 图表（``<a:t>`` 节点，含标题/系列名/轴标题/数据标签/图例）
      7. 工作簿属性 + 定义名称

实测依据（勿凭直觉改）：
    ``.verify/probe_xlsx/probe_xlsx.txt``（Q1–Q8）
    ``.verify/probe_xlsx/probe_xlsx2.txt``（Q1b/Q2b/Q2c/Q2d）

三条最容易踩的坑（都实测过）：

1. 🔴 **共享字符串表是否存在取决于写入方**，不是格式固有属性。
   openpyxl 写出的文件**没有** ``sharedStrings.xml``，全是 ``t="inlineStr"``；
   而 Excel 原生保存会写共享串表。清洗必须**两种形态都覆盖**，
   否则处理 Excel 原生文件时会整片漏脱敏。
   → 本模块走 openpyxl 高层 API（``cell.value`` 赋值），
   实测写回时命中格会变成 ``inlineStr``，**天然绕开引用图**（裁定 A1）。

2. 🔴 **数值型敏感串的分界线是 15 位有效数字**（IEEE754 + Excel 精度上限）。
   手机号 11 位：写盘是 ``<v>13800138000</v>``，``str()`` 后**能**匹配 → 必须清。
   身份证 18 位：写盘是 ``1.101011990030745e+17``，**匹配不到**，
   且精度已不可逆丢失 → **只告警不改**（裁定 B1）。

3. 🔴 **公式里的字符串常量含敏感串**
   （实测 ``=CONCATENATE("手机","13800138000")``、``=IF(A1="13800138000",1,2)``
   都能命中）→ 必须按公式字符串清洗。
   但**共享公式的引用格**与**数组公式**强改会破坏结构 → 跳过 + 告警（裁定 C1）。

安全红线（沿用 PDF P3b）：

    位置标签**绝不能带用户数据**。xlsx 里 sheet 名、单元格内容、
    批注内容、属性值**全都是用户数据**（实测过 sheet 名可以是手机号）。
    故标签统一用**序号**（``工作表 3!A1``），见 :func:`sheet_label`。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple

from redactor.core.engine import RedactionEngine
from redactor.core.models import Match

__all__ = [
    "STRUCTURE_LOCATION_INDEX",
    "XlsxScrubResult",
    "chart_label",
    "comment_label",
    "defined_name_label",
    "headerfooter_label",
    "property_label",
    "scrub_chart_parts",
    "scrub_structures",
    "sheet_label",
    "workbook_property_label",
]

#: 工作簿级载体（属性/定义名称/图表）的位置键页下标占位。
#:
#: 与 PDF P3b 的 ``STRUCTURE_PAGE_INDEX = -1`` 同一思路：这些载体不属于
#: 任何一个工作表，用 -1 与"单元格级"（用sheet 序号）区分开，
#: 避免两处都从 0 编号造成撞键、后写的标签覆盖先写的、报告少报。
STRUCTURE_LOCATION_INDEX = -1

#: 数值型敏感串的**有效数字位数上限**。
#:
#: 实测：``1.3800138e10``（11 位）写盘是 ``<v>13800138000</v>``，
#: ``str()`` 后能匹配手机号规则；而 ``1.101011990030745e+17``（18 位）
#: 写盘就是科学计数法，**匹配不到**，且精度已不可逆丢失。
#: 分界线取 15 位（IEEE754 双精度 + Excel 精度上限的共同约束）。
MAX_SIGNIFICANT_DIGITS = 15

#: 图表 XML 里所有文本节点（标题/系列名/轴标题/数据标签/图例都在这里）
_CHART_TEXT = re.compile(rb"<a:t>(.*?)</a:t>", re.S)
#: 批注 XML 的两个独立字段
_COMMENT_AUTHORS = re.compile(
    rb"<authors>(.*?)</authors>", re.S
)
_AUTHOR_ITEM = re.compile(rb"<author>(.*?)</author>", re.S)
_COMMENT_TEXT = re.compile(
    rb'<comment\b[^>]*>\s*<text>(.*?)</text>', re.S
)
_T_NODE = re.compile(rb"<t(?:\s[^>]*)?>(.*?)</t>", re.S)
_XML_ESC = re.compile(r"&(?:amp|lt|gt|quot|apos);")
_XML_ENT = {
    b"&amp;": b"&",
    b"&lt;": b"<",
    b"&gt;": b">",
    b"&quot;": b'"',
    b"&apos;": b"'",
}


def _unescape(raw: bytes) -> str:
    """把 XML 实体还原成明文（5 个预定义实体 + 数值实体）。

    🔴 顺序敏感：``&amp;`` 必须**最后**替换，否则 ``&amp;lt;``
    会被误解成 ``&lt;``（实测这个坑）。
    """
    def _dec(m: "re.Match[bytes]", base: int) -> bytes:
        try:
            return chr(int(m.group(1), base)).encode("utf-8")
        except (ValueError, OverflowError):
            return m.group(0)

    s = raw.replace(b"\n", b" ").strip()
    s = re.sub(rb"&#x([0-9A-Fa-f]+);", lambda m: _dec(m, 16), s)
    s = re.sub(rb"&#(\d+);", lambda m: _dec(m, 10), s)
    for ent, rep in _XML_ENT.items():
        if ent == b"&amp;":
            continue
        s = s.replace(ent, rep)
    s = s.replace(b"&amp;", b"&")
    return s.decode("utf-8", "replace")


def _escape(text: str) -> bytes:
    """把明文转义成 XML 文本节点内容（顺序敏感：先 &）。"""
    out = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return out.encode("utf-8")


# ------------------------------------------------------------------ 位置标签
# 🔴 安全红线：标签**不含任何用户数据**。sheet 名尤其危险 ——
#    用户完全可能把手机号写进工作表名。故一律用序号。
def sheet_label(seq: int, coord: str, *, hidden: bool = False,
                 merge: str = "", formula: bool = False) -> str:
    """单元格的位置标签，如 ``工作表 3!A1`` / ``工作表 3（隐藏）!B1``。

    Args:
        seq: 工作表序号（从 1 开始）。
        coord: 单元格坐标（如 ``A1``）。**坐标不是用户数据**，可安全展示。
        hidden: 该表是否为隐藏/veryHidden。
        merge: 合并区描述（如 ``B1:D1``），为空表示不合并。
        formula: 该格是否为公式。

    Returns:
        紧凑标签。刻意用 ``!`` 连接——这是 Excel 的通用记法，
        比"第 3 个工作表第 1 行"短得多，几千单元格时报告不会臃肿。
    """
    tag = f"工作表 {seq}（隐藏）" if hidden else f"工作表 {seq}"
    base = f"{tag}!{coord}"
    extra = []
    if merge:
        extra.append(f"含合并区 {merge}")
    if formula:
        extra.append("公式")
    return f"{base}（{'，'.join(extra)}）" if extra else base


def comment_label(sheet_seq: int, coord: str, *, field: str) -> str:
    """批注的位置标签。

    Args:
        sheet_seq: 工作表序号。
        coord: 单元格坐标。
        field: ``正文`` 或 ``作者`` —— **固定词**，不传用户数据。
    """
    return f"工作表 {sheet_seq}!{coord} 的批注{field}"


def headerfooter_label(sheet_seq: int, *, kind: str, part: str) -> str:
    """页眉页脚的位置标签，如 ``工作表 1 页眉 居中``。"""
    return f"工作表 {sheet_seq} {kind}{part}"


def chart_label(chart_index: int, part: str) -> str:
    """图表文本节点的位置标签，如 ``图表1 标题``。

    Args:
        chart_index: 图表序号（从 1 开始，按 ``xl/charts/chartN.xml`` 排序）。
        part: 节点类型描述（固定词，如 ``标题`` / ``文本``）。
    """
    return f"图表{chart_index} {part}"


def property_label(field: str) -> str:
    """工作簿属性的位置标签，如 ``工作簿 属性 标题``。

    Args:
        field: **属性名**（如 ``标题``），不是属性值。
    """
    return f"工作簿 属性 {field}"


#: openpyxl 属性名 -> 中文标签词（固定词表，**不是用户数据**）
PROPERTY_LABELS = {
    "title": "标题",
    "creator": "作者",
    "description": "描述",
    "subject": "主题",
    "keywords": "关键词",
    "category": "类别",
    "lastModifiedBy": "最后修改者",
    "company": "公司",
    "manager": "经理",
}


def workbook_property_label(field: str) -> str:
    """兼容别名，与 :func:`property_label` 等价。"""
    return property_label(field)


def defined_name_label() -> str:
    """定义名称的位置标签。

    🔴 **刻意不带名称本身** —— 定义名称可能是 ``客户_张三`` 这类用户数据。
    """
    return "工作簿 定义名称"


# ------------------------------------------------------------------ 结果
class XlsxScrubResult(NamedTuple):
    """一次结构清洗的结果。

    Attributes:
        matches: 全部生效的命中。``start`` 是**全局递增序号**
            （单元格坐标不是数字，全局序号才能当位置键的第三元）。
        labels: 与 :attr:`matches` **一一对应**的位置标签。
        warnings: 需要人工核查的提示（超精度数值、共享/数组公式等）。
        parts_touched: 各类容器被改写的计数，如 ``{'单元格': 3}``。
    """

    matches: List[Match]
    labels: List[str]
    warnings: List[str]
    parts_touched: Dict[str, int]


class _Collector:
    """收集命中并统一编号。

    为什么要收集器而不是各处自己编号：位置键是
    ``(规则名, 载体类别, 序号)``，七类容器若各自从 0 开始就会**撞键**，
    后写的标签覆盖先写的、报告少报（与 PDF P3b 同型问题）。

    ``start`` 用**全局递增序号**而非文本偏移：xlsx 的每个单元格值都是
    独立字符串，"偏移"只在单个字符串内有意义，跨容器不可比。
    """

    def __init__(self, engine: RedactionEngine, *, start_seq: int = 0) -> None:
        self._engine = engine
        self._seq = start_seq
        self.matches: List[Match] = []
        self.labels: List[str] = []
        self.warnings: List[str] = []
        self.parts: Dict[str, int] = {}

    def add(self, matches: List[Match], label: str) -> int:
        """登记一批命中（用同一标签）。返回登记数。"""
        n = 0
        for m in matches:
            self.matches.append(
                Match(
                    rule_name=m.rule_name,
                    placeholder=(
                        m.placeholder
                        or self._engine.render_placeholder(m)
                    ),
                    start=self._seq,
                    end=self._seq + 1,
                    raw=m.raw,
                    page_index=STRUCTURE_LOCATION_INDEX,
                )
            )
            self.labels.append(label)
            self._seq += 1
            n += 1
        return n

    def bump(self, part: str, n: int = 1) -> None:
        """累加某一类容器的改写计数。"""
        if n:
            self.parts[part] = self.parts.get(part, 0) + n


# ------------------------------------------------------------------ 数值
def _significant_digits(value: Any) -> int:
    """数值的**有效数字位数**（判定"精度是否已不可逆丢失"）。

    🔴 **不能用 ``repr(float(v))`` 计数** —— 实测会错：
        ``repr(13800138000.0)`` 是 ``'13800138000.0'``，去掉小数点和**尾零**
        后只剩 8 位，而真值是 11 位；``1.101011990030745e17`` 更会算出 33 位。
        这种错会让"超精度应告警"的判据**失效**（11 位手机号被误判成 8 位，
        18 位身份证反而通过）→ 静默漏脱敏。

    正确做法：``int`` 走整数位数；``float`` 走 ``%.17g`` 后剥掉
    指数与小数点，再去**首尾**零（不是只去尾零）。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return len(str(abs(value))) if value else 1
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 1
    if f == 0:
        return 1
    if f.is_integer():
        # 用整数形式取位数，避免 repr 的小数尾零问题
        return len(str(abs(int(f))))
    s = f"{f:.17g}"
    mantissa, _, exp = s.partition("e")
    digits = mantissa.replace("-", "").replace(".", "").strip("0")
    if not digits:
        return 1
    return len(digits) + (int(exp) if exp else 0)


def _sci_candidates(text: str) -> List[str]:
    """把科学计数法串还原成**一组候选**整数串（供引擎逐个试）。

    🔴🔴 **不能只还原成一个"最像的"值** —— 这是本轮实测踩到的坑：
        ``1.101011990030745e+17`` 精确还原得
        ``110101199003074500``（末尾补 0），而身份证校验位**在末位**
        → 校验失败 → **命中 0** → 告警不触发。
        也就是说"最该告警的场景"被算法本身消音了。

        正确做法：IEEE754 已经丢精度，**末几位本来就是错的**，
        所以必须**枚举**末位被改动的情况：把尾数按指数左移得到
        全长数字串，然后依次尝试"原样"、"末 1 位替换为 0~9"、
        "末 2 位替换为 00~99 中最接近的"，让**引擎自己**判。
        **不自造校验逻辑** —— 判据与实现共用同一个引擎，避免互相掩盖。
    """
    m = re.match(r"^-?(\d)\.(\d+)[eE]\+?(\d+)$", text.strip())
    if not m:
        return []
    lead, frac, exp = m.group(1), m.group(2), int(m.group(3))
    digits = lead + frac
    shift = exp - len(frac)
    if shift <= 0:
        keep = shift if shift > 0 else len(digits)
        out = digits[:keep] or digits[:1]
    else:
        out = digits + "0" * shift
    if not out:
        return []

    cands: List[str] = [out]
    # 末 1 位可能因精度丢失而错：枚举 0-9
    if len(out) >= 2:
        head = out[:-1]
        cands.extend(f"{head}{d}" for d in "0123456789")
    return cands


def _format_number_like(cell: Any) -> str:
    """把数值单元格还原成**用户看到的**样子，再交给引擎。

    为什么不能直接 ``str(value)``：Excel 会按 ``number_format`` 呈现。
    实测把手机号当数字存时 ``number_format='0'``，用户看到的是
    ``13900139000``；但若格式是 ``General`` 且值很大，Excel 会显示
    科学计数法。首版只按 ``str(value)`` 走（覆盖绝大多数场景），
    并对**超精度**情形告警（见 :func:`_scrub_cells`）。
    """
    return str(cell.value)


# ------------------------------------------------------------------ 1. 单元格
def _iter_cells(ws: Any) -> Iterator[Any]:
    """遍历工作表内所有**有值**的单元格（含公式格的公式文本）。"""
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            yield cell


def _merge_range_of(ws: Any, cell: Any) -> str:
    """若该格落在合并区内，返回 ``B1:D1`` 这样的描述，否则空串。"""
    try:
        for rng in ws.merged_cells.ranges:
            if (
                rng.min_row <= cell.row <= rng.max_row
                and rng.min_col <= cell.column <= rng.max_col
            ):
                return str(rng)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _scrub_cells(
    wb: Any, engine: RedactionEngine, col: _Collector
) -> None:
    """容器 1+2+3：单元格（字符串 / 数值 / 公式）。

    🔴 **必须遍历全部 sheet**：``load_workbook()`` 默认包含
    ``hidden`` 与 ``veryHidden``，实测隐藏页确实有脏数据。
    而用户在 Excel 里默认**看不见**隐藏页 → 不清就是漏脱敏，
    且用户不会去查。标签里标"（隐藏）"提示他去看。
    """
    for sheet_seq, ws in enumerate(wb.worksheets, start=1):
        hidden = getattr(ws, "sheet_state", "visible") != "visible"
        for cell in _iter_cells(ws):
            value = cell.value

            # --- 容器 3：公式 ---
            if cell.data_type == "f" or (
                isinstance(value, str) and value.startswith("=")
            ):
                _scrub_formula_cell(
                    ws, sheet_seq, cell, hidden, engine, col
                )
                continue

            # --- 数组公式：value 不是字符串，强转会坏 ---
            if value.__class__.__name__ == "ArrayFormula":
                col.warnings.append(
                    f"工作表 {sheet_seq}!{cell.coordinate} 是数组公式，"
                    f"**未修改**，请人工核查该公式是否含敏感信息"
                )
                continue

            # --- 容器 2：数值 ---
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                _scrub_number_cell(
                    ws, sheet_seq, cell, hidden, engine, col
                )
                continue

            # --- 容器 1：字符串（含 inlineStr 与共享字符串）---
            if not isinstance(value, str):
                continue
            new_text, matches = engine.redact(value)
            if not matches:
                continue
            cell.value = new_text
            label = sheet_label(
                sheet_seq,
                cell.coordinate,
                hidden=hidden,
                merge=_merge_range_of(ws, cell),
            )
            col.add(matches, label)
            col.bump("单元格", len(matches))


def _scrub_number_cell(
    ws: Any, sheet_seq: int, cell: Any, hidden: bool,
    engine: RedactionEngine, col: _Collector,
) -> None:
    """容器 2：数值型单元格。

    分两条线（实测结论，勿改）：
      - ≤15 位有效数字 → ``str()`` 后**能**匹配 → 清洗
      - >15 位 → 写盘即科学计数法，**匹配不到**且精度已不可逆丢失
        → **只告警不改**（裁定 B1）。改了等于给用户一个假的干净结果。

    🔴 **告警不能挂在"是否命中"的分支之后**（第一版的 bug）：
        超精度数值的 ``str()`` 是 ``'1.101011990030745e+17'``，
        **正则匹配不到任何规则** → 若先写 ``if not matches: return``，
        后面"超精度告警"的分支**永远走不到** ——
        恰好是**最该告警的场景**（精度丢失导致漏检）静默通过。
        故这里改成：**先判超精度并告警**，再判是否命中。
        但"是否含敏感信息"无法从科学计数法串直接判断，
        故额外检查**去掉指数部分**后的尾数是否像身份证/手机号。
    """
    text = _format_number_like(cell)
    digits = _significant_digits(cell.value)

    if digits > MAX_SIGNIFICANT_DIGITS:
        # 科学计数法形态下规则匹配不到，必须把尾数还原成整数串再判。
        # 🔴 **用引擎自己判**（不自造正则）—— 否则"判据"与"实现"两套
        #    规则各写一遍，会互相掩盖：实现改了判据没改 / 反之亦然。
        # 🔴 且必须**枚举候选**：IEEE754 丢精度时末位就是错的，
        #    单一还原值会因校验位不符而漏判（见 _sci_candidates 说明）。
        suspicious = any(
            engine.scan(c) for c in _sci_candidates(text)
        )
        if suspicious:
            col.warnings.append(
                f"工作表 {sheet_seq}!{cell.coordinate} 是 {digits} 位数值"
                f"（超过 {MAX_SIGNIFICANT_DIGITS} 位精度），"
                f"其内容疑似敏感信息，但精度已不可逆丢失，"
                f"**无法自动脱敏**，请人工核查"
            )
        return

    new_text, matches = engine.redact(text)
    if not matches:
        return

    # 该格从此变成文本 —— 显式告警，因为它会影响用户后续的 Excel 计算
    cell.value = new_text
    label = sheet_label(
        sheet_seq, cell.coordinate, hidden=hidden,
        merge=_merge_range_of(ws, cell),
    )
    col.add(matches, label)
    col.bump("数值单元格", len(matches))
    col.warnings.append(
        f"工作表 {sheet_seq}!{cell.coordinate} 原为数值型，"
        f"已替换为文本占位符，该单元格类型由数字变为文本"
    )


def _scrub_formula_cell(
    ws: Any, sheet_seq: int, cell: Any, hidden: bool,
    engine: RedactionEngine, col: _Collector,
) -> None:
    """容器 3：公式单元格。

    公式里的**字符串常量**确实含敏感串（实测
    ``=CONCATENATE("手机","13800138000")``、``=IF(A1="13800138000",1,2)``
    都能命中）→ 必须按公式字符串清洗。

    一类**不可强改**的情形，必须显式告警：
      - **数组公式**：``value`` 是对象不是字符串 → 跳过。

    关于"共享公式的引用格"（``<f t="shared" si="N"/>`` 无 ``ref=``）：
    **openpyxl 默认会把它们展开为独立公式**（实测：build_sample 输出里
    出现 ``t="shared"`` 次数=0，引用格被翻译成具体公式表达式），
    因此本模块**不再特判引用格** —— 引用格与普通公式走同一清洗路径，
    不告警、不跳过。
    """
    value = cell.value

    if value.__class__.__name__ == "ArrayFormula":
        col.warnings.append(
            f"工作表 {sheet_seq}!{cell.coordinate} 是数组公式，"
            f"**未修改**，请人工核查该公式是否含敏感信息"
        )
        return

    if not isinstance(value, str):
        return

    new_formula, matches = engine.redact(value)
    if not matches:
        return
    cell.value = new_formula
    label = sheet_label(
        sheet_seq, cell.coordinate, hidden=hidden, formula=True,
        merge=_merge_range_of(ws, cell),
    )
    col.add(matches, label)
    col.bump("公式", len(matches))


# ------------------------------------------------------------------ 4. 批注
def _scrub_comments(
    wb: Any, engine: RedactionEngine, col: _Collector, *, dry_run: bool
) -> None:
    """容器 4：批注。

    🔴 **两个独立字段都要清**（实测 ``xl/comments/comment1.xml`` 里）：
        - 正文 ``<comment><text><t>…</t></text></comment>``
        - 作者 ``<authors><author>张三</author></authors>``
      实测正文含 ``批注作者 13800138000\\n邮箱 zhang.san@example.com``。
      作者字段实测是中文姓名（内置规则不命中），但**用户词库可能配了
      姓名规则** → 仍必须跑引擎。

    为什么用 openpyxl 高层 API（``cell.comment``）而不是改 XML：
    实测高层 API 写入后会重排 zip 条目（``xl/drawings/commentsDrawing1.vml``
    可能消失），而那个 VML 里**没有文本**（实测），所以丢它不影响脱敏。
    """
    for sheet_seq, ws in enumerate(wb.worksheets, start=1):
        hidden = getattr(ws, "sheet_state", "visible") != "visible"
        for row in ws.iter_rows():
            for cell in row:
                comment = cell.comment
                if comment is None:
                    continue

                # --- 正文 ---
                body = comment.text or ""
                if body:
                    new_body, matches = engine.redact(body)
                    if matches:
                        if not dry_run:
                            comment.text = new_body
                        label = comment_label(
                            sheet_seq, cell.coordinate, field="正文"
                        )
                        col.add(matches, label)
                        col.bump("批注", len(matches))

                # --- 作者 ---
                author = comment.author or ""
                if author:
                    new_author, a_matches = engine.redact(author)
                    if a_matches:
                        if not dry_run:
                            comment.author = new_author
                        label = comment_label(
                            sheet_seq, cell.coordinate, field="作者"
                        )
                        col.add(a_matches, label)
                        col.bump("批注", len(a_matches))
                _ = hidden  # 批注标签暂不标隐藏（避免标签臃肿），正文单元格会标


# ------------------------------------------------------------------ 5. 页眉页脚
#: 6 种（odd/even/first × header/footer）× 3 个位置（left/center/right）
_HF_KINDS = (
    ("oddHeader", "页眉"),
    ("oddFooter", "页脚"),
    ("evenHeader", "偶数页页眉"),
    ("evenFooter", "偶数页页脚"),
    ("firstHeader", "首页页眉"),
    ("firstFooter", "首页页脚"),
)
_HF_PARTS = (("left", "左"), ("center", "居中"), ("right", "右"))


def _scrub_header_footer(
    wb: Any, engine: RedactionEngine, col: _Collector, *, dry_run: bool
) -> None:
    """容器 5：页眉页脚（**18 个字段**）。

    🔴 xlsx 的页眉页脚**没有独立 zip 条目**，写在**每个 sheet 的 XML** 的
    ``<headerFooter>`` 里（实测）—— 所以"遍历 sheet XML"才是对的做法。

    🔴 格式码（``&C`` 居中 / ``&L`` 左 / ``&R`` 右 / ``&P`` 页码 /
    ``&D`` 日期）是 Excel 控制码，**不能碰**。实测 openpyxl 的
    ``.text`` 属性**已剥离格式码**（读出 ``表头 13800138000`` 而非
    ``&C表头 13800138000``），故直接改 ``.text`` 即可，库会保留格式码。
    """
    for sheet_seq, ws in enumerate(wb.worksheets, start=1):
        for attr, kind_cn in _HF_KINDS:
            try:
                container = getattr(ws, attr)
            except Exception:  # noqa: BLE001
                continue
            for part_attr, part_cn in _HF_PARTS:
                try:
                    part = getattr(container, part_attr)
                    text = part.text
                except Exception:  # noqa: BLE001
                    continue
                if not text:
                    continue
                new_text, matches = engine.redact(text)
                if not matches:
                    continue
                if not dry_run:
                    part.text = new_text
                label = headerfooter_label(
                    sheet_seq, kind=kind_cn, part=part_cn
                )
                col.add(matches, label)
                col.bump("页眉页脚", len(matches))


# ------------------------------------------------------------------ 6. 图表
def scrub_chart_parts(
    path: Path,
    engine: RedactionEngine,
    *,
    start_seq: int = 0,
) -> XlsxScrubResult:
    """容器 6 独立入口：清洗**已落盘文件**里的图表文本节点。

    裁定 E1：**扫全部 ``<a:t>``**。实测标题在
    ``<a:t>图表标题 13800138000</a:t>``；系列名/坐标轴标题/
    数据标签/图例项**都在同一个文件的 ``<a:t>`` 里** —— 按标签类型
    精确分类成本高且易漏，而全扫成本低、无破坏风险。

    Args:
        path: **已保存**的 xlsx 路径。必须已落盘，因为 ``openpyxl``
            把图表重建成对象树，内存里拿不到 ``chart*.xml`` 的原始字节。
        engine: 脱敏引擎。
        start_seq: 命中序号起始值（避免与其它载体撞键）。

    Returns:
        :class:`XlsxScrubResult`。文件里没有图表时返回空结果且**不告警**
        （"没有图表"不是风险）。
    """
    col = _Collector(engine, start_seq=start_seq)
    try:
        with zipfile.ZipFile(str(path)) as z:
            names = [
                n for n in z.namelist()
                if n.startswith("xl/charts/chart") and n.endswith(".xml")
            ]
            if not names:
                return XlsxScrubResult([], [], [], {})
            items = {n: z.read(n) for n in z.namelist()}
    except Exception as exc:  # noqa: BLE001
        col.warnings.append(
            f"图表部分读取失败（{type(exc).__name__}），**未处理**，"
            f"请人工核查图表标题与坐标轴标题"
        )
        return XlsxScrubResult(col.matches, col.labels, col.warnings, col.parts)

    changed = False
    chart_seq = 0
    for name in sorted(names):
        blob = items[name]
        chart_seq += 1

        def _repl(m: "re.Match[bytes]", _seq=chart_seq) -> bytes:
            nonlocal changed
            text = _unescape(m.group(1))
            new_text, matches = engine.redact(text)
            if not matches:
                return m.group(0)
            changed = True
            col.add(matches, chart_label(_seq, "文本"))
            col.bump("图表", len(matches))
            return b"<a:t>" + _escape(new_text) + b"</a:t>"

        new_blob = _CHART_TEXT.sub(_repl, blob)
        if new_blob != blob:
            items[name] = new_blob

    if changed:
        try:
            tmp = Path(str(path) + ".charttmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for n, b in items.items():
                    zout.writestr(n, b)
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            col.warnings.append(
                f"图表写回失败（{type(exc).__name__}），**未清洗**，"
                f"请人工核查图表标题"
            )

    return XlsxScrubResult(
        matches=col.matches,
        labels=col.labels,
        warnings=col.warnings,
        parts_touched=col.parts,
    )


# ------------------------------------------------------------------ 7. 属性 + 定义名称
def _scrub_properties(
    wb: Any, engine: RedactionEngine, col: _Collector, *, dry_run: bool
) -> None:
    """容器 7a：工作簿属性（裁定 F1）。

    实测确实含敏感串：``properties.title='工作簿标题 13800138000'``、
    ``properties.creator='作者 zhang.san@example.com'``。
    与 PDF P3 的 ``/Info`` 清洗同型。
    """
    props = getattr(wb, "properties", None)
    if props is None:
        return
    for attr, cn in PROPERTY_LABELS.items():
        raw = getattr(props, attr, None)
        if not raw or not isinstance(raw, str):
            continue
        new_value, matches = engine.redact(raw)
        if not matches:
            continue
        if not dry_run:
            setattr(props, attr, new_value)
        col.add(matches, property_label(cn))
        col.bump("工作簿属性", len(matches))


def _scrub_defined_names(
    wb: Any, engine: RedactionEngine, col: _Collector, *, dry_run: bool
) -> None:
    """容器 7b：定义名称（裁定 G1）。

    实测定义名称存在（``['敏感名称']``）。招采场景里客户名单/供应商名
    常落在定义名称上，故必须跑引擎。
    """
    names = getattr(wb, "defined_names", None)
    if names is None:
        return
    try:
        items = list(names.items())
    except Exception:  # noqa: BLE001
        return
    for key, dn in items:
        raw = getattr(dn, "name", None) or str(key)
        new_name, matches = engine.redact(raw)
        if not matches:
            continue
        if dry_run:
            col.add(matches, defined_name_label())
            col.bump("定义名称", len(matches))
            continue
        try:
            # 改名的正确做法：删旧增新（openpyxl 的 DefinedNameDict
            # 支持直接赋值新键，值需重建）
            from openpyxl.workbook.defined_name import DefinedName

            names.pop(key, None)
            names[new_name] = DefinedName(new_name, attr_text=dn.attr_text)
            col.add(matches, defined_name_label())
            col.bump("定义名称", len(matches))
        except Exception as exc:  # noqa: BLE001
            col.warnings.append(
                f"定义名称改名失败（{type(exc).__name__}），**未清洗**，"
                f"请人工核查"
            )


# ------------------------------------------------------------------ 入口
def scrub_structures(
    wb: Any,
    engine: RedactionEngine,
    *,
    start_seq: int = 0,
    dry_run: bool = False,
) -> XlsxScrubResult:
    """清洗 xlsx 的 6 类**内存态**容器（单元格/批注/页眉页脚/属性/定义名称）。

    Args:
        wb: :class:`openpyxl.Workbook`（**会被就地修改**）。
        engine: 脱敏引擎。
        start_seq: 命中序号的起始值（供与其它载体续号，避免撞键）。
        dry_run: 只统计不写。

    Returns:
        :class:`XlsxScrubResult`。

    Note:
        **图表不在这里处理** —— 它必须读磁盘上的 ``chart*.xml``，
        只能在 handler 完成最后一次 ``save()`` 之后做，
        见 :func:`scrub_chart_parts`。混进来会导致"改了又被覆盖"。
    """
    col = _Collector(engine, start_seq=start_seq)

    _scrub_cells(wb, engine, col)
    _scrub_comments(wb, engine, col, dry_run=dry_run)
    _scrub_header_footer(wb, engine, col, dry_run=dry_run)
    _scrub_properties(wb, engine, col, dry_run=dry_run)
    _scrub_defined_names(wb, engine, col, dry_run=dry_run)

    return XlsxScrubResult(
        matches=col.matches,
        labels=col.labels,
        warnings=col.warnings,
        parts_touched=col.parts,
    )
