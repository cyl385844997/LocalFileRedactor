"""PDF 结构清洗（P3b）：另 5 处藏身处 + 页面标签。

为什么需要这一层
================

P3 只覆盖了 ``/Info`` 与 XMP 两处**元数据**。但 PDF 里还有一批**结构化载体**
同样会携带敏感串，且**不在任何文本层**——正文涂干净了，它们仍在：

1. **书签 / 大纲**（``/Outlines`` 的 ``/Title``）
2. **注释 / 批注**（``/Contents`` ``/T`` ``/Subj`` + 外观流 ``/AP``）
3. **链接**（URI 类的 ``/A/URI``；LAUNCH 类的 ``/A/F`` ``/A/UF``）
4. **内嵌文件**（``/Names/EmbeddedFiles`` 的文件名与描述）
5. **表单域**（``/T`` ``/V`` ``/TU`` + 外观流 ``/AP``）
6. （顺带）**页面标签**（``/PageLabels`` 的 ``/P``）

实测这些位置**确实含敏感串**（见 ``.verify/probe_p3b/``），
且**L3（:mod:`redactor.handlers.pdf_verify`）对其中 5 处是盲区**——
它只搜内容流，而 ``/Title`` ``/Contents`` ``/URI`` ``/Names`` ``/PageLabels``
都**不是流**。所以不能指望 L3 兜住它们。

🔴 六条实测结论（勿凭直觉改）
============================

1. **同一种"文本"在 PDF 里有三种编码形态**

   - 十六进制 UTF-16BE（``<FEFF...>``）：书签 ``/Title``、注释 ``/Contents``、
     表单 ``/TU``、内嵌文件名、LAUNCH ``/F``/``/UF``
   - 明文（``(...)``）：链接 ``/URI``、表单 ``/T``/``/V``
   - 十六进制 UTF-8（``<E7ACAC...>``）：**仅**页面标签 ``/P``

   **只搜明文必然漏**（探针一第一版就踩了：书签从"未找到"变成"命中 xref=[10,11]"）。

2. **``xref_set_key`` 的 value 必须是 PDF 对象语法字符串**，不是裸文本。
   传裸串 → ``FzErrorSyntax: code=8: invalid key in dict``。

3. **``hex(BOM)`` 是唯一"全场景安全"的写入形态**

   ``"<FEFF" + text.encode("utf-16-be").hex().upper() + ">"``。
   实测矩阵（4 写入形态 × 5 类特殊内容）：
   ``裸串不转义`` 在反斜杠/圆括号下**静默丢字**；``无 BOM hex`` 中文**回读乱码**；
   ``hex(BOM)`` 与 ``(...)+自己转义`` 全场景安全。选 hex(BOM) 是因为
   **没有"忘记转义"的风险**——少一个人工环节 = 少一个 bug 面。

4. **``set_toc`` 会抹掉书签样式**（``/C`` 颜色、``/F`` 粗斜体）

   实测路径 A（``set_toc``）样式 **0/2**，对照组 **2/2**，
   权威判据 ``xref_get_key`` 复核后对象里**确实看不到** ``/C`` ``/F``。
   故书签**必须**用 ``xref_set_key(xref, "Title", ...)`` 逐条改，
   **不能用 ``set_toc``**。

5. **``update_link`` 与 ``delete_link+insert_link`` 都会静默丢反斜杠**

   实测 ``'https://x.com/A\\B'`` → 回读 ``'https://x.com/AB'``。
   **链接 URI 必须**用 ``xref_set_key(xref, "A/URI", ...)``。
   LAUNCH 类更严重：``update_link`` 会把 ``/S /Launch`` 改成 ``/S /GoToR``
   并做双重百分号编码 —— **结构被破坏**。

6. **``xref`` 跨 save 会重排**

   实测保存前 ``[14,15,16]`` → 保存后 ``[12,13,14]``（``garbage=0`` 时不变）。
   故**所有改写必须在同一次 open→改→save 内完成**，绝不跨 save 缓存 xref。

🔴 一条被推翻的旧结论（留痕，勿回退）
====================================

旧方案说"注释/表单域必须 ``update_stream`` 清 ``/AP``"，依据是
探针二 D1(b)：改 ``field_value`` + ``update()`` 后"仍含手机号的流 = [(7, ...)]"。

**这条是假阳性。** 重做探针（``.verify/probe_p3b/probe_p3b_ap.txt``）后：
``update()`` 会把 ``/AP`` 从旧对象（7/13）换成**新对象**（9/12），
旧对象在**内存里还没被回收**，扫"全部流"就扫到了那个**已不再被引用的孤儿**。
save + 重开后判定，三种路径对照：

    只改结构不 update()      -> 🔴 残留
    改结构 + update()        -> ✅ 干净
    改结构 + update() + 流兜底 -> ✅ 干净

> 与 E2d 的"xref 跨 save 重排"是**同一类失效**：拿"改之前的坐标"问"改之后的状态"。
> **铁律：判定残留必须 save + 重开，且用重开后重新取的 xref。**

因此本模块的 ``/AP`` 处理做成**检测式兜底**（:func:`_scrub_appearance_stream`）：
正常路径下扫一遍返回空、零副作用；``update()`` 未生效时才真替换。
保留它的理由：P1 实测证明"``update()`` 没生效时 ``/AP`` 确实残留"是
**已知的真实触发场景**（漏调 ``update()``、或 ``update()`` 抛异常被吞）。

流兜底还必须处理**两种形态**（实测 ``probe_p3b_stream.txt``）：

    FreeText 注释的 /AP  -> 十六进制字形 <...0031003300380030...>
                            天真替换（只换明文字节）**一个都换不到，且不报错**
    表单域的 /AP          -> 明文 (13800138000) Tj

报告约定（用户裁定 B1 + D）
==========================

- 位置标签**统一用序号，绝不带用户数据**（安全红线）：
  ``书签 第 1 条`` / ``第 1 页 注释 第 1 个`` / ``内嵌文件 第 1 个`` /
  ``页面标签 第 1 段``。
  🔴 反例：字段名可能是 ``phone_13800138000``、文件名可能是
  ``陈彦霖的联系.txt`` —— 写进标签 = 把敏感串写进报告。
- ``page_index = -1``（与 P3 元数据一致；结构命中只进 ``aggregate`` 的
  ``flat``，**不进** ``_redact``，故 ``-1`` 不会导致索引出错）。
- 序号**续 P3 的号**（``start_seq`` 由 handler 传入），
  否则两处都用 ``page_index=-1`` 且各自从 0 编号会撞键、报告少报。
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

import fitz  # PyMuPDF

from redactor.core.engine import RedactionEngine
from redactor.core.models import Match

__all__ = [
    "STRUCTURE_PAGE_INDEX",
    "StructureScrubResult",
    "bookmark_label",
    "embfile_label",
    "page_label_label",
    "page_object_label",
    "park_freetext_annots",
    "pdf_text_hex",
    "restore_freetext_annots",
    "scrub_structures",
]

#: 结构命中的 ``page_index``：``-1`` = 非正文位置（与 P3 元数据同约定）。
#:
#: 书签/内嵌文件/页面标签**本来就不属于任何页**；注释/链接/表单域虽属某页，
#: 但页码已写在标签文字里（``第 1 页 注释 第 1 个``），键里再放一次无收益。
STRUCTURE_PAGE_INDEX = -1

#: 内容流里的十六进制字符串。``<<``/``>>`` 是字典分隔符，不会被匹配。
_HEX_STRING = re.compile(rb"<([0-9A-Fa-f\s]+)>")

#: 内容流里的字面字符串 ``(...)``，允许 ``\`` 转义。
_LITERAL_STRING = re.compile(rb"\(((?:\\.|[^\\()])*)\)")

#: 尝试的字节编码（与 :data:`redactor.handlers.pdf_verify.ENCODINGS` 一致）。
_ENCODINGS: Tuple[str, ...] = ("utf-8", "gbk", "utf-16-be", "utf-16-le")

#: 书签对象层扫 ``/Title`` 时，判定"这是书签节点"的判据。
_BOOKMARK_TYPE = re.compile(r"/Type\s*/Outlines?\b")


# ------------------------------------------------------------------ 写入形态
def pdf_text_hex(text: str) -> str:
    """把文本转成 PDF 字符串的 **hex UTF-16BE（带 BOM）** 形态。

    这是全场景安全的写入形态（实测 E2c / E3b，见模块 docstring 结论 3）：

    - 反斜杠 ``\\``：不会丢字（明文形态会）
    - 圆括号 ``()``：不需要转义（hex 里全是十六进制字符）
    - 中文：带 BOM 才能正确回读（无 BOM 会乱码）
    - 纯 ASCII：也正确（实测 Q5）→ 无需按内容分支

    Args:
        text: 要写入的文本。

    Returns:
        形如 ``<FEFF...>`` 的 PDF 对象语法字符串，可直接交给
        :meth:`fitz.Document.xref_set_key`。

    Note:
        ``xref_set_key`` 的 value **必须是 PDF 对象语法**（含尖括号），
        不能传裸文本 —— 传裸串抛 ``FzErrorSyntax: code=8``。
    """
    return "<FEFF" + text.encode("utf-16-be").hex().upper() + ">"


def _decode_pdf_hex(raw: str) -> Optional[str]:
    """把 ``<...>`` 形态的 PDF 字符串解码成文本；失败返回 None。

    依次尝试 UTF-8 / UTF-16BE（带 BOM）/ 去 BOM 的 UTF-16BE。
    页面标签是 UTF-8，书签/注释是 UTF-16BE —— 两者都要能解。
    """
    s = raw.strip()
    if not (s.startswith("<") and s.endswith(">")):
        return None
    body = re.sub(r"\s+", "", s[1:-1])
    if not body or len(body) % 2:
        return None
    try:
        data = bytes.fromhex(body)
    except ValueError:
        return None
    if data.startswith(b"\xfe\xff"):
        try:
            return data[2:].decode("utf-16-be")
        except UnicodeDecodeError:
            return None
    for enc in ("utf-8", "utf-16-be"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _de_mojibake(name: str) -> str:
    """还原 PyMuPDF ``embfile_names()`` 返回的 mojibake 文件名。

    实测：``embfile_names()`` 返回 ``'é\\x99\\x88å½¦...txt'``，
    即"UTF-8 字节被按 latin-1 解码"的形态。还原 = 反向操作。

    Args:
        name: API 返回的原始名。

    Returns:
        可读文件名；无法还原时返回原值（**不抛异常**——文件名不是脱敏的
        关键路径，不该因它中断整个流程）。
    """
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


#: PDF 字面字符串里的八进制转义 ``\347``（1~3 位八进制数字）。
_OCT_ESCAPE = re.compile(r"\\([0-7]{1,3})")

#: 非八进制的 PDF 单字符转义。
_PDF_SIMPLE_ESCAPE = {
    "n": b"\n", "r": b"\r", "t": b"\t",
    "b": b"\b", "f": b"\f",
    "(": b"(", ")": b")", "\\": b"\\",
}


def _decode_pdf_text(raw: str) -> str:
    """把 PDF 文本还原成明文，**三种形态**都要能解。

    🔴 为什么不能只用 :func:`_decode_pdf_hex`（P3b 实测踩坑）：

    ``get_page_labels()`` 返回的 ``prefix`` 是 **八进制转义的字面串**，
    不是 ``<hex>``：实测 ``set_page_labels(prefix="第13800138000章-")``
    写盘后，``get_page_labels()`` 返回 ``'\\\\347\\\\254\\\\25413800138000\\\\347\\\\253\\\\240-'``
    （``\\347\\254\\254`` 正是「第」的 UTF-8 字节 ``E7 AC AC``）。
    ``_decode_pdf_hex`` 对这种形态返回 ``None``，旧实现走 ``or prefix``
    拿到**转义串本身**，引擎当然不命中 → 页面标签**完全未清洗**。

    三种形态：

    ====================  ==========================================
    形态                 来源
    ====================  ==========================================
    ``\\347\\254\\254``  八进制转义字面串（页面标签，实测）
    ``<E7ACAC>``        十六进制（另一类 PDF 的页面标签）
    纯明文              英文标签，或已是明文
    ====================  ==========================================

    Args:
        raw: 上述任一形态。

    Returns:
        明文。无法解码时返回**原值**（宁可原样传下去让引擎判空，
        也不要抛异常中断整条清洗链）。
    """
    if not raw:
        return raw
    if raw.startswith("<") and raw.endswith(">"):
        return _decode_pdf_hex(raw) or raw
    if "\\" not in raw:
        return raw
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        m = _OCT_ESCAPE.match(raw, i)
        if m:
            out.append(int(m.group(1), 8) & 0xFF)
            i = m.end()
            continue
        if raw[i] == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            out.extend(_PDF_SIMPLE_ESCAPE.get(nxt, nxt.encode("latin-1", "replace")))
            i += 2
            continue
        out.extend(raw[i].encode("utf-8"))
        i += 1
    for enc in ("utf-8", "gbk"):
        try:
            return out.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.decode("latin-1")


# ------------------------------------------------------------------ 位置标签
def bookmark_label(seq: int) -> str:
    """书签的位置标签，如 ``书签 第 1 条``。"""
    return f"书签 第 {seq} 条"


def page_object_label(kind: str, page_index: int, seq: int) -> str:
    """页内对象（注释/链接/表单域）的位置标签，如 ``第 1 页 注释 第 1 个``。

    Args:
        kind: ``注释`` / ``链接`` / ``表单域``。
        page_index: **0 起**的页下标（标签里会 +1 展示）。
        seq: 该类的序号（全文档递增）。

    Note:
        标签**不含**对象自身的任何内容（注释正文、字段名、URI）——
        那些都是用户数据（见模块 docstring 的"安全红线"）。
    """
    return f"第 {page_index + 1} 页 {kind} 第 {seq} 个"


def embfile_label(seq: int) -> str:
    """内嵌文件的位置标签，如 ``内嵌文件 第 1 个``。

    **刻意不带文件名**：实测文件名可能是 ``陈彦霖的联系.txt``。
    """
    return f"内嵌文件 第 {seq} 个"


def page_label_label(seq: int) -> str:
    """页面标签的位置标签，如 ``页面标签 第 1 段``。"""
    return f"页面标签 第 {seq} 段"


class StructureScrubResult(NamedTuple):
    """一次结构清洗的结果。

    Attributes:
        matches: 命中的 :class:`Match` 列表。``page_index`` 恒为
            :data:`STRUCTURE_PAGE_INDEX`；``start`` 是**全局递增序号**
            （结构位置没有"文本偏移"概念，但位置键
            ``(规则名, page_index, start)`` 需要 ``start`` 唯一）。
        labels: 与 ``matches`` **一一对应**的位置标签。
        warnings: 需要人工核查的提示（内嵌文件内容未清洗等）。
        parts_touched: 各类被改写的计数，如 ``{'书签': 2, '注释': 1}``。
    """

    matches: List[Match]
    labels: List[str]
    warnings: List[str]
    parts_touched: Dict[str, int]


# ------------------------------------------------------------------ 流替换
def _replace_plain(data: bytes, needle: str, repl: str) -> Tuple[bytes, int]:
    """替换流里的**明文字节**（多编码）。

    Args:
        data: 原始字节。
        needle: 待替换的敏感串。
        repl: 占位符。

    Returns:
        ``(新字节, 替换次数)``。
    """
    out = data
    n = 0
    for enc in _ENCODINGS:
        try:
            nb = needle.encode(enc)
            rb = repl.encode(enc)
        except UnicodeEncodeError:
            continue
        c = out.count(nb)
        if c:
            out = out.replace(nb, rb)
            n += c
    return out, n


def scrub_stream_text(
    data: bytes, engine: RedactionEngine
) -> Tuple[bytes, List[Match]]:
    """对一段内容流做**双形态**脱敏。

    两种形态都要处理（实测 ``probe_p3b_stream.txt``）：

    1. ``<hex>`` 段 —— 解码成字节后交给 ``engine.redact``，再重新编码为紧凑 hex。
       实测 FreeText 注释的 ``/AP`` 就是这种形态，只替换明文字节
       **一个都换不到，且不报错**。
    2. ``(...)`` 字面串 / 裸明文 —— 直接按多编码替换。

    Args:
        data: 流的解压后字节。
        engine: 脱敏引擎。

    Returns:
        ``(新字节, 命中列表)``。未命中时返回**原字节对象**（连编码都不动，
        避免无谓改写）。

    Note:
        与 L3（:func:`redactor.handlers.pdf_verify.decode_stream_strings`）
        同源判据：那里是"解码后搜"，这里是"解码后换"。
    """
    hits: List[Match] = []
    seq = 0

    def _redact_bytes(raw: bytes) -> Tuple[bytes, int]:
        """按多种编码尝试脱敏，返回 ``(新字节, 命中数)``。"""
        nonlocal seq
        total = 0
        out = raw
        for enc in _ENCODINGS:
            try:
                text = out.decode(enc)
            except UnicodeDecodeError:
                continue
            new_text, matches = engine.redact(text)
            if not matches:
                continue
            # 只在"解码-重编码"往返无损时才替换，避免破坏其他字节
            try:
                new_raw = new_text.encode(enc)
            except UnicodeEncodeError:
                continue
            out = new_raw
            for m in matches:
                hits.append(
                    Match(
                        rule_name=m.rule_name,
                        placeholder=m.placeholder,
                        start=seq,
                        end=seq + 1,
                        raw=m.raw,
                        page_index=STRUCTURE_PAGE_INDEX,
                    )
                )
                seq += 1
            total += len(matches)
        return out, total

    # --- 形态一：<hex> 段 ---
    def _do_hex(m: "re.Match") -> bytes:
        h = re.sub(rb"\s+", b"", m.group(1))
        if len(h) % 2:
            h = h[:-1]
        try:
            raw = bytes.fromhex(h.decode("ascii"))
        except Exception:
            return m.group(0)
        new_raw, cnt = _redact_bytes(raw)
        if not cnt:
            return m.group(0)
        return b"<" + new_raw.hex().encode("ascii") + b">"

    out = _HEX_STRING.sub(_do_hex, data)

    # --- 形态二：字面串 ---
    def _do_literal(m: "re.Match") -> bytes:
        new_raw, cnt = _redact_bytes(m.group(1))
        if not cnt:
            return m.group(0)
        return b"(" + new_raw + b")"

    out = _LITERAL_STRING.sub(_do_literal, out)

    # --- 形态三：裸明文兜底（非常规，但存在）---
    out, _ = _replace_plain_engine(out, engine, hits)

    if not hits:
        return data, []
    return out, hits


def _replace_plain_engine(
    data: bytes, engine: RedactionEngine, hits: List[Match]
) -> Tuple[bytes, int]:
    """对"既不在 ``<>`` 也不在 ``()`` 里"的裸明文做替换。

    这一步是兜底：正常内容流不会出现裸明文，但**某些生成器**会写。
    为不误伤，只在整段可解码成文本时才做。
    """
    seq = len(hits)
    for enc in _ENCODINGS:
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        new_text, matches = engine.redact(text)
        if not matches:
            continue
        try:
            new_data = new_text.encode(enc)
        except UnicodeEncodeError:
            continue
        for m in matches:
            hits.append(
                Match(
                    rule_name=m.rule_name,
                    placeholder=m.placeholder,
                    start=seq,
                    end=seq + 1,
                    raw=m.raw,
                    page_index=STRUCTURE_PAGE_INDEX,
                )
            )
            seq += 1
        return new_data, len(matches)
    return data, 0


def _stream_has_secret(
    doc: "fitz.Document", xref: int, engine: RedactionEngine
) -> bool:
    """流里是否仍有敏感串（**双形态**判据）。

    这是"检测式兜底"的触发判据。必须与 :func:`scrub_stream_text`
    用同一套判据，否则会"检测不到但能替换"或反之。
    """
    if xref <= 0:
        return False
    try:
        data = doc.xref_stream(xref)
    except Exception:
        return False
    if not data:
        return False

    def _has(raw: bytes) -> bool:
        for enc in _ENCODINGS:
            try:
                if engine.scan(raw.decode(enc)):
                    return True
            except UnicodeDecodeError:
                continue
        return False

    if _has(data):
        return True
    for m in _HEX_STRING.finditer(data):
        h = re.sub(rb"\s+", b"", m.group(1))
        if len(h) % 2:
            h = h[:-1]
        try:
            raw = bytes.fromhex(h.decode("ascii"))
        except Exception:
            continue
        if _has(raw):
            return True
    return False


def _scrub_appearance_stream(
    doc: "fitz.Document", obj_xref: int, engine: RedactionEngine
) -> List[Match]:
    """**检测式**清洗对象的外观流（``/AP/N``）。

    为什么是"检测式"而不是"无条件替换"：见模块 docstring
    "一条被推翻的旧结论" —— 实测 ``update()`` 之后 ``/AP`` 已被重建、
    不再残留，无条件替换纯属多余动作（且每多一次写操作就多一次
    破坏结构的风险）。只在检测到残留时才动手。

    Args:
        doc: 文档（会被就地修改）。
        obj_xref: 注释/表单域对象的 xref。
        engine: 脱敏引擎。

    Returns:
        命中列表（未触发时为空）。
    """
    ap = _ap_xref(doc, obj_xref)
    if ap <= 0:
        return []
    if not _stream_has_secret(doc, ap, engine):
        return []
    try:
        data = doc.xref_stream(ap) or b""
    except Exception:
        return []
    new_data, hits = scrub_stream_text(data, engine)
    if not hits or new_data == data:
        return []
    try:
        doc.update_stream(ap, new_data)
    except Exception:
        return []
    return hits


def _ap_xref(doc: "fitz.Document", obj_xref: int) -> int:
    """取对象的 ``/AP/N`` 外观流 xref；``0`` 表示没有可改的外观流。"""
    try:
        typ, val = doc.xref_get_key(obj_xref, "AP/N")
    except Exception:
        return 0
    if typ != "xref":
        return 0
    m = re.match(r"\s*(\d+)\s+\d+\s+R", str(val))
    return int(m.group(1)) if m else 0


# ------------------------------------------------------------------ 注释停放
#
# 🔴 P1 既有缺陷（P3b 端到端冒烟暴露，修法经 5 轮探针定案）
# ========================================================
#
# ``apply_redactions()`` 会**删除与涂改矩形相交的注释**，且
# **12 种参数组合（images×graphics×text）全部删除，无参数可控**
# （实测 ``.verify/probe_p3b/probe_p3b_applyred.txt``）。
#
# 为什么这会打穿 P3b：``extract_spans`` 用 ``page.get_text("dict")``，
# 它**包含注释的渲染文本** —— FreeText 注释的显示文字会作为普通 span
# 出现在 dict 里。于是「正文命中 → 涂改矩形压在注释上 → 注释被删」，
# 注释的 ``/Contents`` 清洗结果连同注释本体一起消失。
#
# 排查记录（三条被推翻的假设，勿重走）
# ------------------------------------
#   ① "生成器迭代中 ``update()`` 丢元素"  → 探针 diag 推翻（最小样本 2 个都在）
#   ② "某一步清洗函数弄丢"               → 探针 locate 推翻（单独跑每步都正常）
#   ③ "``rawdict`` 不含注释文本，改用它"  → 探针 rawdict **推翻**：
#      ``rawdict`` 的 span **没有 ``text`` 键**（只有 ``chars``），
#      用 ``span.get("text","")`` 取值恒为空 → **假阴性**。
#      正确拼接 ``chars[].c`` 后，``rawdict`` 同样含注释文本。
#   ④ "``get_texttrace()`` 只读内容流、不含注释" → 探针 exclude **推翻**：
#      trace 读的是**渲染树**，注释 ``/AP`` 同样在渲染树里。
#   ⑤ "用注释 rect 做几何排除"           → 探针 exclude **推翻**：
#      注释渲染文本 bbox 的**上边界比注释 ``rect`` 高约 3.3pt**
#      （实测 rect y0=88.0，渲染 bbox y0=84.7），覆盖率仅 0.80 < 0.9，
#      排不掉注释自己的文本；把阈值放松又会**误排正文**（对抗性反例：
#      正文 span 与注释 bbox 部分重叠时被整段排除 = 漏脱敏）。
#      → **几何排除法弃用**。
#
# 定案修法（方案 A）：涂改前把 FreeText 注释"停放到页外"，涂改后"还原"
# ----------------------------------------------------------------------
#   停放：``an.set_rect(rect + (0, 5000))`` + ``an.update()``
#   还原：``an.set_rect(原 rect)`` + ``an.update()``
#
# 保真度实测（``.verify/probe_p3b/probe_p3b_fidelity.txt`` /
# ``probe_p3b_park.txt`` / ``probe_p3b_parkfid.txt``）：
#   - ``/AP`` xref 不变、**流字节 SHA-256 完全一致**（含 save + 重开）
#   - 14 项属性全等（rect/apn_bbox/rect_delta/colors/border/opacity/
#     flags/rotation/info×3/has_popup/type/xref）
#   - 注释数 1→1；中间态孤儿 ``/AP`` 被 ``garbage=4`` 回收（实测残留 = []）
#
# 🔴 停放范围**必须**收窄到 FreeText，两个方向都已实测：
#   - **必要性**：9 类注释扫描，**只有 FreeText** 相交时被删
#     （Text/Highlight/Square/Underline/StrikeOut/Squiggly/Stamp/
#      FileAttachment 全部保留）
#   - **充分性**：11 类注释做"停放→涂改→还原"保真度矩阵，
#     **Square 有损**（rect 被撑大 1pt、``/AP`` 字节变），
#     **Highlight/Underline/StrikeOut/Squiggly/Ink/Line 的 ``set_rect``
#     是静默 no-op**。
#
#     🔴 更正（``.verify/probe_p3b/probe_fix2b.txt`` Q4，含全新子进程
#     交叉验证）：这里原先写的是"这 6 类**根本没有 ``/Rect`` 属性**，
#     ``set_rect`` 抛 ``FzErrorArgument: code=4``" —— **与实测不符**。
#     实测：对象里**有** ``/Rect`` 键（``type='array'``）；``set_rect``
#     **不抛异常**、``rect`` **不变**；提示
#     ``cannot set rect: code=4: <Type> annotations have no Rect property``
#     打到 **fd 1（stdout）**、**每次调用打印一行**。
#
#     为什么"静默"比"报错"更危险（这才是收窄范围的真正理由）：
#     :func:`park_freetext_annots` 的形状是"先记 ``saved[xref]`` 再
#     ``set_rect``"。范围一旦放宽，这 6 类会走成：记入 saved（成功）
#     → ``set_rect`` 静默失败 → 注释**从未移动** → ``apply_redactions()``
#     照样把它删掉；而 :func:`restore_freetext_annots` 里 ``set_rect``
#     同样不抛 → ``restored += 1`` → ``len(parked) == restored``
#     → **"未复位"告警永不触发**。即：静默失效会**连带**让告警机制失效。
#     → 若"停放全部相交注释"，会**破坏 Square 的外观**、对 6 类**静默失效**
#       （且告警一起失效）。
#   两者恰好吻合：只停 FreeText，既够用又无损。
#
# Note:
#    停放用 ``+5000`` 而非"移到负坐标"：PDF 页面坐标原点在**左上**，
#    负 y 会跑到页外上方，部分阅读器会把它"夹"回页内；
#    下移 5000pt 稳妥落在任何常规页面尺寸之外。
_FREETEXT_PARK_DY = 5000.0


def park_freetext_annots(
    doc: "fitz.Document",
) -> Dict[int, Tuple[int, Tuple[float, float, float, float]]]:
    """把全部 FreeText 注释**停放到页外**，返回还原所需的信息。

    必须与 :func:`restore_freetext_annots` **成对使用**，
    且两次调用之间**不得** ``save``（``xref`` 跨 save 会重排，
    实测 ``[14,15,16]`` → ``[12,13,14]``）。

    Args:
        doc: 已打开的文档（**会被就地修改**）。

    Returns:
        ``{xref: (page_index, 原 rect 元组)}``。空字典 = 文档没有 FreeText 注释。

    Note:
        **两遍扫描**：第一遍只收集纯数据（不持有 Annot 引用），
        第二遍才施加位移。``page.annots()`` 是生成器，
        在迭代中改属性会干扰后续 yield（``page.annots()`` 返回的 Annot
        与 page 的绑定在 ``list()`` 后失效，抛
        ``FzErrorArgument: code=4: annotation not bound to any page``）。
    """
    saved: Dict[int, Tuple[int, Tuple[float, float, float, float]]] = {}
    for page_index in range(doc.page_count):
        page = doc[page_index]
        try:
            annots = page.annots()
        except Exception:
            continue
        targets: List[int] = []
        for an in annots:
            if an.type[1] != "FreeText":
                continue
            targets.append(an.xref)
            r = an.rect
            saved[an.xref] = (page_index, (r.x0, r.y0, r.x1, r.y1))
        if not targets:
            continue
        want = set(targets)
        try:
            again = page.annots()
        except Exception:
            continue
        for an in again:
            if an.xref not in want:
                continue
            try:
                r = an.rect
                an.set_rect(
                    fitz.Rect(r.x0, r.y0 + _FREETEXT_PARK_DY,
                              r.x1, r.y1 + _FREETEXT_PARK_DY)
                )
                an.update()
            except Exception:
                # 停放失败就从还原表里摘掉，避免还原时对"没被移动的注释"
                # 做一次无意义（且可能出错）的 set_rect。
                saved.pop(an.xref, None)
    return saved


def restore_freetext_annots(
    doc: "fitz.Document",
    saved: Dict[int, Tuple[int, Tuple[float, float, float, float]]],
) -> int:
    """还原 :func:`park_freetext_annots` 停放过的 FreeText 注释。

    Args:
        doc: 同一个文档对象（**未跨 save**）。
        saved: :func:`park_freetext_annots` 的返回值。

    Returns:
        成功还原的注释数。``saved`` 为空时返回 0。

    Note:
        按 **xref** 匹配而非按序号：停放后注释在页内的枚举顺序可能变化，
        按序号还原会张冠李戴。
    """
    if not saved:
        return 0
    restored = 0
    for page_index in range(doc.page_count):
        page = doc[page_index]
        try:
            annots = page.annots()
        except Exception:
            continue
        for an in annots:
            item = saved.get(an.xref)
            if item is None:
                continue
            _, rect_tuple = item
            try:
                an.set_rect(fitz.Rect(rect_tuple))
                an.update()
                restored += 1
            except Exception:
                continue
    return restored


# ------------------------------------------------------------------ 各处清洗
class _Collector:
    """收集命中并统一编号。

    为什么要一个收集器而不是各处自己编号：位置键是
    ``(规则名, page_index, start)``，六处都用 ``page_index=-1``，
    若各自从 0 开始就会**撞键**，后写的标签覆盖先写的、报告少报。
    故必须共享同一个序号空间，并**续 P3 的号**（``start_seq``）。
    """

    def __init__(self, start_seq: int = 0):
        self._seq = start_seq
        self.matches: List[Match] = []
        self.labels: List[str] = []
        self.warnings: List[str] = []
        self.parts: Dict[str, int] = {}

    def add(self, matches: Sequence[Match], label: str) -> int:
        """把一组命中登记到同一个标签下，返回登记条数。"""
        n = 0
        for m in matches:
            self.matches.append(
                Match(
                    rule_name=m.rule_name,
                    placeholder=m.placeholder,
                    start=self._seq,
                    end=self._seq + 1,
                    raw=m.raw,
                    page_index=STRUCTURE_PAGE_INDEX,
                )
            )
            self.labels.append(label)
            self._seq += 1
            n += 1
        return n

    def bump(self, part: str, n: int = 1) -> None:
        """累加某一类载体的改写计数。"""
        if n:
            self.parts[part] = self.parts.get(part, 0) + n


def _scrub_toc(
    doc: "fitz.Document", engine: RedactionEngine, col: _Collector
) -> None:
    """清洗书签（大纲）标题。

    🔴 **必须逐条 ``xref_set_key``，不能用 ``set_toc``** ——
    实测 ``set_toc`` 会抹掉 ``/C``（颜色）与 ``/F``（粗斜体），
    且这是**已实测**的（0/2，对照组 2/2），见模块 docstring 结论 4。

    xref 来源：``get_toc(simple=False)[i][3]['xref']``（实测总 >0 且唯一）。
    兜底：若某条拿不到 xref，扫对象层含 ``/Title`` 的非流对象
    （实测两条路的 xref 一致，可互为兜底）。

    层级/页码/顺序**不受影响** —— 我们只改 ``/Title`` 的值，
    不动 ``/First`` ``/Next`` ``/Prev`` ``/Parent`` 链。
    """
    try:
        toc = doc.get_toc(simple=False)
    except Exception:
        return
    if not toc:
        return

    for seq, item in enumerate(toc, start=1):
        if len(item) < 4:
            continue
        title = item[1] or ""
        style = item[3] or {}
        xref = int(style.get("xref", 0) or 0)
        new_title, matches = engine.redact(title)
        if not matches:
            continue
        if xref <= 0:
            xref = _find_title_object(doc, title)
        if xref <= 0:
            col.warnings.append(
                f"书签第 {seq} 条无法定位对象（xref 不可用），该条**未清洗**，"
                f"请人工核查文档书签"
            )
            continue
        try:
            doc.xref_set_key(xref, "Title", pdf_text_hex(new_title))
        except Exception as exc:
            col.warnings.append(
                f"书签第 {seq} 条写入失败（{type(exc).__name__}），"
                f"该条**未清洗**，请人工核查文档书签"
            )
            continue
        n = col.add(matches, bookmark_label(seq))
        col.bump("书签", n)


def _find_title_object(doc: "fitz.Document", title: str) -> int:
    """兜底：扫对象层找含该 ``/Title`` 的**非流**书签对象。

    实测与 ``get_toc`` 给出的 xref 一致（``probe_p3b_style4.txt`` Q6），
    故可互为兜底。只认 ``/Type /Outlines``（含 ``/Outline``）的对象，
    避免误伤页面对象的 ``/Title``。
    """
    target = pdf_text_hex(title).lower()
    plain = f"({title})".lower()
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
        except Exception:
            continue
        if not obj or "/Title" not in obj:
            continue
        if not _BOOKMARK_TYPE.search(obj):
            continue
        try:
            if doc.xref_is_stream(xref):
                continue
        except Exception:
            pass
        low = obj.lower()
        if target in low or plain in low:
            return xref
    return 0


def _scrub_annots(
    doc: "fitz.Document", engine: RedactionEngine, col: _Collector
) -> None:
    """清洗注释（批注）的 ``content`` / ``title`` / ``subject`` + 外观流。

    🔴 **必须就地迭代 ``for an in page.annots():``**。
    ``page.annots()`` 返回生成器，``list()`` 之后 Annot 与 page 的绑定失效，
    再访问属性抛 ``FzErrorArgument: code=4: annotation not bound to any page``。
    （``page.widgets()`` **无**此陷阱。）

    🔴 **必须清 ``title`` 与 ``subject``**：实测这两个键**确实含敏感串**
    （``title='作者 陈彦霖'``、``subject='主题 zhang.san@example.com'``）。
    只清 ``content`` 是漏脱敏。

    ``page.annots()`` **不含** Link 与 Widget（实测：第 1 页
    ``annots=[(0,'Text'),(2,'FreeText')]``、``links=[18,19]``），
    故注释/链接/表单域三者遍历互不重叠，不会重复清洗。
    """
    seq = 0
    for page_index in range(doc.page_count):
        page = doc[page_index]
        try:
            annots = page.annots()
        except Exception:
            continue
        for an in annots:
            seq += 1
            info = dict(an.info)
            updates: Dict[str, str] = {}
            hit_total = 0
            for key in ("content", "title", "subject"):
                raw = info.get(key) or ""
                if not raw:
                    continue
                new, matches = engine.redact(raw)
                if not matches:
                    continue
                updates[key] = new
                hit_total += len(matches)

            if not updates:
                # 结构层没命中，但外观流可能仍有（兜底）
                stream_hits = _scrub_appearance_stream(doc, an.xref, engine)
                if stream_hits:
                    col.add(
                        stream_hits,
                        page_object_label("注释", page_index, seq),
                    )
                    col.bump("注释")
                continue

            try:
                an.set_info(updates)
                an.update()
            except Exception as exc:
                col.warnings.append(
                    f"第 {page_index + 1} 页注释第 {seq} 个改写失败"
                    f"（{type(exc).__name__}），请人工核查批注"
                )
                continue

            # 逐键登记，让报告能区分是哪个键命中（标签仍不含内容）
            for key in ("content", "title", "subject"):
                if key not in updates:
                    continue
                _, matches = engine.redact(info.get(key) or "")
                col.add(
                    matches,
                    page_object_label("注释", page_index, seq),
                )
            col.bump("注释")

            # 检测式流兜底（见模块 docstring：正常路径下返回空）
            stream_hits = _scrub_appearance_stream(doc, an.xref, engine)
            if stream_hits:
                col.add(
                    stream_hits,
                    page_object_label("注释", page_index, seq),
                )


def _scrub_links(
    doc: "fitz.Document", engine: RedactionEngine, col: _Collector
) -> None:
    """清洗链接：URI 类的 ``/A/URI``，LAUNCH 类的 ``/A``。

    🔴 **不能用 ``update_link``**：实测它会**静默丢反斜杠**
    （``'https://x.com/A\\B'`` → ``'https://x.com/AB'``，保存成功、无异常），
    对 LAUNCH 类更会把 ``/S /Launch`` 改成 ``/S /GoToR`` 并双重百分号编码。

    🔴 **LAUNCH 的 ``kind`` 判定不能用 ``fitz.LINK_LAUNCH``**：

    实测 ``fitz.LINK_LAUNCH == 3``，但 ``get_links()`` 对 ``/S /Launch``
    返回 **``kind == 5``（即 ``LINK_GOTOR``）** —— 判等**恒为假**，
    整个 LAUNCH 分支从未执行（P3b 端到端实测抓出：LAUNCH 链接原样未清洗）。
    改用 **``link.get("file")`` 是否非空** 判定，与 PyMuPDF 的 kind 编号解耦。

    🔴 **必须整体重写 ``/A``，不能走嵌套路径 ``A/F/F``**：

    实测 ``xref_set_key(xref, "A/F/F", ...)`` 写入**无异常但只改了 ``/F``**，
    ``/UF`` 保持旧值；而 MuPDF 读的是 ``/UF`` → ``get_links()['file']``
    仍返回旧路径（**静默漏脱敏**）。整体重写 ``/A`` 后两键同时更新，
    且实测 ``/S /Launch`` 保持、``/Type /Filespec`` 保留。

    位置标签与 URI 类**共用**（``第 1 页 链接 第 1 个``）—— kind 差异
    对用户无定位价值。
    """
    seq = 0
    for page_index in range(doc.page_count):
        page = doc[page_index]
        try:
            links = page.get_links()
        except Exception:
            continue
        for link in links:
            xref = int(link.get("xref", 0) or 0)
            if xref <= 0:
                continue
            seq += 1
            label = page_object_label("链接", page_index, seq)
            hit_here = 0

            # 判定顺序：先看有没有 file（LAUNCH 的唯一可靠特征），
            # 再看 uri。不依赖 fitz.LINK_* 常量与 get_links 的 kind 映射。
            if link.get("file"):
                hit_here += _scrub_launch(doc, xref, engine, col, label)
            elif link.get("uri"):
                hit_here += _scrub_link_key(
                    doc, xref, "A/URI", engine, col, label
                )
            if hit_here:
                col.bump("链接")


def _scrub_launch(
    doc: "fitz.Document",
    xref: int,
    engine: RedactionEngine,
    col: _Collector,
    label: str,
) -> int:
    """清洗 LAUNCH 链接：整体重写 ``/A``，同时更新 ``/F`` 与 ``/UF``。

    只处理 **内联 dict** 形态的 ``/A/F``（实测 PyMuPDF ``insert_link``
    产出的就是这个形态）。``/A/F`` 是间接引用时无法安全重写整个 ``/A``
    （会丢掉被引用对象的其他键），此时走嵌套路径尽力改，并在两键
    不一致时告警。

    Returns:
        命中数。
    """
    typ, a_val = _safe_get_key(doc, xref, "A")
    if typ != "dict":
        return 0
    a_text = str(a_val)

    typ_f, f_val = _safe_get_key(doc, xref, "A/F")
    if typ_f == "xref":
        # 间接引用：退回逐键改（嵌套路径实测只改 /F 不改 /UF，故两键都试）
        hits = 0
        for key in ("A/F/F", "A/F/UF"):
            hits += _scrub_link_key(doc, xref, key, engine, col, label)
        return hits

    if typ_f != "dict":
        return 0

    # 内联 dict：取出 /F 与 /UF 的原文（可能是 (明文) 或 <hex>）
    new_files: Dict[str, str] = {}
    matches_total: List[Match] = []
    for key in ("F", "UF"):
        typ_k, val_k = _safe_get_key(doc, xref, f"A/F/{key}")
        if typ_k == "null":
            continue
        raw_text = (
            str(val_k) if typ_k in ("string", "name")
            else _decode_pdf_hex(str(val_k))
        )
        if not raw_text:
            continue
        new_text, matches = engine.redact(raw_text)
        if matches:
            new_files[key] = new_text
            matches_total.extend(matches)

    if not new_files:
        return 0

    # 重建 /A：保留 /S /Launch 与 /Type /Filespec，替换 /F 与 /UF。
    # 用正则替换而非重排字典，最大限度保留原结构（/A 里可能还有
    # 本模块不认识的键，整体重建会丢）。
    new_a = a_text
    for key, text in new_files.items():
        # 匹配 /F (...)  或  /F <...>，且**不匹配 /UF**（用 \b 前视）
        pattern = re.compile(
            r"(/" + key + r")(\s*)(\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>)"
        )
        repl = r"\1\2" + pdf_text_hex(text)
        new_a, n = pattern.subn(repl, new_a, count=1)
        if n == 0:
            col.warnings.append(
                f"{label} 的 /A/F/{key} 未能定位，**该键未清洗**，请人工核查"
            )
    try:
        doc.xref_set_key(xref, "A", new_a)
    except Exception as exc:
        col.warnings.append(
            f"{label} 写入失败（{type(exc).__name__}），请人工核查该链接"
        )
        return 0
    return col.add(matches_total, label)


def _safe_get_key(doc: "fitz.Document", xref: int, key: str):
    """``xref_get_key`` 的容错包装，返回 ``(type, value)``；失败给 ``("null", None)``。"""
    try:
        return doc.xref_get_key(xref, key)
    except Exception:
        return ("null", None)


def _scrub_link_key(
    doc: "fitz.Document",
    xref: int,
    key: str,
    engine: RedactionEngine,
    col: _Collector,
    label: str,
) -> int:
    """改链接对象的一个键，返回命中数。

    ``xref_set_key`` 收的是 **PDF 对象语法**，故用 :func:`pdf_text_hex`。

    为什么不用 ``engine.redact`` 的返回值直接写：URI 常含 ``mailto:`` 前缀，
    我们要的是"整串脱敏后写回"，与正文口径一致 —— ``engine.redact``
    正好做这件事。
    """
    try:
        typ, val = doc.xref_get_key(xref, key)
    except Exception:
        return 0
    if typ == "null":
        return 0
    # 明文形态：val 就是文本；hex 形态：需要解码
    raw_text: Optional[str]
    if typ == "string":
        raw_text = str(val)
    elif typ == "name":
        raw_text = str(val)
    else:
        raw_text = _decode_pdf_hex(str(val))
    if not raw_text:
        return 0

    new_text, matches = engine.redact(raw_text)
    if not matches:
        return 0
    try:
        doc.xref_set_key(xref, key, pdf_text_hex(new_text))
    except Exception as exc:
        col.warnings.append(
            f"{label} 写入失败（{type(exc).__name__}），请人工核查该链接"
        )
        return 0
    return col.add(matches, label)


def _scrub_embfiles(
    doc: "fitz.Document", engine: RedactionEngine, col: _Collector
) -> None:
    """清洗内嵌文件的**文件名与描述**；内容只检测、不改、告警。

    为什么内容首版不做（用户裁定 A1）：内嵌文件是**任意二进制**
    （docx/xlsx/zip/图片），直接改字节会破坏其内部结构（zip 偏移、
    压缩流）。与 P2"无框线表格不给行列号"同类判断 —— **宁可不做，
    也不做半吊子**。用户可先导出内嵌文件、单独跑一遍本工具再重新嵌入。

    🔴 ``embfile_upd`` 在 PyMuPDF 1.28 **是 bug**（全形式报
    ``AttributeError: 'bytes' object has no attribute 'm_internal'``），
    故用 ``embfile_del`` + ``embfile_add`` 重建。

    🔴 ``embfile_del`` 的 key 必须用 **API 层原样名（mojibake）**，
    不能用 :func:`_de_mojibake` 还原后的名字 —— 否则
    ``ValueError: '...' not in EmbeddedFiles array``（实测）。
    """
    try:
        count = doc.embfile_count()
    except Exception:
        return
    if not count:
        return

    warnings: List[str] = []
    #: ``(API 原样名, 内容, 新文件名, 新描述, 命中标签)``
    plan: List[Tuple[str, bytes, str, str, str]] = []
    for i in range(count):
        raw_name = doc.embfile_names()[i]
        human = _de_mojibake(raw_name)
        try:
            info = doc.embfile_info(i)
        except Exception:
            info = {}
        desc = info.get("description") or ""
        data = doc.embfile_get(i) or b""

        new_human, name_matches = engine.redact(human)
        new_desc, desc_matches = engine.redact(desc)

        # --- 内容只检测，不改（用户裁定 A1）---
        if data:
            content_hits = _scan_bytes(data, engine)
            if content_hits:
                warnings.append(
                    f"内嵌文件第 {i + 1} 个的内容里含敏感信息"
                    f"（{content_hits} 处），**首版不清洗内嵌文件内容**，"
                    f"请导出该文件后单独处理，再重新嵌入"
                )

        if not name_matches and not desc_matches:
            continue
        plan.append((raw_name, data, new_human, new_desc, embfile_label(i + 1)))

    for raw_name, data, new_human, new_desc, label in plan:
        # 先把该条内容读到内存（del 之后就取不到了）
        try:
            doc.embfile_del(raw_name)
        except Exception:
            warnings.append(
                f"{label} 删除失败，**未清洗**，请人工核查附件"
            )
            continue
        try:
            doc.embfile_add(new_human, data, desc=new_desc or None)
        except Exception:
            # 加不回去 = 文件丢了，这比"没清洗"严重得多，必须回滚原名
            try:
                doc.embfile_add(
                    _de_mojibake(raw_name), data, desc=new_desc or None
                )
            except Exception:
                pass
            warnings.append(
                f"{label} 重建失败，已回滚为原名，**未清洗**，"
                f"请人工核查附件"
            )
            continue

        # 逐键重新识别一遍以登记命中（redact 有编号语义，不能复用旧结果）
        _, name_matches = engine.redact(_de_mojibake(raw_name))
        col.add(name_matches, label)
        if new_desc:
            _, desc_matches = engine.redact(
                _embfile_desc_at(doc, new_human)
            )
            col.add(desc_matches, label)
        col.bump("内嵌文件")

    col.warnings.extend(warnings)


def _embfile_desc_at(doc: "fitz.Document", human_name: str) -> str:
    """按**还原后**的文件名取该内嵌文件的描述；取不到返回空串。"""
    try:
        names = doc.embfile_names()
    except Exception:
        return ""
    for i, n in enumerate(names):
        if _de_mojibake(n) == human_name:
            try:
                return doc.embfile_info(i).get("description") or ""
            except Exception:
                return ""
    return ""


def _scan_bytes(data: bytes, engine: RedactionEngine) -> int:
    """在任意字节里数敏感串命中数（多编码），用于"内容含敏感串"检测。"""
    total = 0
    seen = set()
    for enc in _ENCODINGS:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        total = max(total, len(engine.scan(text)))
    return total


def _scrub_widgets(
    doc: "fitz.Document", engine: RedactionEngine, col: _Collector
) -> None:
    """清洗表单域：``field_name`` / ``field_value`` / ``field_label`` + 外观流。

    为什么清 ``field_name``（用户裁定 F1）：实测改后 ``is_form_pdf`` 仍为 1、
    字段仍可被找到（``after = ['phone_redacted']``），不破坏表单；
    而不清就是漏脱敏 —— 实测字段名可以是 ``phone_13800138000``。

    ``update()`` 之后 ``/AP`` 会被重建（实测，见模块 docstring），
    故流兜底只做检测式补漏。
    """
    seq = 0
    for page_index in range(doc.page_count):
        page = doc[page_index]
        try:
            widgets = page.widgets()
        except Exception:
            continue
        for w in widgets:
            seq += 1
            label = page_object_label("表单域", page_index, seq)
            hit_total = 0
            touched = False

            for attr in ("field_name", "field_value", "field_label"):
                raw = getattr(w, attr, None)
                if not raw or not isinstance(raw, str):
                    continue
                new, matches = engine.redact(raw)
                if not matches:
                    continue
                try:
                    setattr(w, attr, new)
                    touched = True
                except Exception:
                    col.warnings.append(
                        f"{label} 的 {attr} 写入失败，请人工核查表单"
                    )
                    continue
                hit_total += col.add(matches, label)

            if touched:
                try:
                    w.update()
                except Exception as exc:
                    col.warnings.append(
                        f"{label} 外观更新失败（{type(exc).__name__}），"
                        f"请人工核查表单"
                    )
                col.bump("表单域")

            # 检测式流兜底
            stream_hits = _scrub_appearance_stream(doc, w.xref, engine)
            if stream_hits:
                col.add(stream_hits, label)


def _scrub_page_labels(
    doc: "fitz.Document", engine: RedactionEngine, col: _Collector
) -> None:
    """清洗页面标签（``/PageLabels`` 的 ``/P`` 前缀）。

    这处**不在用户给的 5 处清单里**，但探针顺手测到确实含敏感串
    （``'第陈彦霖章-'``），且成本极低（用户裁定 G1 纳入）。

    🔴 ``get_page_labels()`` 的 ``prefix`` 是**八进制转义字面串**
    （``'\\347\\254\\25413800138000\\347\\253\\240-'`` = 「第…章-」），
    **不是** ``<hex>``。旧实现用 :func:`_decode_pdf_hex` 解码得到
    ``None``，再 ``or prefix`` 兜回转义串 → 引擎不命中 → **完全未清洗**
    （P3b 端到端实测抓出）。故必须用 :func:`_decode_pdf_text`。

    ``set_page_labels`` 收**明文**，写盘后回读是 ``<hex UTF-8>``
    （实测：写 ``'第[手机号]章-'`` → 读回 ``'<E7ACAC5BE6898B...>'``）。
    """
    try:
        labels = doc.get_page_labels()
    except Exception:
        return
    if not labels:
        return

    new_labels = []
    seq = 0
    changed = False
    for lab in labels:
        seq += 1
        item = dict(lab)
        prefix = item.get("prefix") or ""
        if prefix:
            text = _decode_pdf_text(prefix)
            new_prefix, matches = engine.redact(text)
            if matches:
                item["prefix"] = new_prefix
                changed = True
                col.add(matches, page_label_label(seq))
                col.bump("页面标签")
        new_labels.append(item)

    if not changed:
        return
    try:
        doc.set_page_labels(new_labels)
    except Exception as exc:
        col.warnings.append(
            f"页面标签写入失败（{type(exc).__name__}），请人工核查页码标签"
        )


# ------------------------------------------------------------------ 入口
def scrub_structures(
    doc: "fitz.Document",
    engine: RedactionEngine,
    *,
    start_seq: int = 0,
) -> StructureScrubResult:
    """清洗 PDF 的 6 处结构化载体，返回统一的命中列表（已续号）。

    **这是本模块的唯一对外入口。**

    Args:
        doc: 已打开的 PDF（**会被就地修改**）。
        engine: 脱敏引擎。
        start_seq: 起始序号。由 handler 传入 ``len(meta.matches)``
            （P3 元数据的命中数），使两处共享同一个序号空间 ——
            否则两处都用 ``page_index=-1`` 且各自从 0 编号会**撞键**，
            后写的标签覆盖先写的、报告少报。

    Returns:
        :class:`StructureScrubResult`。

    Note:
        **必须在同一次 open→改→save 内调用**：``xref`` 跨 save 会重排
        （实测 ``[14,15,16]`` → ``[12,13,14]``），跨 save 缓存 xref 会改错对象。
    """
    col = _Collector(start_seq=start_seq)
    # 顺序无关（实测 K1/K2：先流后结构 / 先结构后流，结果相同）。
    # 这里按"对象层 -> 页内对象 -> 文档级"排，便于日志阅读。
    _scrub_toc(doc, engine, col)
    _scrub_annots(doc, engine, col)
    _scrub_links(doc, engine, col)
    _scrub_widgets(doc, engine, col)
    _scrub_embfiles(doc, engine, col)
    _scrub_page_labels(doc, engine, col)

    return StructureScrubResult(
        matches=col.matches,
        labels=col.labels,
        warnings=col.warnings,
        parts_touched=col.parts,
    )
