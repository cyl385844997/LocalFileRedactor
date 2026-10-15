"""docx 文本框 / 脚注 / 尾注单元测试（优先级 6）。

这是 docx 的最后一站。三类容器的存储形态完全不同：

* **文本框**：内嵌在 ``document.xml`` 的 ``<w:txbxContent>`` 里，无独立 part
* **脚注/ 尾注**：独立 part，且 part **没有 ``element`` 属性**（需 blob 解析 + 显式写回）

覆盖：
    * 外层段落与文本框**两处都脱敏**（用户补充的重点 A）
    * ``w:txbxContent`` 元素本身完好
    * 嵌套文本框
    * 脚注/尾注脱敏 + **写回落盘**
    * 系统保留条目（separator / continuationSeparator）不被误处理
    * 域代码仍被拦截（反向约束）
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
from lxml import etree

from fixtures.make_docx_samples import make_textbox_note_sample
from fixtures.make_samples import (
    FAKE_BANK_CARD,
    FAKE_EMAIL,
    FAKE_ID_VALID,
    FAKE_PHONE_1,
    FAKE_PHONE_2,
)
from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rules
from redactor.handlers.base import HandlerError
from redactor.handlers.docx_handler import DocxHandler
from redactor.handlers.docx_textbox import (
    MAX_TEXTBOX_DEPTH,
    commit_note_part,
    get_note_part,
    iter_note_paragraphs,
    iter_textbox_paragraphs,
)

BUILTIN_RULES = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


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


def read_part(docx_path: Path, name: str) -> str:
    with zipfile.ZipFile(docx_path) as archive:
        return archive.read(name).decode("utf-8")


def textbox_texts(docx_path: Path):
    """读出每个文本框**直属**段落的文本。

    用 ``findall(w:t)`` 而非 ``iter``——``iter`` 会把嵌套文本框的内容
    也算进外层，导致断言时把两层内容混在一起（实测踩过）。
    """
    root = etree.fromstring(read_part(docx_path, "word/document.xml").encode("utf-8"))
    return [
        "".join(
            t.text or ""
            for t in p.findall(W_NS + "r/" + W_NS + "t")
        )
        for txbx in root.iter(W_NS + "txbxContent")
        for p in txbx.findall(W_NS + "p")
    ]


def note_texts(docx_path: Path, part_name: str):
    xml = read_part(docx_path, part_name)
    return re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)


class TestTextBox:
    """文本框。"""

    def test_文本框内容被脱敏(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = "\n".join(textbox_texts(output))
        assert FAKE_EMAIL not in text, "文本框内容未脱敏"
        assert "[邮箱]" in text

    def test_外层正文也被脱敏(self, engine, samples_dir):
        """重点 A：外层段落同时含普通文本和文本框时，两处都要处理。"""
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        texts = "\n".join(p.text for p in Document(str(output)).paragraphs)
        assert FAKE_PHONE_1 not in texts, "外层正文未脱敏"
        assert "外层正文电话 [手机号]" in texts

    def test_内外都脱敏(self, engine, samples_dir):
        """把两处放在一起断言，避免只修一处就以为完成。"""
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        body = "\n".join(p.text for p in Document(str(output)).paragraphs)
        box = "\n".join(textbox_texts(output))
        assert FAKE_PHONE_1 not in body, "外层正文漏脱敏"
        assert FAKE_EMAIL not in box, "文本框内漏脱敏"

    def test_txbxContent元素本身完好(self, engine, samples_dir):
        """文本框容器元素不能因处理而丢失。"""
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        before = len(
            list(
                etree.fromstring(
                    read_part(source, "word/document.xml").encode("utf-8")
                ).iter(W_NS + "txbxContent")
            )
        )
        after = len(
            list(
                etree.fromstring(
                    read_part(output, "word/document.xml").encode("utf-8")
                ).iter(W_NS + "txbxContent")
            )
        )
        assert before == after, f"文本框数量改变：{before} -> {after}"

    def test_嵌套文本框被处理(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = "\n".join(textbox_texts(output))
        assert FAKE_ID_VALID not in text, "嵌套文本框未脱敏"
        assert "[身份证号]" in text

    def test_真实嵌套结构各层级均被处理且不重复(self, engine, samples_dir):
        """回归：真正的嵌套（内层 txbxContent 挂在外层 <w:p> 内）。

        实测踩过的坑：
          * 顶层 ``root.iter()`` 会把内层也当独立文本框处理一遍，
            递归再处理一遍 -> 同一段落产出 **3 次**、命中数翻倍；
          * 若改成"跳过有祖先的"，递归时内层会自我跳过 -> **内层内容丢失**。

        正确行为：每层恰好处理一次，标签体现层级。
        """
        document = Document()
        paragraph = document.add_paragraph()

        outer = OxmlElement("w:txbxContent")
        outer_p = OxmlElement("w:p")
        outer_r = OxmlElement("w:r")
        outer_t = OxmlElement("w:t")
        outer_t.text = f"外层框 {FAKE_PHONE_1}"
        outer_r.append(outer_t)
        outer_p.append(outer_r)

        # 内层文本框：作为外层段落的子元素（真实嵌套形态）
        inner = OxmlElement("w:txbxContent")
        inner_p = OxmlElement("w:p")
        inner_r = OxmlElement("w:r")
        inner_t = OxmlElement("w:t")
        inner_t.text = f"内层框 {FAKE_EMAIL}"
        inner_r.append(inner_t)
        inner_p.append(inner_r)
        inner.append(inner_p)

        outer_p.append(inner)
        outer.append(outer_p)
        paragraph._p.append(outer)

        source = samples_dir / "nested.docx"
        document.save(str(source))

        output = samples_dir / "nested_out.docx"
        DocxHandler().process(source, output, engine)

        texts = textbox_texts(output)
        assert any("[手机号]" in t for t in texts), f"外层未脱敏：{texts}"
        assert any("[邮箱]" in t for t in texts), f"内层未脱敏：{texts}"
        # 每层恰好一次，不重复
        assert len(texts) == 2, f"段落被重复或丢失：{texts}"

    def test_嵌套层级体现在标签中(self, samples_dir):
        document = Document()
        paragraph = document.add_paragraph()
        outer = OxmlElement("w:txbxContent")
        outer_p = OxmlElement("w:p")
        outer_r = OxmlElement("w:r")
        outer_t = OxmlElement("w:t")
        outer_t.text = "外层"
        outer_r.append(outer_t)
        outer_p.append(outer_r)
        inner = OxmlElement("w:txbxContent")
        inner_p = OxmlElement("w:p")
        inner_r = OxmlElement("w:r")
        inner_t = OxmlElement("w:t")
        inner_t.text = "内层"
        inner_r.append(inner_t)
        inner_p.append(inner_r)
        inner.append(inner_p)
        outer_p.append(inner)
        outer.append(outer_p)
        paragraph._p.append(outer)
        source = samples_dir / "nest2.docx"
        document.save(str(source))

        items = list(iter_textbox_paragraphs(Document(str(source))))
        labels = [label for _, label in items]
        assert "文本框 1" in labels, labels
        assert any("第 2 层" in label for label in labels), labels

    def test_位置标签格式(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        locations = {loc for stat in result.hits for loc in stat.locations}
        box_locs = [loc for loc in locations if loc.startswith("文本框")]
        assert box_locs, f"未见文本框位置标签：{locations}"
        assert all(re.match(r"^文本框 \d+", loc) for loc in box_locs), box_locs

    def test_跳过段落数归零(self, engine, samples_dir):
        """回归：含 txbxContent 的段落不再被当作复杂结构跳过。"""
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)
        assert result.message is None, f"仍被跳过：{result.message}"

    def test_遍历器可单独使用(self):
        document = Document(str(make_textbox_note_sample()))
        items = list(iter_textbox_paragraphs(document))
        assert items, "应产出文本框段落"

    def test_深度上限存在(self):
        assert 1 <= MAX_TEXTBOX_DEPTH <= 10


class TestFootnotesEndnotes:
    """脚注与尾注。"""

    def test_脚注被脱敏(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = "\n".join(note_texts(output, "word/footnotes.xml"))
        assert FAKE_PHONE_2 not in text
        assert "[手机号]" in text

    def test_尾注被脱敏(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = "\n".join(note_texts(output, "word/endnotes.xml"))
        assert FAKE_BANK_CARD not in text
        assert "[银行卡号]" in text

    def test_改动确实落盘(self, engine, samples_dir):
        """核心回归：footnotes/endnotes 的 part 无 element，必须显式写回。

        若忘记 commit，脚注内容在内存里改了但不会存进文件。
        """
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        # 直接读包内文件确认（而不是读内存对象）
        assert "[手机号]" in read_part(output, "word/footnotes.xml")
        assert "[银行卡号]" in read_part(output, "word/endnotes.xml")

    def test_脚注id与type不变(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        xml = read_part(output, "word/footnotes.xml")
        assert '<w:footnote w:type="separator" w:id="-1">' in xml
        assert '<w:footnote w:type="continuationSeparator" w:id="0">' in xml
        assert '<w:footnote w:id="2">' in xml

    def test_系统保留条目不被处理(self, engine, samples_dir):
        """separator / continuationSeparator 是空条目，不应产生命中。"""
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        locations = {loc for stat in result.hits for loc in stat.locations}
        note_locs = [loc for loc in locations if loc.startswith(("脚注", "尾注"))]
        # 只有真实脚注 #2，不应有 -1 或 0
        assert all("#2" in loc for loc in note_locs), note_locs

    def test_正文引用标记完好(self, engine, samples_dir):
        """w:id 是正文与脚注的关联依据，不能动。"""
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        doc_xml = read_part(output, "word/document.xml")
        assert '<w:footnoteReference w:id="2"/>' in doc_xml
        assert '<w:endnoteReference w:id="2"/>' in doc_xml

    def test_位置标签格式(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        locations = {loc for stat in result.hits for loc in stat.locations}
        assert "脚注 #2" in locations, locations
        assert "尾注 #2" in locations, locations

    def test_命名空间前缀完整(self, engine, samples_dir):
        """写回时若命名空间丢失，Word 会打不开。"""
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        for part in ("word/footnotes.xml", "word/endnotes.xml"):
            xml = read_part(output, part)
            assert "ns0:" not in xml and "ns1:" not in xml, f"{part} 出现未声明前缀"
            assert "w:footnotes" in xml or "w:endnotes" in xml

    def test_python_docx可重开(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        assert Document(str(output)) is not None

    def test_commit_note_part可单独使用(self, samples_dir):
        """写回函数应能独立工作（便于测试与复用）。"""
        document = Document(str(make_textbox_note_sample()))
        part = get_note_part(document, "footnote")
        assert part is not None

        document.save(str(samples_dir / "cm.docx"))
        # 重新打开后确认可解析
        reloaded = Document(str(samples_dir / "cm.docx"))
        part2 = get_note_part(reloaded, "footnote")
        assert part2 is not None
        assert b"footnote" in part2.blob

    def test_无脚注文档正常处理(self, engine, samples_dir):
        """无脚注时不得凭空创建 footnotes.xml（页眉页脚的前车之鉴）。"""
        document = Document()
        document.add_paragraph(f"正文手机 {FAKE_PHONE_1}")
        source = samples_dir / "plain.docx"
        document.save(str(source))

        output = samples_dir / "plain_out.docx"
        DocxHandler().process(source, output, engine)

        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
        assert not any("footnotes" in n for n in names), f"凭空创建：{names}"


class TestReverseConstraints:
    """反向约束：放行不能过度。"""

    def test_域代码仍被拦截(self, engine, samples_dir):
        """含 txbxContent 的段落若还有域代码，仍应跳过。"""
        document = Document()
        paragraph = document.add_paragraph()
        txbx = OxmlElement("w:txbxContent")
        tp = OxmlElement("w:p")
        tr = OxmlElement("w:r")
        tt = OxmlElement("w:t")
        tt.text = "文本框内容"
        tr.append(tt)
        tp.append(tr)
        txbx.append(tp)
        paragraph._p.append(txbx)

        fld_run = OxmlElement("w:r")
        fld = OxmlElement("w:fldChar")
        fld.set(qn("w:fldCharType"), "begin")
        fld_run.append(fld)
        paragraph._p.append(fld_run)

        source = samples_dir / "txbx_fld.docx"
        document.save(str(source))

        output = samples_dir / "txbx_fld_out.docx"
        result = DocxHandler().process(source, output, engine)
        assert result.message is not None and "跳过" in result.message

    def test_文本框内域代码被跳过但其他框仍处理(self, engine, samples_dir):
        """一个框内域代码不应影响另一个框的脱敏。"""
        document = Document()
        # 框1：正常
        p1 = document.add_paragraph()
        good = OxmlElement("w:txbxContent")
        gp = OxmlElement("w:p")
        gr = OxmlElement("w:r")
        gt = OxmlElement("w:t")
        gt.text = f"正常框 {FAKE_PHONE_1}"
        gr.append(gt)
        gp.append(gr)
        good.append(gp)
        p1._p.append(good)

        # 框2：含域代码 + 敏感信息
        p2 = document.add_paragraph()
        bad = OxmlElement("w:txbxContent")
        bp = OxmlElement("w:p")
        br = OxmlElement("w:r")
        bt = OxmlElement("w:t")
        bt.text = f"域代码框 {FAKE_EMAIL}"
        br.append(bt)
        bp.append(br)
        bfld = OxmlElement("w:r")
        bf = OxmlElement("w:fldChar")
        bf.set(qn("w:fldCharType"), "begin")
        bfld.append(bf)
        bp.append(bfld)
        bad.append(bp)
        p2._p.append(bad)

        source = samples_dir / "mixed_txbx.docx"
        document.save(str(source))

        output = samples_dir / "mixed_txbx_out.docx"
        DocxHandler().process(source, output, engine)

        texts = textbox_texts(output)
        assert "[手机号]" in texts[0], f"正常框未被处理：{texts}"
        assert FAKE_EMAIL in texts[1], "域代码框应保留原文（宁漏不错位）"


class TestProofErrAndBookmark:
    """回归测试：``w:proofErr`` / ``w:bookmark*`` 不应导致整段跳过。

    这类元素都是**空元素、无文本子节点、不占偏移**（实测确认），
    放行它们没有错位风险。但不放行的代价很大——**Word 默认会自动插入
    ``proofErr``**，几乎所有人工编辑过的真实文档都有，
    会导致大量段落正文整段跳过、漏脱敏。
    """

    @pytest.fixture()
    def marked_doc(self, samples_dir):
        """构造带proofErr + bookmark 的段落。"""
        document = Document()
        paragraph = document.add_paragraph()

        run1 = OxmlElement("w:r")
        text1 = OxmlElement("w:t")
        text1.text = f"联系电话 {FAKE_PHONE_1}"
        run1.append(text1)

        spell_start = OxmlElement("w:proofErr")
        spell_start.set(qn("w:type"), "spellStart")
        paragraph._p.append(spell_start)
        paragraph._p.append(run1)
        spell_end = OxmlElement("w:proofErr")
        spell_end.set(qn("w:type"), "spellEnd")
        paragraph._p.append(spell_end)

        bookmark_start = OxmlElement("w:bookmarkStart")
        bookmark_start.set(qn("w:id"), "0")
        bookmark_start.set(qn("w:name"), "_GoBack")
        paragraph._p.append(bookmark_start)

        run2 = OxmlElement("w:r")
        text2 = OxmlElement("w:t")
        text2.text = f"邮箱 {FAKE_EMAIL}"
        run2.append(text2)
        paragraph._p.append(run2)

        bookmark_end = OxmlElement("w:bookmarkEnd")
        bookmark_end.set(qn("w:id"), "0")
        paragraph._p.append(bookmark_end)

        path = samples_dir / "marked.docx"
        document.save(str(path))
        return path

    def test_带proofErr的段落正文被脱敏(self, engine, marked_doc, samples_dir):
        output = samples_dir / "out.docx"
        DocxHandler().process(marked_doc, output, engine)

        text = Document(str(output)).paragraphs[0].text
        assert FAKE_PHONE_1 not in text, "带 proofErr 的段落未脱敏"
        assert "[手机号]" in text

    def test_带bookmark的段落正文被脱敏(self, engine, marked_doc, samples_dir):
        output = samples_dir / "out.docx"
        DocxHandler().process(marked_doc, output, engine)

        text = Document(str(output)).paragraphs[0].text
        assert FAKE_EMAIL not in text, "带 bookmark 的段落未脱敏"
        assert "[邮箱]" in text

    def test_跳过段落数归零(self, engine, marked_doc, samples_dir):
        output = samples_dir / "out.docx"
        result = DocxHandler().process(marked_doc, output, engine)
        assert result.message is None, f"仍被当作复杂结构跳过：{result.message}"

    def test_proofErr元素本身完好(self, engine, marked_doc, samples_dir):
        output = samples_dir / "out.docx"
        DocxHandler().process(marked_doc, output, engine)

        xml = read_part(output, "word/document.xml")
        assert '<w:proofErr w:type="spellStart"/>' in xml
        assert '<w:proofErr w:type="spellEnd"/>' in xml

    def test_bookmark元素本身完好(self, engine, marked_doc, samples_dir):
        output = samples_dir / "out.docx"
        DocxHandler().process(marked_doc, output, engine)

        xml = read_part(output, "word/document.xml")
        assert '<w:bookmarkStart w:id="0" w:name="_GoBack"/>' in xml
        assert '<w:bookmarkEnd w:id="0"/>' in xml

    def test_grammar标记同样放行(self, engine, samples_dir):
        """proofErr 还有 gramStart / gramEnd（语法校对），也应放行。"""
        document = Document()
        paragraph = document.add_paragraph()
        start = OxmlElement("w:proofErr")
        start.set(qn("w:type"), "gramStart")
        paragraph._p.append(start)
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = f"语法标记内 {FAKE_PHONE_1}"
        run.append(text)
        paragraph._p.append(run)
        end = OxmlElement("w:proofErr")
        end.set(qn("w:type"), "gramEnd")
        paragraph._p.append(end)
        source = samples_dir / "gram.docx"
        document.save(str(source))

        output = samples_dir / "gram_out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_PHONE_1 not in Document(str(output)).paragraphs[0].text

    def test_域代码仍被拦截(self, engine, samples_dir):
        """反向约束：放行 proofErr 不能扩大到域代码。"""
        document = Document()
        paragraph = document.add_paragraph()
        start = OxmlElement("w:proofErr")
        start.set(qn("w:type"), "spellStart")
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
        source = samples_dir / "pe_fld.docx"
        document.save(str(source))

        output = samples_dir / "pe_fld_out.docx"
        result = DocxHandler().process(source, output, engine)

        assert FAKE_PHONE_1 in Document(str(output)).paragraphs[0].text, "域代码段落应被跳过"
        assert result.message is not None and "跳过" in result.message

    def test_修订标记仍被拦截(self, engine, samples_dir):
        """反向约束：修订标记（w:ins）内容不可靠，仍应跳过。"""
        document = Document()
        paragraph = document.add_paragraph()
        ins = OxmlElement("w:ins")
        ins.set(qn("w:id"), "1")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = f"修订内容 {FAKE_PHONE_1}"
        run.append(text)
        ins.append(run)
        paragraph._p.append(ins)
        source = samples_dir / "ins.docx"
        document.save(str(source))

        output = samples_dir / "ins_out.docx"
        result = DocxHandler().process(source, output, engine)
        assert result.message is not None and "跳过" in result.message

    def test_图形对象仍被拦截(self, engine, samples_dir):
        """反向约束：含图片/形状的 run 仍应跳过。"""
        document = Document()
        paragraph = document.add_paragraph()
        run = OxmlElement("w:r")
        drawing = OxmlElement("w:drawing")
        run.append(drawing)
        paragraph._p.append(run)
        text_run = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = f"图旁文字 {FAKE_PHONE_1}"
        text_run.append(t)
        paragraph._p.append(text_run)
        source = samples_dir / "pic.docx"
        document.save(str(source))

        output = samples_dir / "pic_out.docx"
        result = DocxHandler().process(source, output, engine)
        assert result.message is not None and "跳过" in result.message


class TestSourceUnchanged:
    """源文件不变。"""

    def test_sha256一致(self, engine, samples_dir):
        source = make_textbox_note_sample()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        DocxHandler().process(source, samples_dir / "out.docx", engine)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before

    def test_dry_run不写文件(self, engine, samples_dir):
        source = make_textbox_note_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine, dry_run=True)
        assert not output.exists()
        assert result.total_hits > 0


class TestSecurityBottomLine:
    """安全底线。"""

    def test_输出与源同路径被拒(self, engine, samples_dir):
        source = make_textbox_note_sample()
        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(source, source, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_SAME


class TestTraversal:
    """遍历器。"""

    def test_脚注遍历产出三元组(self):
        document = Document(str(make_textbox_note_sample()))
        items = list(iter_note_paragraphs(document, "footnote"))
        assert items, "应产出脚注段落"
        _para, label, _root = items[0]
        assert label == "脚注 #2"

    def test_无脚注时产出空(self, samples_dir):
        document = Document()
        document.add_paragraph("正文")
        source = samples_dir / "none.docx"
        document.save(str(source))
        reloaded = Document(str(source))
        assert list(iter_note_paragraphs(reloaded, "footnote")) == []