"""PDF 脱敏结果的**字节级差分验证**：内容流解码后对照搜索。

为什么需要这个模块（踩坑记录，别删）
====================================

P1.3 的 L3 验证原本是「在输出文件的**原始字节**里搜明文，计数为 0 即通过」。
这条断言**恒真** —— 因为它对**未脱敏的源文件**同样返回 0，不构成任何删净证据。

根因：PDF 把文本存在内容流里，且是**十六进制字形字节**形态，例如

    BT /helv 12 Tf [<b7b7203133383030313338303030>]TJ ET
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                         即 " 13800138000"

明文 ``13800138000`` 在原始字节里**根本不存在**（无论脱敏与否），
内容流默认还被 Flate 压缩。所以「原始字节计数为 0」是空转断言。

修法（本模块）：先解压内容流，再把其中的十六进制/字面字符串**解码**成
字节，得到「字形字节搜索域」；然后在**源文件与输出文件上各搜一次**做差分：

    源文件必须能检出   —— 否则搜索域无鉴别力，断言仍是空转
    输出文件必须为 0   —— 这才是删净证据

两者同时成立才算 PASS。仅"输出为 0"不算。

编码维度为什么不能只试一种
==========================

同一个串在不同字体下的字节形态不同（实测合成样本）：

    /helv     -> " 13800138000"        UTF-8 / GBK 可检出
    china-s   -> \\x001\\x003\\x008...   UTF-16-BE / UTF-16-LE 可检出

故必须遍历多种编码；只试 UTF-8 会在中文内置字体的样本上漏检。

已知局限
========

1. **不做 CMap 还原**：本模块只解码「十六进制/字面字符串」这一层，
   不把字形 ID 反查成 Unicode。因此它证明的是
   「内容流里不再有该串的字节形态」，**不是**「字形渲染出来一定不同」。
   后者靠 L1（get_text）/L2（search_for）与渲染肉眼复核。
2. **字面字符串只解基础转义**（``\\n \\r \\t \\b \\f \\( \\) \\\\`` 与八进制）。
   非常规转义可能漏检，属已知未覆盖项。
3. **排除字体程序流**（``/FontFile*``、``/Length1``）：字体子集里可能
   恰好含目标字节序列，那是误报源而非文本残留。排除后可能漏掉
   「文本被塞进字体程序」这种非常规情形。
4. **不覆盖 /Info 元数据与 XObject 之外的对象**（**产品级缺口，尚未修**）：
   本模块只搜**内容流**（``doc.xref_stream``），而 ``/Info`` 字典
   （Title / Author / Subject / Keywords / Creator / Producer）是
   **普通字符串对象，不是流**，因此完全不在此搜索域内。

   实测复现（``.verify/probe_meta.py``）：造样本
   ``set_metadata(title="报告 13800138000", author=..., subject=...,
   keywords=..., creator=...)`` 走一遍脱敏后，
   **5 个字段全部原样保留**手机号，``fitz`` 打开输出文件
   ``metadata['title']`` 仍读出完整号码；而本模块对同一文件判 **PASS**。

   即：**L3 PASS 不代表整份文件无残留**，只代表「内容流里没有」。
   同理，XObject（表单/嵌入对象）之外的非常规容器也未覆盖。
   该缺口自 P1.1 起就存在，与 L3 差分改造无关，**列入后续轮次**。

   使用本模块时务必同时跑 L1/L2，并对元数据单独检查——
   不要把它当作"整份文件已删净"的唯一证据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Sequence, Tuple

import fitz  # PyMuPDF

__all__ = [
    "ENCODINGS",
    "ResidueCheck",
    "decode_stream_strings",
    "verify_residue",
    "format_report",
]

#: 需要尝试的字节编码。不同字体会让同一串以不同形态落盘，故不能只试一种。
ENCODINGS: Tuple[str, ...] = ("utf-8", "gbk", "utf-16-be", "utf-16-le")

#: 内容流里的十六进制字符串 ``<...>``。
#: ``<<``/``>>`` 是字典分隔符，不会被匹配（``<`` 不是十六进制字符）。
_HEX_STRING = re.compile(rb"<([0-9A-Fa-f\s]+)>")

#: 内容流里的字面字符串 ``(...)``，允许 ``\`` 转义。
_LITERAL_STRING = re.compile(rb"\(((?:\\.|[^\\()])*)\)")

#: 字体程序流的特征标记。命中即跳过——字体子集可能恰好含目标字节。
_FONT_MARKERS: Tuple[bytes, ...] = (
    b"/FontFile",
    b"/Length1",
    b"/Type1C",
    b"/CIDFontType0C",
)

_LITERAL_ESCAPES: Dict[int, int] = {
    0x6E: 0x0A,  # \n
    0x72: 0x0D,  # \r
    0x74: 0x09,  # \t
    0x62: 0x08,  # \b
    0x66: 0x0C,  # \f
    0x28: 0x28,  # \(
    0x29: 0x29,  # \)
    0x5C: 0x5C,  # \\
}


def _unescape_literal(raw: bytes) -> bytes:
    """解开字面字符串的基础转义。

    只处理 PDF 规范里的常规转义与八进制转义；未知转义按"反斜杠丢弃、
    保留后续字符"处理（PDF 规范的推荐行为）。
    """
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != 0x5C or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in _LITERAL_ESCAPES:
            out.append(_LITERAL_ESCAPES[nxt])
            i += 2
            continue
        if 0x30 <= nxt <= 0x37:      # 八进制 \ddd
            j = i + 1
            digits = b""
            while j < n and len(digits) < 3 and 0x30 <= raw[j] <= 0x37:
                digits += bytes([raw[j]])
                j += 1
            out.append(int(digits, 8) & 0xFF)
            i = j
            continue
        out.append(nxt)
        i += 2
    return bytes(out)


def _decode_stream(data: bytes) -> bytes:
    """把一段内容流里的十六进制/字面字符串解码后拼成字节串。

    各片段之间插入 ``\\x00`` 分隔，避免跨片段拼出假命中。
    """
    chunks: List[bytes] = []
    for m in _HEX_STRING.finditer(data):
        h = re.sub(rb"\s+", b"", m.group(1))
        if len(h) % 2:               # 奇数个十六进制字符是非法的，丢掉末位
            h = h[:-1]
        if not h:
            continue
        try:
            chunks.append(bytes.fromhex(h.decode("ascii")))
        except Exception:
            continue
    for m in _LITERAL_STRING.finditer(data):
        chunks.append(_unescape_literal(m.group(1)))
    return b"\x00".join(chunks)


def _iter_search_blobs(path: Path) -> Iterator[Tuple[int, bytes]]:
    """逐个产出 ``(xref, 解码后的搜索域字节)``。

    跳过非流对象与字体程序流。
    """
    doc = fitz.open(str(path))
    try:
        for xref in range(1, doc.xref_length()):
            try:
                obj = doc.xref_object(xref)
            except Exception:
                obj = ""
            if obj:
                head = obj.encode("latin-1", "ignore")
                if any(marker in head for marker in _FONT_MARKERS):
                    continue
            try:
                data = doc.xref_stream(xref)
            except Exception:
                continue
            if not data:
                continue
            yield xref, _decode_stream(data)
    finally:
        doc.close()


def decode_stream_strings(path: Path) -> bytes:
    """全部（非字体）流解码后的合并搜索域。供人工复核与调试。"""
    return b"\x00".join(blob for _, blob in _iter_search_blobs(path))


@dataclass(frozen=True)
class ResidueCheck:
    """单个敏感串的差分验证结果。

    Attributes:
        needle: 被检查的敏感串（**只存在于内存与验证报告，不进脱敏报告**）。
        source_counts: 源文件各编码的命中数。
        output_counts: 输出文件各编码的命中数。
        source_xrefs: 源文件里出现该串的对象号（供人工定位）。
        output_xrefs: 输出文件里出现该串的对象号。
    """

    needle: str
    source_counts: Dict[str, int]
    output_counts: Dict[str, int]
    source_xrefs: Tuple[int, ...] = ()
    output_xrefs: Tuple[int, ...] = ()

    @property
    def detectable_in_source(self) -> bool:
        """搜索域是否有鉴别力：源文件里**必须**能检出该串。

        这是防「空转断言」的核心：若源文件也检不出，说明搜索域选错了，
        此时"输出为 0"毫无意义。
        """
        return any(v > 0 for v in self.source_counts.values())

    @property
    def residue_in_output(self) -> bool:
        """输出文件里是否仍有残留（任一编码命中即为残留）。"""
        return any(v > 0 for v in self.output_counts.values())

    @property
    def ok(self) -> bool:
        """差分验证是否通过。"""
        return self.detectable_in_source and not self.residue_in_output

    def describe(self) -> str:
        """一行可读结论，供验证脚本打印。"""
        if not self.detectable_in_source:
            return (
                f"[无鉴别力] {self.needle!r}：源文件也检不出 —— "
                f"该串不能用作 L3 对照，断言会空转"
            )
        if self.residue_in_output:
            return (
                f"[残留] {self.needle!r}：源={self.source_counts} "
                f"输出={self.output_counts} 对象={self.output_xrefs}"
            )
        return (
            f"[PASS] {self.needle!r}：源={self.source_counts} "
            f"-> 输出={self.output_counts}"
        )


def _scan(
    path: Path,
    needles: Sequence[str],
    encodings: Sequence[str],
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, List[int]]]:
    """在单个文件上按多种编码统计各串命中数。"""
    counts: Dict[str, Dict[str, int]] = {
        n: {e: 0 for e in encodings} for n in needles
    }
    xrefs: Dict[str, List[int]] = {n: [] for n in needles}
    encoded: Dict[Tuple[str, str], bytes | None] = {}
    for needle in needles:
        for enc in encodings:
            try:
                encoded[(needle, enc)] = needle.encode(enc)
            except UnicodeEncodeError:
                encoded[(needle, enc)] = None

    for xref, blob in _iter_search_blobs(path):
        for needle in needles:
            hit = False
            for enc in encodings:
                nb = encoded[(needle, enc)]
                if nb is None:
                    continue
                found = blob.count(nb)
                if found:
                    counts[needle][enc] += found
                    hit = True
            if hit:
                xrefs[needle].append(xref)
    return counts, xrefs


def verify_residue(
    source: Path,
    output: Path,
    needles: Sequence[str],
    *,
    encodings: Sequence[str] = ENCODINGS,
) -> List[ResidueCheck]:
    """对每个敏感串做「源可检出 / 输出为 0」的差分验证。

    Args:
        source: **未脱敏**的源文件。用于证明搜索域有鉴别力。
        output: 脱敏后的输出文件。
        needles: 待验证的敏感串。
        encodings: 尝试的字节编码，默认 :data:`ENCODINGS`。

    Returns:
        与 ``needles`` 等长的 :class:`ResidueCheck` 列表。
        **注意**：调用方必须同时检查 ``detectable_in_source``，
        只看 ``residue_in_output`` 会退化成空转断言。
    """
    source = Path(source)
    output = Path(output)
    src_counts, src_xrefs = _scan(source, needles, encodings)
    out_counts, out_xrefs = _scan(output, needles, encodings)
    return [
        ResidueCheck(
            needle=needle,
            source_counts=src_counts[needle],
            output_counts=out_counts[needle],
            source_xrefs=tuple(src_xrefs[needle]),
            output_xrefs=tuple(out_xrefs[needle]),
        )
        for needle in needles
    ]


def format_report(checks: Sequence[ResidueCheck]) -> str:
    """把验证结果排版成多行文本。"""
    lines = ["L3 字节流差分验证（内容流解码后对照）"]
    for check in checks:
        lines.append("  " + check.describe())
    failed = [c for c in checks if not c.ok]
    if not failed:
        lines.append("  结论：全部通过（源可检出 且 输出为 0）")
    else:
        lines.append(f"  结论：{len(failed)} 项未通过")
        for c in failed:
            if not c.detectable_in_source:
                lines.append(
                    f"    - {c.needle!r} 源文件检不出：搜索域无鉴别力，"
                    f"该断言会空转，须换搜索域或换对照串"
                )
            else:
                lines.append(
                    f"    - {c.needle!r} 输出仍有残留："
                    f"{c.output_counts} @ 对象 {c.output_xrefs}"
                )
    return "\n".join(lines)
