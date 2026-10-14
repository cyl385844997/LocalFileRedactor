"""Word(.docx) 测试样例构造器 —— P1 第一版。

用途：让你能**本地打开 Word 亲眼确认**跨run 替换效果与格式保留情况，
而不是只信测试报告。

全部数据为**虚构合成**，不含任何真实个人信息。

运行方式::

    python tests/fixtures/make_docx_samples.py
    # 生成到 samples/ 下，然后用 Word 打开 samples/docx_demo_split_runs.docx

三个核心样例：

* ``docx_demo_split_runs.docx``  手机号被拆在 3 个 run 里（加粗/斜体 / 常规），
  并在同段落放一个"未被涉及"的加粗 run 作为格式对照
* ``docx_demo_single_run.docx``   手机号完整落在单个 run 内（对照组）
* ``docx_demo_complex.docx``      含 ``<w:br/>`` 与 ``<w:tab/>`` 的复杂段落，
  用于验证 br/tab 现在能被正常处理且元素不被破坏
* ``docx_demo_hyperlink.docx``超链接样例：mailto 链接、https 链接、
  内部锚点链接、显示文字含邮箱、手机号跨越链接边界
* ``docx_demo_table.docx``      表格样例：普通表格、合并单元格、
  嵌套表格、单元格内超链接、单元格内域代码
* ``docx_demo_headerfooter.docx`` 页眉页脚样例：多节、链接到前一节、
  首页不同、偶数页不同、页眉内表格与超链接
* ``docx_demo_comment.docx``批注样例：批注含敏感信息、无敏感信息、多段落
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import List

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_BREAK
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# 允许 `python tests/fixtures/make_docx_samples.py` 直接运行：
# 把 tests/ 目录加入 sys.path，才能 `import fixtures.make_samples`。
_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from fixtures.make_samples import (  # noqa: E402
    FAKE_BANK_CARD,
    FAKE_EMAIL,
    FAKE_ID_VALID,
    FAKE_IP,
    FAKE_PHONE_1,
    FAKE_PHONE_2,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = PROJECT_ROOT / "samples"


def _set_split_phones(paragraph, phone: str) -> None:
    """把一个手机号**故意拆成 3 个 run**，并赋予不同格式。

    拆分点刻意选在号码中间（而不是首尾），确保每个 run 都是"部分号码"，
    任何单run 匹配都不可能命中——这正是跨 run 处理要解决的场景。

    格式：第1 段加粗、第 2 段斜体、第 3 段常规。

    Args:
        paragraph: 目标段落。
        phone: 完整手机号（11 位）。
    """
    cut1 = len(phone) // 3
    cut2 = len(phone) * 2 // 3
    part1, part2, part3 = phone[:cut1], phone[cut1:cut2], phone[cut2:]

    run1 = paragraph.add_run(part1)
    run1.bold = True

    run2 = paragraph.add_run(part2)
    run2.italic = True

    run3 = paragraph.add_run(part3)  # 常规格式


def make_split_runs_sample() -> Path:
    """生成"跨 run 手机号"样例——本阶段的核心验收对象。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_split_runs.docx"

    document = Document()

    # 标题
    document.add_heading("Word 跨 run 脱敏样例（合成数据）", level=1)

    # 段落 1：手机号拆成 3 个 run，分别加粗 / 斜体 / 常规
    paragraph = document.add_paragraph()
    paragraph.add_run("联系人手机：")
    _set_split_phones(paragraph, FAKE_PHONE_1)
    tail = paragraph.add_run("（该号码被刻意拆成 3 个 run）")
    tail.font.color.rgb = None  # 不设颜色，保持默认

    # 段落 2：格式对照——一个"不会被命中"的加粗 run，验证其格式保持不变
    paragraph2 = document.add_paragraph()
    marker = paragraph2.add_run("【格式对照组：这段加粗文字不含敏感信息，应保持加粗】")
    marker.bold = True
    marker.font.size = None

    # 段落 3：多个敏感字段 + 其中两个邮箱拆 run
    paragraph3 = document.add_paragraph()
    paragraph3.add_run("邮箱：")
    email = FAKE_EMAIL
    cut = email.index("@")  # 在 @ 处拆开，跨 run 的邮箱
    run_a = paragraph3.add_run(email[:cut])
    run_a.bold = True
    run_b = paragraph3.add_run(email[cut:])
    run_b.underline = True

    # 段落 4：完整落在单个 run 内的多个敏感字段（对照组）
    paragraph4 = document.add_paragraph()
    paragraph4.add_run(
        f"身份证 {FAKE_ID_VALID}，银行卡 {FAKE_BANK_CARD}，服务器 {FAKE_IP}，备用手机 {FAKE_PHONE_2}"
    )

    # 段落 5：无敏感信息，应完全不被改动
    document.add_paragraph("本段落不含任何敏感信息，用于验证未被改动的段落保持原样。")

    document.save(str(path))
    return path


