"""docx 页眉页脚处理单元测试（优先级 4）。

覆盖：
    * 多节页眉页脚遍历 + 按 part 去重（链接到前一节不重复处理）
    * 首页页眉 / 偶数页页眉遍历
    * 页眉内的表格与超链接
    * 跳过粒度为段落而非整节
    * 报告位置标签与"当前可能不显示"的提示

不在本轮范围：批注、文本框、脚注尾注。
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from fixtures.make_docx_samples import add_hyperlink, make_headerfooter_sample
from fixtures.make_samples import (
    FAKE_BANK_CARD,
    FAKE_EMAIL,
    FAKE_ID_VALID,
    FAKE_IP,
    FAKE_PHONE_1,
    FAKE_PHONE_2,
)
from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rules
from redactor.handlers.base import HandlerError
from redactor.handlers.docx_handler import DocxHandler
from redactor.handlers.docx_headerfooter import (
    collect_header_footer_items,
    iter_header_footer_paragraphs,
)

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


def _header_footer_files(docx_path: Path) -> set:
    """列出包内所有 header/footer XML 文件名。"""
    with zipfile.ZipFile(docx_path) as archive:
        return {
            name
            for name in archive.namelist()
            if ("header" in name or "footer" in name) and name.endswith(".xml")
        }


def all_headerfooter_text(docx_path: Path) -> str:
    """读出所有页眉页脚 part 的文本（含表格内）。

    直接读包内 XML 文件，**不碰 section.header 等代理属性**——
    实测读取那些属性会产生副作用（自动创建不存在的 part）。
    """
    chunks = []
    with zipfile.ZipFile(docx_path) as archive:
        for name in sorted(archive.namelist()):
            if not (("header" in name or "footer" in name) and name.endswith(".xml")):
                continue
            from lxml import etree

            root = etree.fromstring(archive.read(name))
            W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            chunks.extend(t.text or "" for t in root.iter(W + "t"))
    return "\n".join(chunks)


class TestCollectHeaderFooterItems:
    """part 去重。"""

    def test_按part去重不重复计数(self):
        """回归：链接到前一节的节不应产生第二个 item。

        实测事实：当某节"链接到前一节"时，它的 ``sectPr`` 里
        **根本没有 headerReference**——内容存放在被链接的那一节。
        所以正确实现天然不会重复；本用例锁住这个行为。
        """
        document = Document()
        section1 = document.sections[0]
        section1.header.is_linked_to_previous = False
        section1.header.paragraphs[0].text = "页眉1"
        section2 = document.add_section(WD_SECTION.NEW_PAGE)
        section2.header.is_linked_to_previous = False
        section2.header.paragraphs[0].text = "页眉2"
        document.add_section(WD_SECTION.NEW_PAGE)  # 链接到前一节，无引用

        items, duplicates = collect_header_footer_items(document)

        parts = [item.part for item in items]
        assert len(parts) == len(set(parts)), "同一 part 出现多次"
        header_labels = [i.label for i in items if i.is_header]
        assert header_labels == ["页眉 第1节", "页眉 第2节"], header_labels
        # 第3节无引用，因此不会产生重复 part
        assert duplicates == 0, f"不应有重复 part，实际 {duplicates}"

    def test_位置标签格式(self):
        document = Document()
        document.sections[0].header.is_linked_to_previous = False
        document.sections[0].footer.is_linked_to_previous = False
        items, _ = collect_header_footer_items(document)
        labels = [item.label for item in items]
        assert "页眉 第1节" in labels, labels
        assert "页脚 第1节" in labels, labels

    def test_四种类型都被收集(self):
        """default/first/even 三种引用都应被收集。"""
        document = Document()
        section = document.sections[0]
        section.different_first_page_header_footer = True
        section.header.is_linked_to_previous = False
        section.first_page_header.is_linked_to_previous = False
        section.even_page_header.is_linked_to_previous = False
        section.footer.is_linked_to_previous = False

        items, _ = collect_header_footer_items(document)
        labels = {item.label for item in items}
        assert any("页眉 第1节" == l for l in labels), labels
        assert any(l.startswith("首页页眉") for l in labels), labels
        assert any(l.startswith("偶数页眉") for l in labels), labels
        assert any(l.startswith("页脚") for l in labels), labels

    def test_读取不产生副作用(self, tmp_path):
        """回归测试：仅遍历不得凭空创建 first/even 页眉页脚。

        实测：早期版本用 ``getattr(section, "first_page_header")`` 遍历，
        读取本身就会``_add_definition()`` 创建 header2.xml/header3.xml，
        并在 sectPr 里加出type="first"/"even" 引用——
        等于每次处理都往输出文档里塞 4 个空页眉页脚。
        """
        document = Document()
        document.sections[0].header.is_linked_to_previous = False
        document.sections[0].header.paragraphs[0].text = "页眉"
        source = tmp_path / "side_effect.docx"
        document.save(str(source))

        before = _header_footer_files(source)

        output = tmp_path / "side_effect_out.docx"
        DocxHandler().process(
            source,
            output,
            RedactionEngine(load_rules(BUILTIN_RULES)),
        )

        after = _header_footer_files(output)
        assert before == after, f"页眉页脚文件集合被改变：{before} -> {after}"


class TestBasicHeaderFooter:
    """基本脱敏。"""

    def test_页眉页脚敏感信息被脱敏(self, engine, samples_dir):
        source = make_headerfooter_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        text = all_headerfooter_text(output)

        assert FAKE_PHONE_1 not in text, "页眉手机号未脱敏"
        assert FAKE_EMAIL not in text, "页脚邮箱未脱敏"
        assert FAKE_ID_VALID not in text
        assert FAKE_IP not in text
        assert FAKE_BANK_CARD not in text
        assert FAKE_PHONE_2 not in text

    def test_位置标签进入报告(self, engine, samples_dir):
        source = make_headerfooter_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        locations = {loc for stat in result.hits for loc in stat.locations}
        assert any("页眉 第1节" in loc for loc in locations), f"未见页眉位置：{locations}"
        assert any("页脚 第1节" in loc for loc in locations), f"未见页脚位置：{locations}"

    def test_链接到前一节不虚增计数(self, engine, samples_dir):
        """第2节的页眉不该因第3节链接而被计两次。"""
        document = Document()
        section1 = document.sections[0]
        section1.header.is_linked_to_previous = False
        section1.header.paragraphs[0].text = f"共享页眉 {FAKE_PHONE_1}"
        document.add_section(WD_SECTION.NEW_PAGE)  # 链接到前一节
        source = samples_dir / "linked.docx"
        document.save(str(source))

        output = samples_dir / "linked_out.docx"
        result = DocxHandler().process(source, output, engine)

        phone = [s for s in result.hits if s.rule_name == "手机号"]
        assert phone, "应统计到手机号"
        assert phone[0].count == 1, f"链接节导致重复计数：{phone[0].count}"

    def test_正文不受影响(self, engine, samples_dir):
        source = make_headerfooter_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        # 正文段落（"正文段落"）应仍在
        text = "\n".join(p.text for p in Document(str(output)).paragraphs)
        assert "正文段落" in text


class TestSpecialKinds:
    """首页/ 偶数页。"""

    def test_首页页眉被脱敏(self, engine, samples_dir):
        source = make_headerfooter_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_IP not in all_headerfooter_text(output)

    def test_偶数页页眉被脱敏(self, engine, samples_dir):
        source = make_headerfooter_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_BANK_CARD not in all_headerfooter_text(output)

    def test_提示说明当前可能不显示(self, engine, samples_dir):
        source = make_headerfooter_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        assert result.message is not None
        assert "首页" in result.message or "偶数页" in result.message
        assert "不显示" in result.message or "未开启" in result.message

    def test_无特殊类型时不给该提示(self, engine, samples_dir):
        document = Document()
        document.sections[0].header.is_linked_to_previous = False
        document.sections[0].header.paragraphs[0].text = f"页眉 {FAKE_PHONE_1}"
        source = samples_dir / "plain.docx"
        document.save(str(source))

        output = samples_dir / "plain_out.docx"
        result = DocxHandler().process(source, output, engine)
        assert result.message is None or "首页" not in result.message


class TestHeaderInnerStructures:
    """页眉内的表格与超链接。"""

    def test_页眉表格内容被脱敏(self, engine, samples_dir):
        source = make_headerfooter_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_PHONE_2 not in all_headerfooter_text(output)

    def test_页眉超链接文字被脱敏(self, engine, samples_dir):
        document = Document()
        section = document.sections[0]
        section.header.is_linked_to_previous = False
        add_hyperlink(
            section.header.paragraphs[0], f"mailto:{FAKE_EMAIL}", f"联系 {FAKE_EMAIL}"
        )
        source = samples_dir / "hdr_link.docx"
        document.save(str(source))

        output = samples_dir / "hdr_link_out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_EMAIL not in all_headerfooter_text(output)

    def test_页眉超链接mailto目标被改写(self, engine, samples_dir):
        """页眉 part 有自己的 rels，也要改写 mailto 目标。"""
        document = Document()
        section = document.sections[0]
        section.header.is_linked_to_previous = False
        add_hyperlink(section.header.paragraphs[0], f"mailto:{FAKE_EMAIL}", "邮件")
        source = samples_dir / "hdr_mailto.docx"
        document.save(str(source))

        output = samples_dir / "hdr_mailto_out.docx"
        DocxHandler().process(source, output, engine)

        with zipfile.ZipFile(output) as archive:
            header_rels = [
                name
                for name in archive.namelist()
                if "header" in name and name.endswith(".rels")
            ]
            assert header_rels, "应有页眉的 rels 文件"
            for name in header_rels:
                content = archive.read(name).decode("utf-8")
                assert FAKE_EMAIL not in content, f"{name} 仍含邮箱"

    def test_遍历器产出段落(self):
        document = Document()
        document.sections[0].header.is_linked_to_previous = False
        document.sections[0].header.paragraphs[0].text = "页眉内容"

        items = list(iter_header_footer_paragraphs(document))
        assert items, "应产出页眉段落"
        assert any("页眉内容" in p.text for p, _ in items)


class TestSkipStrategy:
    """跳过粒度：只跳过问题段落。"""

    def test_域代码段落被跳过(self, engine, samples_dir):
        document = Document()
        section = document.sections[0]
        section.footer.is_linked_to_previous = False
        paragraph = section.footer.paragraphs[0]
        paragraph.add_run("第")
        run = OxmlElement("w:r")
        fld = OxmlElement("w:fldChar")
        fld.set(qn("w:fldCharType"), "begin")
        run.append(fld)
        paragraph._p.append(run)

        source = samples_dir / "fld.docx"
        document.save(str(source))

        output = samples_dir / "fld_out.docx"
        result = DocxHandler().process(source, output, engine)
        assert result.message is not None
        assert "页眉页脚" in result.message

    def test_跳过不影响同节其他段落(self, engine, samples_dir):
        """页眉里一段有域代码，另一段仍应正常脱敏。"""
        document = Document()
        section = document.sections[0]
        section.header.is_linked_to_previous = False
        section.header.paragraphs[0].text = f"正常页眉 {FAKE_PHONE_1}"

        # 第二个段落塞域代码
        bad = section.header.add_paragraph("页码")
        run = OxmlElement("w:r")
        fld = OxmlElement("w:fldChar")
        fld.set(qn("w:fldCharType"), "begin")
        run.append(fld)
        bad._p.append(run)

        source = samples_dir / "mixed_fld.docx"
        document.save(str(source))

        output = samples_dir / "mixed_fld_out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_PHONE_1 not in all_headerfooter_text(output)


class TestSourceUnchanged:
    """源文件不变。"""

    def test_sha256一致(self, engine, samples_dir):
        source = make_headerfooter_sample()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        DocxHandler().process(source, samples_dir / "out.docx", engine)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before

    def test_dry_run不写文件(self, engine, samples_dir):
        source = make_headerfooter_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine, dry_run=True)
        assert not output.exists()
        assert result.total_hits > 0


class TestSecurityBottomLine:
    """安全底线。"""

    def test_输出与源同路径被拒(self, engine, samples_dir):
        source = make_headerfooter_sample()
        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(source, source, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_SAME


class TestNotCovered:
    """边界：本轮之外的容器仍未处理。"""

    def test_批注仍未处理(self, engine, samples_dir):
        """批注属优先级 5，本轮不处理——锁定边界。"""
        document = Document()
        document.add_paragraph("正文段落")
        source = samples_dir / "doc.docx"
        document.save(str(source))

        output = samples_dir / "doc_out.docx"
        result = DocxHandler().process(source, output, engine)

        # 能正常打开即说明未破坏；本轮只锁定"不崩溃"
        assert Document(str(output)) is not None