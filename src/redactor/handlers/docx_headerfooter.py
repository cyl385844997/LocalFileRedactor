"""docx 页眉页脚遍历（优先级 4）。

为什么需要独立模块
------------------
``document.paragraphs`` **不含页眉页脚内容**（实测确认），它们存放在
``word/header1.xml`` / ``word/footer1.xml`` 等独立 part 里，
必须按 ``section.header`` / ``section.footer`` 单独遍历。

三个实测确认的关键事实
---------------------
1. **读取 ``section.first_page_header`` 等入口会产生副作用**::

       # 只读取，不做任何设置，保存后包内就多出文件：
       header1.xml  header2.xml  header3.xml
       footer1.xml  footer2.xml  footer3.xml
       且 sectPr 里凭空多出 type="first" / type="even" 的引用

   所以**不能靠 getattr 遍历**——那会在输出文档里凭空生成 4 个空页眉页脚，
   改变文档结构。正确做法是从 ``sectPr`` 里读**已存在的引用**。

2. **"链接到前一节"是读取时的回溯，不是独立存储**::

       第1节: is_linked=False  part=/word/header1.xml
       第2节: is_linked=False  part=/word/header2.xml
       第3节: is_linked=True   part=/word/header2.xml   ← 与第2节同一个 part

   若按"节"遍历而不去重，第3节会把第2节已处理的内容再处理一遍，
   **虚增命中次数**。

3. **必须按 part 对象本身去重，不能用 id()**。
   见表格模块的教训：代理对象被回收后 id() 会被复用。
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Tuple

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

#: ``sectPr`` 里的引用类型 -> (中文前缀, 是否属于"可能不显示"的特殊类型)
#: ``w:type`` 的取值：default（默认页）、first（首页）、even（偶数页）
_REFERENCE_TYPES = {
    "default": ("页眉", False),
    "first": ("首页页眉", True),
    "even": ("偶数页眉", True),
}

_FOOTER_REFERENCE_TYPES = {
    "default": ("页脚", False),
    "first": ("首页页脚", True),
    "even": ("偶数页脚", True),
}


class HeaderFooterItem:
    """一个待处理的页眉/页脚 part。

    Attributes:
        part: 底层 part（``HeaderPart`` / ``FooterPart``）。
        label: 位置标签，如 ``页眉 第1节``。
        section_index: 节序号（从 1 开始）。
        ref_type: ``sectPr`` 里的引用类型：``default`` / ``first`` / ``even``。
        is_header: True 表示页眉，False 表示页脚。
        is_special: 是否属于"当前可能不显示"的类型（首页/偶数页）。
    """

    __slots__ = ("part", "label", "section_index", "ref_type", "is_header", "is_special")

    def __init__(
        self,
        part,
        label: str,
        section_index: int,
        ref_type: str,
        is_header: bool,
        is_special: bool,
    ):
        self.part = part
        self.label = label
        self.section_index = section_index
        self.ref_type = ref_type
        self.is_header = is_header
        self.is_special = is_special


def _iter_references(sect_pr, tag: str) -> Iterator[Tuple[str, str]]:
    """产出 ``sectPr`` 下某个引用标签的 ``(w_type, r_id)``。

    直接读 XML 而**不碰 ``section.header`` 等代理属性**——
    实测读取那些属性会自动创建不存在的 part，属副作用。

    Args:
        sect_pr: ``<w:sectPr>`` 元素。
        tag: ``w:headerReference`` 或 ``w:footerReference``。

    Yields:
        ``(引用类型, 关系 ID)``。
    """
    for ref in sect_pr.findall(qn(tag)):
        ref_type = ref.get(qn("w:type")) or "default"
        r_id = ref.get(qn("r:id"))
        if r_id:
            yield ref_type, r_id


def collect_header_footer_items(document) -> Tuple[List[HeaderFooterItem], int]:
    """收集文档里**实际存在**的页眉页脚 part（已按 part 去重）。

    只读 ``sectPr`` 里已声明的引用，**不访问代理属性**，
    因此不会凭空创建 first/even 页眉页脚。

    遍历顺序：按节、每节按 default → first → even，
    保证同一文档每次运行的处理顺序一致（可复现）。

    Args:
        document: python-docx 的 ``Document`` 对象。

    Returns:
        ``(待处理项列表, 因 part 重复而跳过的数量)``。
    """
    items: List[HeaderFooterItem] = []
    # 按 part 对象本身去重——不能用 id()，代理回收后 id() 会被复用
    seen_parts = set()
    skipped_duplicates = 0

    document_part = document.part
    # 用 document.sections 取sectPr，而不是 body.iter()：
    # 后者会把段落内的分节符 sectPr 也算进来，节序号可能与用户界面看到的不一致。
    # 实测：给第1节设置首页页眉时，python-docx 会把引用写进body 末尾的 sectPr，
    # 导致 body.iter() 下标与真实节号错位。
    sections = list(document.sections)
    for section_index, section in enumerate(sections, start=1):
        sect_pr = getattr(section, "_sectPr", None)
        if sect_pr is None:
            continue
        for tag, type_map, is_header in (
            ("w:headerReference", _REFERENCE_TYPES, True),
            ("w:footerReference", _FOOTER_REFERENCE_TYPES, False),
        ):
            for ref_type, r_id in _iter_references(sect_pr, tag):
                try:
                    part = document_part.rels[r_id].target_part
                except (KeyError, AttributeError):
                    # 关系缺失或指向内部 part，保守跳过
                    continue

                # 用 part 本身作集合成员（lxml part 可哈希且按身份比较）
                if part in seen_parts:
                    skipped_duplicates += 1
                    continue
                seen_parts.add(part)

                prefix, is_special = type_map.get(ref_type, ("页眉", False))
                items.append(
                    HeaderFooterItem(
                        part=part,
                        label=f"{prefix} 第{section_index}节",
                        section_index=section_index,
                        ref_type=ref_type,
                        is_header=is_header,
                        is_special=is_special,
                    )
                )

    return items, skipped_duplicates


def iter_header_footer_paragraphs(document) -> Iterator[Tuple[Paragraph, str]]:
    """遍历所有页眉页脚里的段落（含其中的表格内段落）。

    Args:
        document: python-docx 的 ``Document`` 对象。

    Yields:
        ``(段落, 位置标签)``。标签形如 ``页眉 第1节``。
    """
    items, _ = collect_header_footer_items(document)
    for item in items:
        element = getattr(item.part, "element", None)
        if element is None:
            continue
        yield from iter_paragraphs_in_container(element, item.label)


def iter_paragraphs_in_container(
    container_element, label: str = ""
) -> Iterator[Tuple[Paragraph, str]]:
    """遍历一个页眉/页脚容器内的段落（含表格内、含嵌套表格）。

    Args:
        container_element: ``<w:hdr>`` / ``<w:ftr>`` 元素。
        label: 位置标签。

    Yields:
        ``(段落, 位置标签)``。
    """
    # 顶层段落（直接挂在 hdr/ftr 下的）
    for p_element in container_element.findall(qn("w:p")):
        yield Paragraph(p_element, container_element), label

    # 表格内段落（含嵌套）：按单元格元素去重，应对合并单元格
    seen_cells = set()
    for tc in container_element.iter(qn("w:tc")):
        if tc in seen_cells:
            continue  # 合并单元格：同一 XML 元素
        seen_cells.add(tc)
        for p_element in tc.findall(qn("w:p")):
            yield Paragraph(p_element, tc), label