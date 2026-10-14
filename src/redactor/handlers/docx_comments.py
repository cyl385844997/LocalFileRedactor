"""docx 批注遍历（优先级 5）。

存储结构（实测确认）
--------------------
批注存在独立 part ``word/comments.xml`` 里::

    <w:comments>
      <w:comment w:id="1" w:author="张三" w:initials="ZS" w:date="...">
        <w:p><w:r><w:t>批注内容</w:t></w:r></w:p>
      </w:comment>
    </w:comments>

正文侧通过 ``commentRangeStart`` / ``commentRangeEnd`` / ``commentReference``
三个元素的 ``w:id`` 与之关联，全部在 ``document.xml`` 里。

关键设计
--------
**只改 ``comments.xml`` 里的 ``<w:t>`` 文本，绝不碰任何 id**。
实测证明这样处理后 Word 能正常打开、批注数不变、作者名保留。

作者名不脱敏
------------
``w:author`` / ``w:initials`` **保持原样**。理由：

* 本工具不识别"人名"——纯正则做不到，强行匹配会误伤（已写入 README 能力边界）
* 作者名属文档元数据，与"手机号/银行卡"这类泄露即造成损害的信息不同级
* 批注是协作记录，抹掉作者会让文档失去可追溯性

但存在盲区：若有人用**手机号或邮箱当署名**，本版不会处理。
已写入 known limitations。
"""

from __future__ import annotations

from typing import Iterator, List, Tuple

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph


def get_comments_element(document):
    """取出 ``comments.xml`` 的根元素，若不存在则返回 ``None``。

    只从 ``document.part.rels`` 里查真实存在的 COMMENTS 关系，
    **不访问可能触发创建的代理属性**（页眉页脚已实测过该副作用）。

    Args:
        document: python-docx 的 ``Document`` 对象。

    Returns:
        ``<w:comments>`` 元素；文档无批注时返回 ``None``。
    """
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    for rel in document.part.rels.values():
        if rel.reltype != RT.COMMENTS:
            continue
        part = getattr(rel, "target_part", None)
        if part is None:
            continue
        element = getattr(part, "element", None)
        if element is not None:
            return element
    return None


def iter_comment_paragraphs(document) -> Iterator[Tuple[Paragraph, str]]:
    """遍历所有批注里的段落。

    位置标签用 ``批注 #<w:id>``——用**文档内部唯一 id** 而非遍历序号，
    因为部分批注被删除时序号会漂移，id 始终稳定。

    Args:
        document: python-docx 的 ``Document`` 对象。

    Yields:
        ``(段落, 位置标签)``。标签形如 ``批注 #1``。
    """
    root = get_comments_element(document)
    if root is None:
        return

    for comment in root.findall(qn("w:comment")):
        comment_id = comment.get(qn("w:id")) or "?"
        label = f"批注 #{comment_id}"
        # 批注正文段落直接挂在 <w:comment> 下
        for p_element in comment.findall(qn("w:p")):
            yield Paragraph(p_element, comment), label


def count_comments(document) -> int:
    """统计批注条数（用于报告汇总）。"""
    root = get_comments_element(document)
    if root is None:
        return 0
    return len(root.findall(qn("w:comment")))
