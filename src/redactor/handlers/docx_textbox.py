"""docx 文本框 / 脚注 / 尾注遍历（优先级 6，docx 最后一站）。

三种容器的存储形态完全不同（均实测确认）
----------------------------------------
=========  ==========================================  ==================
容器       存储位置                                   part 形态
=========  ==========================================  ==================
文本框     ``document.xml`` 内的 ``<w:txbxContent>``    无独立 part
批注``word/comments.xml``                有 ``element``
脚注/ 尾注   ``word/footnotes.xml`` / ``endnotes.xml``    **无 ``element``**
=========  ==========================================  ==================

脚注/尾注的写回路径（实测验证）
------------------------------
它们的 part 是裸 ``Part``，**没有 ``element`` 属性**，所以不能像批注那样
"改完 element 直接 save"。实测可行路径::

    root = parse_xml(part.blob)      # 用 python-docx 的定制 parser
    ... 改root 里的节点 ...
    part._blob = etree.tostring(root, xml_declaration=True,
                                encoding="UTF-8", standalone=True)
    document.save(...)                # 随 save 一并落盘

已验证：写回后二次打开可正常解析（幂等）、``w:`` 命名空间前缀完整保留、
``w:id`` 与 ``w:type`` 均不变。

特殊条目
--------
脚注/尾注里有 ``w:type="separator"`` 与 ``w:type="continuationSeparator"``
两类**系统保留条目**（分隔线用的空条目），不是用户内容，遍历时必须过滤。
"""

from __future__ import annotations

from typing import Iterator, List, Optional, Tuple

from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree

#: 文本框嵌套深度上限。超过则跳过该文本框并计入报告。
MAX_TEXTBOX_DEPTH = 5

#: 脚注/尾注里需要跳过的系统保留条目类型
_RESERVED_NOTE_TYPES = frozenset({"separator", "continuationSeparator"})


# ---------------------------------------------------------------------------
# 文本框
# ---------------------------------------------------------------------------
def iter_textbox_paragraphs(
    document, depth: int = 1
) -> Iterator[Tuple[Paragraph, str]]:
    """遍历所有文本框内的段落（含嵌套文本框）。

    文本框**内嵌在 document.xml 里**，不在独立 part中。
    VML（``<w:pict>``）与 DrawingML（``<w:drawing>``）两种形态都包含
    ``<w:txbxContent>``，因此遍历方式相同。

    位置标签用**文档级递增编号**（``文本框 1``、``文本框 2`` …），
    嵌套文本框也继续编号——保证"一个编号对应一个实际文本框"，
    便于人工按图索骥。深度用单独的措辞标注。

    Args:
        document: python-docx 的 ``Document`` 对象。
        depth: 当前递归深度（从 1 开始）。

    Yields:
        ``(段落, 位置标签)``。标签形如 ``文本框 1``、``文本框 3（第 2 层）``。

    Raises:
        RecursionError: 嵌套深度超过 :data:`MAX_TEXTBOX_DEPTH`。
    """
    state = {"index": 0}
    yield from _walk_textboxes(document.element.body, 1, state)


def _walk_textboxes(root, depth: int, state: dict) -> Iterator[Tuple[Paragraph, str]]:
    """深度优先遍历 ``root`` 内的**最外层**文本框，内层交给递归。

    ``state`` 持有跨递归共享的递增编号，保证"一个编号对应一个实际文本框"。

    顶层只处理"没有文本框祖先"的那些；递归时 ``root`` 本身就是那个
    嵌套文本框，此时它**不算嵌套**（否则会自我跳过，形成死锁）。
    """
    if depth > MAX_TEXTBOX_DEPTH:
        raise RecursionError(
            f"文本框嵌套深度超过上限 {MAX_TEXTBOX_DEPTH}，疑似病态文档"
        )

    for txbx in root.iter(qn("w:txbxContent")):
        # 只处理"没有文本框祖先"的最外层文本框。
        # 递归时传入的 root 本身就是嵌套文本框，它的后代才有祖先，
        # 而它自己不会——所以这个判据在两种场景下都正确。
        if _has_textbox_ancestor(txbx):
            continue

        state["index"] += 1
        label = (
            f"文本框 {state['index']}"
            if depth == 1
            else f"文本框 {state['index']}（第 {depth} 层）"
        )

        # 用 set 记录已产出的段落，避免"段落遍历"与"表格遍历"重复命中。
        seen = set()

        # 只取**直属**段落——内层段落由下方递归处理，标签才对应正确编号。
        for p_element in txbx.findall(qn("w:p")):
            if p_element not in seen:
                seen.add(p_element)
                yield Paragraph(p_element, txbx), label

        # 其内表格（含嵌套表格）里的段落
        for paragraph, _ in _iter_table_paragraphs(txbx, label):
            if paragraph._p not in seen:
                seen.add(paragraph._p)
                yield paragraph, label

        # 递归处理嵌套文本框：它们挂在本文本框的某个 <w:p> 内部
        for nested in txbx.iter(qn("w:txbxContent")):
            if nested is not txbx:
                yield from _walk_nested_textboxes(nested, depth + 1, state)