def make_single_run_sample() -> Path:
    """生成"单个 run 内完整手机号"样例，作为跨 run 样例的对照组。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_single_run.docx"

    document = Document()
    document.add_heading("Word 单 run 脱敏样例（对照组）", level=1)

    # 手机号完整落在一个 run 内
    paragraph = document.add_paragraph()
    paragraph.add_run("联系人手机：")
    run = paragraph.add_run(FAKE_PHONE_1)  # 整个号码在同一个 run
    run.bold = True

    document.add_paragraph(f"邮箱 {FAKE_EMAIL}，服务器 {FAKE_IP}")

    document.save(str(path))
    return path


def make_complex_sample() -> Path:
    """生成含 ``<w:br/>`` 与 ``<w:tab/>`` 的复杂段落样例。

    这类段落从 P1 优先级 1 起**正常处理**（br/tab 各占 1 个占位字符对齐偏移，
    替换时元素原样保留），本样例用于验证该行为。
    """
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_complex.docx"

    document = Document()
    document.add_heading("Word 复杂结构样例（本版应跳过）", level=1)

    # 含换行符的段落：手机号前后有 <w:br/>
    paragraph = document.add_paragraph()
    paragraph.add_run("第一行文字")
    paragraph.add_run().add_break(WD_BREAK.LINE)
    paragraph.add_run(f"换行符后面的手机 {FAKE_PHONE_1}")
    paragraph.add_run().add_break(WD_BREAK.LINE)
    paragraph.add_run("最后一行")

    # 含制表符的段落
    paragraph2 = document.add_paragraph()
    paragraph2.add_run("制表符前")
    paragraph2.add_run().add_tab()
    paragraph2.add_run(f"制表符后手机 {FAKE_PHONE_2}")

    # 纯 run 段落：这一段应当正常脱敏
    document.add_paragraph(f"这一段是纯 run，应当正常脱敏：手机 {FAKE_PHONE_1}")

    document.save(str(path))
    return path


def add_hyperlink(paragraph, url: str, text: str, anchor: str = None) -> str:
    """给段落插入一个超链接，返回其rId。

    python-docx 没有内置 API，这里手工构造 ``<w:hyperlink>``。
    注意 Word 的存储方式：**正文只存显示文字 + rId，真实 URL 存在 rels 文件里**。

    Args:
        paragraph: 目标段落。
        url: 外部链接地址；内部锚点链接传anchor。
        text: 显示文字。
        anchor: 内部锚点名（非空则创建内部链接，不产生 rels 条目）。

    Returns:
        外部链接的 rId；内部链接返回空串。
    """
    hyperlink = OxmlElement("w:hyperlink")
    if anchor:
        hyperlink.set(qn("w:anchor"), anchor)
        r_id = ""
    else:
        r_id = paragraph.part.relate_to(
            url,
            RT.HYPERLINK,
            is_external=True,
        )
        hyperlink.set(qn("r:id"), r_id)

    run = OxmlElement("w:r")
    text_element = OxmlElement("w:t")
    text_element.text = text
    run.append(text_element)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)
    return r_id


def make_hyperlink_sample() -> Path:
    """生成超链接样例（优先级 2）。

    覆盖四种情形：
        1. mailto 链接（URL 含邮箱，需改写 Target）
        2. 普通 https 链接（URL 不含敏感信息，应原样保留）
        3. 内部锚点链接（无外部 URL，只处理显示文字）
        4. 链接显示文字本身含邮箱（正文脱敏后链接仍存在）
    """
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_hyperlink.docx"

    document = Document()
    document.add_heading("Word 超链接脱敏样例（合成数据）", level=1)

    # 情形 1：mailto 链接，URL 里含邮箱
    paragraph = document.add_paragraph()
    paragraph.add_run("联系方式：")
    add_hyperlink(paragraph, f"mailto:{FAKE_EMAIL}", "点击发邮件")

    # 情形 2：普通 https 链接，URL 不含敏感信息
    paragraph2 = document.add_paragraph()
    paragraph2.add_run("参考资料：")
    add_hyperlink(paragraph2, "https://example.com/contact", "访问联系页")

    # 情形 3：内部锚点链接（无外部 URL）
    paragraph3 = document.add_paragraph()
    add_hyperlink(paragraph3, "", "跳转到附录", anchor="appendix1")

    # 情形 4：显示文字本身含邮箱（正文脱敏，链接结构保留）
    paragraph4 = document.add_paragraph()
    add_hyperlink(paragraph4, "https://example.com/mail", FAKE_EMAIL)

    # 情形 5：手机号跨越"普通run / 链接文字"边界（验证偏移映射不漏）
    paragraph5 = document.add_paragraph()
    paragraph5.add_run("电话 1380013")
    add_hyperlink(paragraph5, "https://example.com/x", "8000 尾号")

    document.save(str(path))
    return path


def make_table_sample() -> Path:
    """生成表格样例（优先级 3）。

    覆盖五种情形：
        1. 普通表格（多行多列，含多类敏感信息）
        2. 合并单元格（同一 XML 元素被多个坐标指向）
        3. 嵌套表格（外格内再放一个表）
        4. 单元格内含超链接
        5. 单元格内含域代码（本版应跳过该段落）
    """
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_table.docx"

    document = Document()
    document.add_heading("Word 表格脱敏样例（合成数据）", level=1)

    # 情形 1：普通表格
    table = document.add_table(rows=2, cols=3)
    table.cell(0, 0).text = f"联系人 {FAKE_PHONE_1}"
    table.cell(0, 1).text = f"邮箱 {FAKE_EMAIL}"
    table.cell(0, 2).text = "无敏感信息"
    table.cell(1, 0).text = f"身份证 {FAKE_ID_VALID}"
    table.cell(1, 1).text = f"银行卡 {FAKE_BANK_CARD}"
    table.cell(1, 2).text = f"服务器 {FAKE_IP}"
    # 在已有单元格里再加一段，验证"单元格内多段落"也被处理
    # 注意：要用 add_paragraph 的那个单元格不能是合并格，
    # 否则 python-docx 的 cell 代理会让相邻坐标指向同一元素、内容互相覆盖。
    table.cell(1, 2).add_paragraph(f"第二段 {FAKE_PHONE_2}")

    # 情形 2：合并单元格
    merged = document.add_table(rows=2, cols=2)
    merged.cell(0, 0).merge(merged.cell(0, 1))
    merged.cell(0, 0).text = f"合并单元格 {FAKE_BANK_CARD}"
    merged.cell(1, 0).text = "A"
    merged.cell(1, 1).text = "B"

    # 情形 3：嵌套表格
    nested = document.add_table(rows=1, cols=1)
    outer_cell = nested.cell(0, 0)
    outer_cell.text = "外层单元格"
    inner = outer_cell.add_table(rows=1, cols=1)
    inner.cell(0, 0).text = f"嵌套表内 {FAKE_IP}"

    # 情形 4：单元格内超链接
    linked = document.add_table(rows=1, cols=1)
    add_hyperlink(linked.cell(0, 0).paragraphs[0], f"mailto:{FAKE_EMAIL}", "邮件链接")

    # 情形 5：单元格内含域代码（本版该段落应被跳过）
    field = document.add_table(rows=1, cols=1)
    field_paragraph = field.cell(0, 0).paragraphs[0]
    field_paragraph.add_run("页码附近")
    run_begin = OxmlElement("w:r")
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    run_begin.append(fld_begin)
    field_paragraph._p.append(run_begin)
    run_instr = OxmlElement("w:r")
    instr = OxmlElement("w:instrText")
    instr.text = " PAGE "
    run_instr.append(instr)
    field_paragraph._p.append(run_instr)

    document.save(str(path))
    return path


def make_headerfooter_sample() -> Path:
    """生成页眉页脚样例（优先级 4）。

    覆盖六种情形：
        1. 第1节的默认页眉/ 页脚
        2. 第2节独立的页眉/页脚
        3. 第3节"链接到前一节"（内容实际存放在第2节）
        4. 首页不同（``first_page_header``）
        5. 偶数页不同（``even_page_header``）
        6. 页眉内的表格与超链接
    """
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_headerfooter.docx"

    document = Document()

    # 第1节：默认页眉页脚
    section1 = document.sections[0]
    section1.header.is_linked_to_previous = False
    section1.header.paragraphs[0].text = f"第1节页眉电话 {FAKE_PHONE_1}"
    section1.footer.is_linked_to_previous = False
    section1.footer.paragraphs[0].text = f"第1节页脚邮箱 {FAKE_EMAIL}"

    # 第2节：独立页眉页脚
    section2 = document.add_section(WD_SECTION.NEW_PAGE)
    section2.header.is_linked_to_previous = False
    section2.header.paragraphs[0].text = f"第2节页眉身份证 {FAKE_ID_VALID}"
    section2.footer.is_linked_to_previous = False
    section2.footer.paragraphs[0].text = "第2节页脚无敏感信息"

    # 第3节：保持"链接到前一节"，不单独设置内容
    document.add_section(WD_SECTION.NEW_PAGE)

    # 首页不同
    # 注意：python-docx 会把首页/偶数页引用写进 body 末尾的 sectPr，
    # 所以这里设置在第1节，实际存储位置可能显示为最后一节——
    # 这不影响功能，标签按真实 sectPr 位置生成。
    section1.different_first_page_header_footer = True
    section1.first_page_header.is_linked_to_previous = False
    section1.first_page_header.paragraphs[0].text = f"首页页眉 {FAKE_IP}"

    # 偶数页不同
    section1.even_page_header.is_linked_to_previous = False
    section1.even_page_header.paragraphs[0].text = f"偶数页页眉 {FAKE_BANK_CARD}"

    # 页眉内放表格
    header_table = section1.header.add_table(rows=1, cols=1, width=2000)
    header_table.cell(0, 0).text = f"页眉表格内 {FAKE_PHONE_2}"

    # 页眉内放超链接
    link_paragraph = section1.header.add_paragraph()
    add_hyperlink(link_paragraph, f"mailto:{FAKE_EMAIL}", "页眉邮件链接")

    document.add_paragraph("正文段落")
    document.save(str(path))
    return path


def attach_comments(document, comments) -> None:
    """给文档挂上批注（python-docx 无此 API，手工构造）。

    Args:
        document: 目标 Document。
        comments: 列表，每项 ``(comment_id, author, initials, text)``。
    """
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    entries = []
    for comment_id, author, initials, text in comments:
        entries.append(
            f'<w:comment w:id="{comment_id}" w:author="{author}" '
            f'w:initials="{initials}" w:date="2026-01-01T00:00:00Z">'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:comment>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main">' + "".join(entries) + "</w:comments>"
    )
    part = Part(
        PackURI("/word/comments.xml"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        xml.encode("utf-8"),
        document.part.package,
    )
    document.part.relate_to(part, RT.COMMENTS)

    # 正文侧建立引用锚点
    paragraph = document.add_paragraph()
    for comment_id, *_ in comments:
        start = OxmlElement("w:commentRangeStart")
        start.set(qn("w:id"), str(comment_id))
        paragraph._p.insert(0, start)

    for comment_id, *_ in comments:
        end = OxmlElement("w:commentRangeEnd")
        end.set(qn("w:id"), str(comment_id))
        paragraph._p.append(end)
        run = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), "CommentReference")
        rpr.append(style)
        run.append(rpr)
        ref = OxmlElement("w:commentReference")
        ref.set(qn("w:id"), str(comment_id))
        run.append(ref)
        paragraph._p.append(run)


def make_comment_sample() -> Path:
    """生成批注样例（优先级 5）。

    覆盖三种情形：
        1. 批注正文含敏感信息（应脱敏）
        2. 批注正文无敏感信息（不应改动）
        3. 同一条批注含多个段落（验证逐段处理）
    """
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = SAMPLES_DIR / "docx_demo_comment.docx"

    document = Document()
    document.add_heading("Word 批注脱敏样例（合成数据）", level=1)
    document.add_paragraph("正文段落，批注挂在这一段。")

    attach_comments(
        document,
        [
            (1, "张三", "ZS", f"批注含手机号 {FAKE_PHONE_1} 和邮箱 {FAKE_EMAIL}"),
            (2, "李四", "LS", "这条批注不含任何敏感信息"),
            (3, "王五", "WW", f"身份证 {FAKE_ID_VALID}"),
        ],
    )

    document.save(str(path))
    return path


def build_all() -> List[Path]:
    """生成全部 docx 样例。"""
    return [
        make_split_runs_sample(),
        make_single_run_sample(),
        make_complex_sample(),
        make_hyperlink_sample(),
        make_table_sample(),
        make_headerfooter_sample(),
        make_comment_sample(),
    ]


def clean() -> None:
    """删除生成的样例。"""
    if SAMPLES_DIR.exists():
        shutil.rmtree(SAMPLES_DIR)


if __name__ == "__main__":
    for created in build_all():
        print(f"已生成：{created}")
    print()
    print("请用 Word 打开 samples/docx_demo_split_runs.docx 确认：")
    print("  1) 原手机号已变为 [手机号]")
    print("  2) 段落 2 的加粗对照文字仍是加粗")
    print("  3) 未含敏感信息的段落完全没变")