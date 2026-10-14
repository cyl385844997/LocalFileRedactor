"""docx 超链接 URL 脱敏（优先级 2）。

为什么需要单独一个模块
----------------------
Word 把超链接拆成两处存放：

* ``word/document.xml``：``<w:hyperlink r:id="rId9">`` —— 只有显示文字 + 关系 ID
* ``word/_rels/document.xml.rels``：``rId9 -> Target="https://..."`` —— 真实 URL

**URL 不在正文里**，所以正文正则永远扫不到它。``mailto:zhangsan@company.com``
会原封不动躺在 rels 文件中——这是本模块要堵的盲区。

第一版范围（重要）
------------------
**只处理 ``mailto:`` 链接。**

理由：``mailto:`` 的 Target 本身就是邮箱，与现有 ``[邮箱]`` 规则同源，
命中后可直接复用占位符，语义无歧义。而 URL 路径里的其他内容
（query 参数 token、路径里的工号等）没有统一的识别规则，
贸然匹配容易误判、破坏文档。这些列入 known limitations。

处理方式
--------
命中 ``mailto:`` 时**改写 Target 内容**（如 ``mailto:[邮箱]``），
**不删除 Relationship 条目**——删除会让 ``document.xml`` 里的 ``r:id``
变成悬空引用，属于"Word 能容忍但结构已损坏"的坏数据。

因为 python-docx 的 ``_Relationship.target_ref`` 是只读属性，
实现上采用「删旧条目 + 用相同 rId 新建」的方式，
保证 ``document.xml`` 里的引用始终有效（已实测验证）。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from docx.opc.constants import RELATIONSHIP_TYPE as RT

from redactor.core.engine import RedactionEngine

#: mailto: 链接的协议前缀（大小写不敏感）
_MAILTO_PREFIX = "mailto:"

#: 拆分 mailto Target 为"地址"与"查询/片段"两部分。
#: 只拆第一个 ? 或 #，其余内容（subject、body 等）**原样保留**——
#: 早期版本只取 ? 之前的部分，替换时把后半段整个丢了，等于静默删除用户数据。
_MAILTO_PARTS = re.compile(r"^(mailto:)([^?#]*)(.*)$", re.IGNORECASE | re.DOTALL)


def is_mailto(target: str) -> bool:
    """判断 Target 是否为 mailto 链接（大小写不敏感）。

    注意：仅有 ``mailto:`` 而没有地址（如 ``mailto:``）也算协议匹配，
    真正的"要不要处理"由 :func:`build_redacted_target` 判断。
    """
    return bool(target) and target.strip().lower().startswith(_MAILTO_PREFIX)


def build_redacted_target(target: str, engine: RedactionEngine) -> Optional[str]:
    """对 mailto 链接的 Target 做脱敏，返回新字符串。

    处理规则：
        * ``mailto:`` 协议头**保留**（可读性，且不含隐私）
        * 后面的邮箱走引擎的 ``[邮箱]`` 规则替换
        * ``?subject=`` / ``#fragment`` 之后的内容**原样保留**
          （本版不处理其内容，但**绝不丢弃**——丢弃等于静默删除用户数据）
        * 邮箱命中时返回新 Target；未命中返回 ``None``（表示无需改动）

    Args:
        target: 原始 Target，如 ``mailto:zhangsan@company.com``。
        engine: 脱敏引擎（复用其邮箱规则与占位符）。

    Returns:
        脱敏后的 Target；邮箱未命中时返回 ``None``。
    """
    match = _MAILTO_PARTS.match(target.strip())
    if not match:
        return None

    scheme, address, remainder = match.group(1), match.group(2), match.group(3)
    if not address:
        return None

    hits = engine.scan(address)
    if not hits:
        return None

    redacted, _ = engine.redact(address)
    if redacted == address:
        return None

    # Target 是 XML 属性值，占位符含中文与方括号，必须做 URL 编码
    # （实测用 %5B%E9%82%AE%E7%AE%B1%5D 表示 "[邮箱]" 可被 Word 正常读取）
    return f"{scheme}{quote(redacted, safe='@.+-_')}{remainder}"


def redact_hyperlink_targets(
    part,
    engine: RedactionEngine,
    *,
    dry_run: bool = False,
) -> List[Tuple[str, str, str]]:
    """脱敏文档部件里的 mailto 超链接 Target。

    Args:
        part: docx 的文档部件（``document.part``）。
        engine: 脱敏引擎。
        dry_run: True 时只统计不改动。

    Returns:
        命中列表，每项为 ``(rId, 原 Target, 新 Target)``。
        **不含敏感内容以外的信息**；原 Target 可能含邮箱，
        因此调用方**不得**把该列表直接写进报告（报告只应记录 rId 与命中规则）。
    """
    hits: List[Tuple[str, str, str]] = []

    # 只处理外部超链接关系（内部锚点用 w:anchor，不在 rels 里）
    for rid, rel in list(part.rels.items()):
        if rel.reltype != RT.HYPERLINK or not rel.is_external:
            continue
        target = rel.target_ref
        if not is_mailto(target):
            continue

        new_target = build_redacted_target(target, engine)
        if new_target is None:
            continue

        hits.append((rid, target, new_target))
        if dry_run:
            continue

        # target_ref 只读 -> 删旧建新，并沿用同一个 rId，
        # 保证 document.xml 里的 r:id 引用不悬空（实测 Word 可正常打开）
        del part.rels[rid]
        part.rels.add_relationship(RT.HYPERLINK, new_target, rid, is_external=True)

    return hits


def iter_hyperlink_elements(paragraph_element):
    """产出段落内的 ``<w:hyperlink>`` 元素。

    Args:
        paragraph_element: ``<w:p>`` 元素。

    Yields:
        ``<w:hyperlink>`` 元素。
    """
    return paragraph_element.iter(qn_hyperlink())


def qn_hyperlink() -> str:
    """返回 ``w:hyperlink`` 的限定标签名。"""
    from docx.oxml.ns import qn

    return qn("w:hyperlink")