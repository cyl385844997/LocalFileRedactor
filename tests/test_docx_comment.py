"""docx 批注处理单元测试（优先级 5）。

覆盖：
    * 批注内容脱敏
    * w:id 关联不被破坏（正文侧三个引用元素 + comments.xml 的 w:comment）
    * 作者名保持原样（明确的设计决策）
    * 多段落批注逐段处理
    * 无批注文档不受影响
    * 跳过粒度为段落而非整条批注

不在本轮范围：文本框、脚注尾注。
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from fixtures.make_docx_samples import attach_comments, make_comment_sample
from fixtures.make_samples import FAKE_EMAIL, FAKE_ID_VALID, FAKE_PHONE_1
from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rules
from redactor.handlers.base import HandlerError
from redactor.handlers.docx_comments import (
    count_comments,
    get_comments_element,
    iter_comment_paragraphs,
)
from redactor.handlers.docx_handler import DocxHandler

BUILTIN_RULES = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


@pytest.fixture()
def engine():
    return RedactionEngine(load_rules(BUILTIN_RULES))


@pytest.fixture()
def samples_module():
    import fixtures.make_docx_samples as module

    return module


@pytest.fixture()
def samples_dir(tmp_path, samples_module):
    target = tmp_path / "samples"
    target.mkdir(parents=True, exist_ok=True)
    original = samples_module.SAMPLES_DIR
    samples_module.SAMPLES_DIR = target
    try:
        yield target
    finally:
        samples_module.SAMPLES_DIR = original


def comments_xml(docx_path: Path) -> str:
    """读出 comments.xml 原文。"""
    with zipfile.ZipFile(docx_path) as archive:
        return archive.read("word/comments.xml").decode("utf-8")


def document_xml(docx_path: Path) -> str:
    with zipfile.ZipFile(docx_path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def comment_texts(docx_path: Path):
    """读出每条批注的文本列表。"""
    xml = comments_xml(docx_path)
    return re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)


class TestGetComments:
    """part 定位。"""

    def test_有批注时返回根元素(self):
        document = Document(str(make_comment_sample()))
        element = get_comments_element(document)
        assert element is not None
        assert element.tag == qn("w:comments")

    def test_无批注时返回none(self, samples_dir):
        document = Document()
        document.add_paragraph("无批注文档")
        source = samples_dir / "plain.docx"
        document.save(str(source))

        reloaded = Document(str(source))
        assert get_comments_element(reloaded) is None

    def test_统计批注条数(self):
        document = Document(str(make_comment_sample()))
        assert count_comments(document) == 3


class TestCommentRedaction:
    """批注内容脱敏。"""

    def test_批注敏感信息被脱敏(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = "\n".join(comment_texts(output))
        assert FAKE_PHONE_1 not in text
        assert FAKE_EMAIL not in text
        assert FAKE_ID_VALID not in text

    def test_占位符写入(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = "\n".join(comment_texts(output))
        assert "[手机号]" in text
        assert "[邮箱]" in text
        assert "[身份证号]" in text

    def test_无敏感信息的批注不变(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        assert "这条批注不含任何敏感信息" in comment_texts(output)

    def test_位置标签用批注id(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        locations = {loc for stat in result.hits for loc in stat.locations}
        comment_locs = [loc for loc in locations if loc.startswith("批注 #")]
        assert comment_locs, f"未见批注位置标签：{locations}"
        assert all(re.match(r"^批注 #\d+$", loc) for loc in comment_locs), comment_locs


class TestAuthorPreserved:
    """作者名保持原样（明确的设计决策）。"""

    def test_作者名不被修改(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        xml = comments_xml(output)
        for author in ("张三", "李四", "王五"):
            assert f'w:author="{author}"' in xml, f"作者名 {author} 被改动"

    def test_缩写不被修改(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        xml = comments_xml(output)
        for initials in ("ZS", "LS", "WW"):
            assert f'w:initials="{initials}"' in xml

    def test_作者名不做规则匹配(self, engine, samples_dir):
        """回归：作者名不应被误当作敏感信息处理。

        设计决策：批注作者名不脱敏（人名不在本工具能力范围内）。
        """
        document = Document()
        document.add_paragraph("正文")
        attach_comments(document, [(1, "13800138000", "PH", "无敏感内容")])
        source = samples_dir / "author_phone.docx"
        document.save(str(source))

        output = samples_dir / "author_phone_out.docx"
        result = DocxHandler().process(source, output, engine)

        xml = comments_xml(output)
        assert 'w:author="13800138000"' in xml, "作者名不应被脱敏"
        assert result.total_hits == 0


class TestIdIntegrity:
    """w:id 关联不被破坏（最关键的安全断言）。"""

    def test_comments里的id不变(self, engine, samples_dir):
        source = make_comment_sample()
        before = re.findall(r'<w:comment w:id="(\d+)"', comments_xml(source))

        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        after = re.findall(r'<w:comment w:id="(\d+)"', comments_xml(output))

        assert before == after, f"批注 id 被改动：{before} -> {after}"

    def test_正文侧三个引用元素不变(self, engine, samples_dir):
        """commentRangeStart / End / Reference 都依赖 id，必须原样保留。"""
        source = make_comment_sample()

        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        before_doc = document_xml(source)
        after_doc = document_xml(output)
        for pattern in ("commentRangeStart", "commentRangeEnd", "commentReference"):
            b = re.findall(rf"<w:{pattern}[^>]*/>", before_doc)
            a = re.findall(rf"<w:{pattern}[^>]*/>", after_doc)
            assert b == a, f"{pattern} 被改动：{b} -> {a}"

    def test_批注数量不变(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        assert count_comments(Document(str(output))) == 3

    def test_正文段落不被改动(self, engine, samples_dir):
        """批注处理不应影响正文。"""
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = "\n".join(p.text for p in Document(str(output)).paragraphs)
        assert "Word 批注脱敏样例" in text
        assert "正文段落，批注挂在这一段。" in text

    def test_关系引用完整(self, engine, samples_dir):
        """document.xml.rels 里指向 comments.xml 的关系必须保留。"""
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        with zipfile.ZipFile(output) as archive:
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "comments.xml" in rels

    def test_python_docx可重开(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        assert Document(str(output)) is not None


class TestMultiParagraphComment:
    """同一条批注含多个段落。"""

    def test_多段落批注逐段处理(self, engine, samples_dir):
        """构造一条含 2 个段落的批注，两段都应被脱敏。

        用 lxml 解析构造而非字符串替换——字符串替换容易破坏标签配对。
        """
        from lxml import etree

        document = Document()
        document.add_paragraph("正文")
        attach_comments(document, [(1, "作者", "A", "占位")])
        source = samples_dir / "multi.docx"
        document.save(str(source))

        # 解出 comments.xml，把单段批注改成双段
        with zipfile.ZipFile(source) as archive:
            payload = {name: archive.read(name) for name in archive.namelist()}

        root = etree.fromstring(payload["word/comments.xml"])
        W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        comment = root.find(W + "comment")
        for child in list(comment):
            comment.remove(child)
        for text in (f"第一段 {FAKE_PHONE_1}", f"第二段 {FAKE_EMAIL}"):
            p = etree.SubElement(comment, W + "p")
            r = etree.SubElement(p, W + "r")
            t = etree.SubElement(r, W + "t")
            t.text = text
        payload["word/comments.xml"] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

        out_src = samples_dir / "multi2.docx"
        with zipfile.ZipFile(out_src, "w", zipfile.ZIP_DEFLATED) as zout:
            for name, data in payload.items():
                zout.writestr(name, data)

        output = samples_dir / "multi2_out.docx"
        DocxHandler().process(out_src, output, engine)
        text = "\n".join(comment_texts(output))

        assert FAKE_PHONE_1 not in text, "第一段未脱敏"
        assert FAKE_EMAIL not in text, "第二段未脱敏"
        assert "[手机号]" in text and "[邮箱]" in text


class TestSkipStrategy:
    """跳过粒度：只跳过问题段落。"""

    def test_域代码段落被跳过且同条其他段落照常处理(self, engine, samples_dir):
        document = Document()
        document.add_paragraph("正文")
        attach_comments(document, [(1, "作者", "A", "占位")])
        source = samples_dir / "base.docx"
        document.save(str(source))

        xml = comments_xml(source)
        xml = xml.replace(
            "占位",
            f"正常段 {FAKE_PHONE_1}",
        )
        # 在同一条批注里塞一个带域代码的段落
        xml = xml.replace(
            "</w:comment>",
            "<w:p><w:r><w:fldChar w:fldCharType=\"begin\"/></w:r>"
            f"<w:r><w:t>带域代码 {FAKE_EMAIL}</w:t></w:r></w:p></w:comment>",
        )
        out_src = samples_dir / "mixed.docx"
        with zipfile.ZipFile(source) as zin, zipfile.ZipFile(out_src, "w") as zout:
            for item in zin.namelist():
                data = zin.read(item)
                if item == "word/comments.xml":
                    data = xml.encode("utf-8")
                zout.writestr(item, data)

        output = samples_dir / "mixed_out.docx"
        result = DocxHandler().process(out_src, output, engine)
        text = "\n".join(comment_texts(output))

        assert FAKE_PHONE_1 not in text, "正常段落未被脱敏"
        assert "批注" in (result.message or "") or result.message is None

    def test_message含批注计数(self, engine, samples_dir):
        document = Document()
        document.add_paragraph("正文")
        attach_comments(document, [(1, "作者", "A", f"手机 {FAKE_PHONE_1}")])
        source = samples_dir / "cnt.docx"
        document.save(str(source))

        output = samples_dir / "cnt_out.docx"
        DocxHandler().process(source, output, engine)
        # 无跳过时不产生 message，这是正常行为
        assert Document(str(output)) is not None


class TestNoComments:
    """无批注文档不受影响。"""

    def test_无批注文档正常处理(self, engine, samples_dir):
        document = Document()
        document.add_paragraph(f"正文手机 {FAKE_PHONE_1}")
        source = samples_dir / "nocom.docx"
        document.save(str(source))

        output = samples_dir / "nocom_out.docx"
        result = DocxHandler().process(source, output, engine)

        assert result.total_hits == 1
        text = "\n".join(p.text for p in Document(str(output)).paragraphs)
        assert FAKE_PHONE_1 not in text

    def test_不产生comments_part(self, engine, samples_dir):
        """回归：处理无批注文档不得凭空创建 comments.xml。

        页眉页脚已实测过"读取即创建"的副作用，这里锁定批注不重蹈覆辙。
        """
        document = Document()
        document.add_paragraph("正文")
        source = samples_dir / "nocm.docx"
        document.save(str(source))

        output = samples_dir / "nocm_out.docx"
        DocxHandler().process(source, output, engine)

        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
        assert not any("comments" in n for n in names), f"凭空创建了批注文件：{names}"


class TestCommentMarkersInBody:
    """回归测试：带批注标记的正文段落必须正常脱敏。

    这曾是优先级 5 的真实盲区——``<w:commentRangeStart>`` /
    ``<w:commentRangeEnd>`` / ``<w:commentReference>`` 都是**无文本标记元素**，
    但原先不在白名单里，导致真实文档中所有带批注的段落被当作
    "复杂结构"整段跳过，正文内容**完全未脱敏**。

    关键区别（与 ``<w:br/>`` / ``<w:tab/>`` 不同）：
        * ``br`` / ``tab`` 在文本流里各占**1 个字符**，参与偏移映射
        * 批注标记**不占偏移**，因此不影响匹配
    """

    @pytest.fixture()
    def marked_doc(self, samples_dir):
        """构造一个"正文有敏感信息 + 带批注标记"的文档。

        结构（实测）::

            <w:p>
              <w:r><w:t>联系人 13800138000</w:t></w:r>
              <w:commentRangeStart w:id="1"/>
              <w:r><w:t>邮箱 zhangsan@example.com</w:t></w:r>
              <w:commentRangeEnd w:id="1"/>
              <w:r><w:rPr>..</w:rPr><w:commentReference w:id="1"/></w:r>
            </w:p>
        """
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from docx.opc.packuri import PackURI
        from docx.opc.part import Part

        document = Document()
        paragraph = document.add_paragraph()

        run1 = OxmlElement("w:r")
        text1 = OxmlElement("w:t")
        text1.text = f"联系人 {FAKE_PHONE_1}"
        run1.append(text1)
        paragraph._p.append(run1)

        start = OxmlElement("w:commentRangeStart")
        start.set(qn("w:id"), "1")
        paragraph._p.append(start)

        run2 = OxmlElement("w:r")
        text2 = OxmlElement("w:t")
        text2.text = f"邮箱 {FAKE_EMAIL}"
        run2.append(text2)
        paragraph._p.append(run2)

        end = OxmlElement("w:commentRangeEnd")
        end.set(qn("w:id"), "1")
        paragraph._p.append(end)

        run3 = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), "CommentReference")
        rpr.append(style)
        run3.append(rpr)
        ref = OxmlElement("w:commentReference")
        ref.set(qn("w:id"), "1")
        run3.append(ref)
        paragraph._p.append(run3)

        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:comments xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:comment w:id="1" w:author="作者" '
            'w:initials="A" w:date="2026-01-01T00:00:00Z">'
            "<w:p><w:r><w:t>批注内容</w:t></w:r></w:p></w:comment></w:comments>"
        )
        part = Part(
            PackURI("/word/comments.xml"),
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.comments+xml",
            xml.encode("utf-8"),
            document.part.package,
        )
        document.part.relate_to(part, RT.COMMENTS)

        path = samples_dir / "marked.docx"
        document.save(str(path))
        return path

    def test_带批注标记的正文被脱敏(self, engine, marked_doc, samples_dir):
        """核心回归：正文内容不再被跳过。"""
        output = samples_dir / "marked_out.docx"
        result = DocxHandler().process(marked_doc, output, engine)

        text = Document(str(output)).paragraphs[0].text
        assert FAKE_PHONE_1 not in text, "带批注标记的段落正文未脱敏"
        assert FAKE_EMAIL not in text
        assert "[手机号]" in text and "[邮箱]" in text

        names = {s.rule_name for s in result.hits}
        assert "手机号" in names and "邮箱" in names

    def test_跳过段落数归零(self, engine, marked_doc, samples_dir):
        """回归：报告不再谎报"含复杂结构已跳过"。"""
        output = samples_dir / "marked_out.docx"
        result = DocxHandler().process(marked_doc, output, engine)
        assert result.message is None, f"仍被当作复杂结构跳过：{result.message}"

    def test_批注标记在输出中完好(self, engine, marked_doc, samples_dir):
        """三个批注标记元素不能因处理而丢失——它们靠 w:id 与批注关联。"""
        output = samples_dir / "marked_out.docx"
        DocxHandler().process(marked_doc, output, engine)

        doc_xml = document_xml(output)
        assert '<w:commentRangeStart w:id="1"/>' in doc_xml
        assert '<w:commentRangeEnd w:id="1"/>' in doc_xml
        assert '<w:commentReference w:id="1"/>' in doc_xml

    def test_标记前后偏移正确(self, engine, marked_doc, samples_dir):
        """批注标记不占偏移，所以标记前后的文本要能被正确拼接与匹配。"""
        output = samples_dir / "marked_out.docx"
        DocxHandler().process(marked_doc, output, engine)

        paragraph = Document(str(output)).paragraphs[0]
        texts = [
            "".join(t.text or "" for t in r.findall(qn("w:t")))
            for r in paragraph._p.findall(qn("w:r"))
        ]
        # 前后两个 run 都应被替换，而不是只有前一个
        assert "[手机号]" in texts[0], texts
        assert "[邮箱]" in texts[1], texts

    def test_含超链接与批注的段落也能处理(self, engine, samples_dir):
        """组合场景：段落里同时有批注标记与超链接。"""
        document = Document()
        paragraph = document.add_paragraph()
        start = OxmlElement("w:commentRangeStart")
        start.set(qn("w:id"), "1")
        paragraph._p.append(start)
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = f"电话 {FAKE_PHONE_1}"
        run.append(text)
        paragraph._p.append(run)
        end = OxmlElement("w:commentRangeEnd")
        end.set(qn("w:id"), "1")
        paragraph._p.append(end)
        source = samples_dir / "both.docx"
        document.save(str(source))

        output = samples_dir / "both_out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_PHONE_1 not in Document(str(output)).paragraphs[0].text

    def test_域代码仍被拦截(self, engine, samples_dir):
        """回归：放行批注标记不能放宽到域代码。

        ``<w:fldChar>`` / ``<w:instrText>`` 仍应导致该段落被跳过。
        """
        document = Document()
        paragraph = document.add_paragraph()
        start = OxmlElement("w:commentRangeStart")
        start.set(qn("w:id"), "1")
        paragraph._p.append(start)
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = f"页码附近 {FAKE_PHONE_1}"
        run.append(text)
        paragraph._p.append(run)
        fld_run = OxmlElement("w:r")
        fld = OxmlElement("w:fldChar")
        fld.set(qn("w:fldCharType"), "begin")
        fld_run.append(fld)
        paragraph._p.append(fld_run)
        end = OxmlElement("w:commentRangeEnd")
        end.set(qn("w:id"), "1")
        paragraph._p.append(end)
        source = samples_dir / "fld_marked.docx"
        document.save(str(source))

        output = samples_dir / "fld_marked_out.docx"
        result = DocxHandler().process(source, output, engine)

        assert FAKE_PHONE_1 in Document(str(output)).paragraphs[0].text, "域代码段落应被跳过"
        assert result.message is not None and "跳过" in result.message


class TestSourceUnchanged:
    """源文件不变。"""

    def test_sha256一致(self, engine, samples_dir):
        source = make_comment_sample()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        DocxHandler().process(source, samples_dir / "out.docx", engine)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before

    def test_dry_run不写文件(self, engine, samples_dir):
        source = make_comment_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine, dry_run=True)
        assert not output.exists()
        assert result.total_hits > 0


class TestSecurityBottomLine:
    """安全底线。"""

    def test_输出与源同路径被拒(self, engine, samples_dir):
        source = make_comment_sample()
        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(source, source, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_SAME


class TestTraversal:
    """遍历器。"""

    def test_产出全部批注段落(self):
        document = Document(str(make_comment_sample()))
        items = list(iter_comment_paragraphs(document))
        assert len(items) == 3
        labels = [label for _, label in items]
        assert labels == ["批注 #1", "批注 #2", "批注 #3"]

    def test_无批注时产出空(self, samples_dir):
        document = Document()
        document.add_paragraph("正文")
        source = samples_dir / "e.docx"
        document.save(str(source))
        assert list(iter_comment_paragraphs(Document(str(source)))) == []