def _walk_nested_textboxes(txbx, depth: int, state: dict) -> Iterator[Tuple[Paragraph, str]]:
    """处理一个**已确定的**嵌套文本框（不再做祖先判定）。"""
    if depth > MAX_TEXTBOX_DEPTH:
        raise RecursionError(
            f"文本框嵌套深度超过上限 {MAX_TEXTBOX_DEPTH}，疑似病态文档"
        )

    state["index"] += 1
    label = f"文本框 {state['index']}（第 {depth} 层）"

    seen = set()
    for p_element in txbx.findall(qn("w:p")):
        if p_element not in seen:
            seen.add(p_element)
            yield Paragraph(p_element, txbx), label

    for paragraph, _ in _iter_table_paragraphs(txbx, label):
        if paragraph._p not in seen:
            seen.add(paragraph._p)
            yield paragraph, label

    for nested in txbx.iter(qn("w:txbxContent")):
        if nested is not txbx:
            yield from _walk_nested_textboxes(nested, depth + 1, state)


def _has_textbox_ancestor(txbx) -> bool:
    """判断 ``txbx`` 是否嵌套在另一个文本框内部。

    只要存在祖先 ``<w:txbxContent>`` 即为嵌套。
    顶层遍历据此只处理最外层文本框，内层交给递归——否则内层会被
    顶层 ``root.iter()`` 与递归各处理一遍，同一段落产出多次（实测 3次）。
    """
    return any(True for _ in txbx.iterancestors(qn("w:txbxContent")))


def _iter_table_paragraphs(container, label: str) -> Iterator[Tuple[Paragraph, str]]:
    """产出容器内所有表格（含嵌套）的段落，按单元格元素去重。"""
    seen_cells = set()
    for tc in container.iter(qn("w:tc")):
        if tc in seen_cells:
            continue  # 合并单元格：同一 XML 元素
        seen_cells.add(tc)
        for p_element in tc.findall(qn("w:p")):
            yield Paragraph(p_element, tc), label


def iter_textbox_contents(document) -> Iterator[Tuple[object, str]]:
    """产出所有 ``<w:txbxContent>`` 元素与位置标签（用于外层白名单核对）。"""
    for index, txbx in enumerate(document.element.body.iter(qn("w:txbxContent")), start=1):
        yield txbx, f"文本框 {index}"


# ---------------------------------------------------------------------------
# 脚注 / 尾注
# ---------------------------------------------------------------------------
def _find_note_root(document, note_kind: str):
    """按类型找到脚注/尾注的 part 并解析其 XML 根元素。

    这两类 part 是裸 ``Part``，**没有 ``element`` 属性**，只能解析 blob。

    Args:
        document: python-docx 的 ``Document`` 对象。
        note_kind: ``"footnote"`` 或 ``"endnote"``。

    Returns:
        ``(part, 根元素)``；不存在则返回 ``(None, None)``。
    """
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    wanted = RT.FOOTNOTES if note_kind == "footnote" else RT.ENDNOTES
    for rel in document.part.rels.values():
        if rel.reltype != wanted:
            continue
        part = getattr(rel, "target_part", None)
        if part is None:
            continue
        return part, parse_xml(part.blob)
    return None, None


def iter_note_paragraphs(
    document, note_kind: str
) -> Iterator[Tuple[Paragraph, str, object]]:
    """遍历脚注或尾注里的段落。

    Args:
        document: python-docx 的 ``Document`` 对象。
        note_kind: ``"footnote"`` 或 ``"endnote"``。

    Yields:
        ``(段落, 位置标签, 根元素)``。标签形如 ``脚注 #2`` / ``尾注 #2``。
        第三个返回值是解析出的根元素，调用方**必须**在处理后把它序列化回part，
        否则改动不会落盘（见模块开头的写回说明）。
    """
    part, root = _find_note_root(document, note_kind)
    if root is None:
        return

    tag = "w:footnote" if note_kind == "footnote" else "w:endnote"
    prefix = "脚注" if note_kind == "footnote" else "尾注"

    for note in root.findall(qn(tag)):
        # 过滤系统保留条目（separator / continuationSeparator）——它们是
        # 分隔线用的空条目，不含用户内容，误处理会破坏文档结构。
        note_type = note.get(qn("w:type"))
        if note_type in _RESERVED_NOTE_TYPES:
            continue

        note_id = note.get(qn("w:id")) or "?"
        label = f"{prefix} #{note_id}"
        for p_element in note.findall(qn("w:p")):
            yield Paragraph(p_element, note), label, root


def commit_note_part(part, root) -> None:
    """把解析后的脚注/尾注根元素写回 part。

    **必须显式调用**，否则改动不会落盘。

    Args:
        part: ``word/footnotes.xml`` / ``endnotes.xml`` 对应的 part。
        root: 已改动的根元素。
    """
    part._blob = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def get_note_part(document, note_kind: str):
    """取脚注/尾注的 part（供 :func:`commit_note_part` 使用）。"""
    part, _ = _find_note_root(document, note_kind)
    return part