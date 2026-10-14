"""docx 表格处理单元测试（优先级 3）。

覆盖：
    * 普通表格的多行多列、单元格内多段落
    * 合并单元格按 XML 元素去重（不虚增命中次数）
    * 嵌套表格递归处理 + 位置标签嵌套
    * 单元格内超链接
    * 单元格内域代码 -> 只跳过该段落，不整表跳过
    * 报告 message 正文/表格分开计数

不在本轮范围：页眉页脚、批注、文本框、脚注尾注。
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from fixtures.make_docx_samples import add_hyperlink, make_table_sample
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
from redactor.handlers.docx_table import (
    MAX_NESTED_DEPTH,
    cell_position_label,
    iter_table_paragraphs,
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
    """样例输出到临时目录。"""
    target = tmp_path / "samples"
    target.mkdir(parents=True, exist_ok=True)
    original = samples_module.SAMPLES_DIR
    samples_module.SAMPLES_DIR = target
    try:
        yield target
    finally:
        samples_module.SAMPLES_DIR = original


def table_text(docx_path: Path) -> str:
    """读出所有表格单元格的文本（含嵌套表）。

    去重同样按 XML 元素本身，**不能用 id()**——
    实测 python-docx 的 ``cell._tc`` 代理被回收后 id() 会被复用，
    导致正常单元格被误判为已访问而漏读（测试自己也踩过这个坑）。
    """
    from docx.oxml.ns import qn

    document = Document(str(docx_path))
    chunks = []

    def walk(table, seen):
        for row in table.rows:
            for cell in row.cells:
                if cell._tc in seen:
                    continue
                seen.add(cell._tc)
                chunks.extend(p.text for p in cell.paragraphs)
                for nested in cell.tables:
                    walk(nested, seen)

    seen_cells = set()
    for table in document.tables:
        walk(table, seen_cells)
    return "\n".join(chunks)


class TestPositionLabel:
    """位置标签格式。"""

    def test_顶层标签(self):
        assert cell_position_label(1, 2, 3) == "表1 R2C3"

    def test_嵌套标签(self):
        assert cell_position_label(1, 1, 1, prefix="表2 R3C1") == "表2 R3C1 内表1 R1C1"

    def test_行列从1开始(self):
        """与 Word 界面一致（行列均从 1 开始）。"""
        label = cell_position_label(3, 1, 1)
        assert label == "表3 R1C1"


class TestBasicTable:
    """普通表格。"""

    def test_表格内敏感信息被脱敏(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        text = table_text(output)

        assert FAKE_PHONE_1 not in text
        assert FAKE_EMAIL not in text
        assert FAKE_ID_VALID not in text
        assert FAKE_PHONE_2 not in text

    def test_占位符写入(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        text = table_text(output)

        assert "[手机号]" in text
        assert "[邮箱]" in text
        assert "[身份证号]" in text

    def test_位置标签为表格格式(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        locations = {loc for stat in result.hits for loc in stat.locations}
        assert any("表1 R1C1" in loc for loc in locations), f"未见表格位置标签：{locations}"

    def test_单元格内多段落都被处理(self, engine, samples_dir):
        """一个单元格可有多个段落，每段都要处理。"""
        source = make_table_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        assert FAKE_PHONE_2 not in table_text(output)

    def test_正文段落仍正常处理(self, engine, samples_dir):
        """表格处理不应影响正文段落。"""
        document = Document()
        document.add_paragraph(f"正文手机 {FAKE_PHONE_1}")
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = f"表格手机 {FAKE_PHONE_2}"
        source = samples_dir / "mixed.docx"
        document.save(str(source))

        output = samples_dir / "mixed_out.docx"
        DocxHandler().process(source, output, engine)
        text = "\n".join(p.text for p in Document(str(output)).paragraphs)

        assert FAKE_PHONE_1 not in text, "正文段落未被脱敏"
        assert "[手机号]" in text


class TestMergedCells:
    """合并单元格必须按 XML 元素去重。"""

    def test_合并单元格内容被脱敏(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_BANK_CARD not in table_text(output)

    def test_合并单元格不虚增命中次数(self, engine, samples_dir):
        """回归测试：合并单元格只应计1 次命中。

        合并后``cell(0,0) is cell(0,1)``，若按坐标遍历会访问两次，
        第二次无命中但若计数会翻倍——报告就会多记一次。
        """
        document = Document()
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).merge(table.cell(0, 1))
        table.cell(0, 0).text = f"合并 {FAKE_PHONE_1}"
        source = samples_dir / "merged.docx"
        document.save(str(source))

        output = samples_dir / "merged_out.docx"
        result = DocxHandler().process(source, output, engine)

        phone_stats = [s for s in result.hits if s.rule_name == "手机号"]
        assert phone_stats, "应统计到手机号命中"
        assert phone_stats[0].count == 1, f"合并单元格被重复计数：{phone_stats[0].count}"

    def test_去重后位置标签只出现一次(self, engine, samples_dir):
        document = Document()
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).merge(table.cell(0, 1))
        table.cell(0, 0).text = f"合并 {FAKE_PHONE_1}"
        source = samples_dir / "merged2.docx"
        document.save(str(source))

        output = samples_dir / "merged2_out.docx"
        result = DocxHandler().process(source, output, engine)

        locations = [loc for stat in result.hits for loc in stat.locations]
        assert len(locations) == len(set(locations)), f"位置标签重复：{locations}"


class TestNestedTable:
    """嵌套表格递归处理。"""

    def test_嵌套表内容被脱敏(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        assert FAKE_IP not in table_text(output)

    def test_嵌套位置标签带前缀(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        locations = {loc for stat in result.hits for loc in stat.locations}
        nested = [loc for loc in locations if "内表" in loc]
        assert nested, f"未见嵌套位置标签：{locations}"
        assert "内表1 R1C1" in nested[0]

    def test_递归深度上限存在(self):
        """必须有深度上限，防止病态文档无限递归。"""
        assert MAX_NESTED_DEPTH >= 1
        assert MAX_NESTED_DEPTH <= 10, "深度上限过宽，失去保护意义"

    def test_超深嵌套被拦截而非崩溃(self, engine, samples_dir):
        """构造超过上限的嵌套，确认抛 HandlerError 而不是 RecursionError 泄漏。"""
        document = Document()
        table = document.add_table(rows=1, cols=1)
        cell = table.cell(0, 0)
        for _ in range(MAX_NESTED_DEPTH + 2):
            inner = cell.add_table(rows=1, cols=1)
            cell = inner.cell(0, 0)
        cell.text = f"深层 {FAKE_PHONE_1}"
        source = samples_dir / "deep.docx"
        document.save(str(source))

        output = samples_dir / "deep_out.docx"
        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(source, output, engine)
        assert "嵌套" in excinfo.value.message

    def test_遍历器可单独使用(self):
        """iter_table_paragraphs 独立可用，便于测试与复用。"""
        document = Document()
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "A"
        table.cell(0, 1).text = "B"

        items = list(iter_table_paragraphs(table, 1))
        labels = [label for _, label in items]
        assert "表1 R1C1" in labels and "表1 R1C2" in labels


class TestTableWithHyperlink:
    """单元格内超链接。"""

    def test_单元格内链接文字被脱敏(self, engine, samples_dir):
        document = Document()
        table = document.add_table(rows=1, cols=1)
        add_hyperlink(table.cell(0, 0).paragraphs[0], "https://example.com", FAKE_EMAIL)
        source = samples_dir / "tbl_link.docx"
        document.save(str(source))

        output = samples_dir / "tbl_link_out.docx"
        DocxHandler().process(source, output, engine)

        assert FAKE_EMAIL not in table_text(output)

    def test_表格内mailto链接的target被改写(self, engine, samples_dir):
        """rels 是文档级的，表格内链接的 mailto 同样应被改写。"""
        document = Document()
        table = document.add_table(rows=1, cols=1)
        add_hyperlink(table.cell(0, 0).paragraphs[0], f"mailto:{FAKE_EMAIL}", "邮件")
        source = samples_dir / "tbl_mailto.docx"
        document.save(str(source))

        output = samples_dir / "tbl_mailto_out.docx"
        DocxHandler().process(source, output, engine)

        with zipfile.ZipFile(output) as archive:
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert FAKE_EMAIL not in rels, "表格内链接的 mailto 未被改写"


class TestSkipStrategy:
    """跳过策略：只跳过问题段落，不整表跳过。"""

    def test_域代码段落被跳过(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        assert result.message is not None
        assert "已跳过" in result.message

    def test_跳过只影响问题段落其他单元格照常处理(self, engine, samples_dir):
        """核心安全断言：含域代码的表不能整表跳过。"""
        document = Document()
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = f"正常单元格 {FAKE_PHONE_1}"

        # 第二个单元格塞域代码
        paragraph = table.cell(0, 1).paragraphs[0]
        paragraph.add_run("页码")
        run = OxmlElement("w:r")
        fld = OxmlElement("w:fldChar")
        fld.set(qn("w:fldCharType"), "begin")
        run.append(fld)
        paragraph._p.append(run)

        source = samples_dir / "mixed_skip.docx"
        document.save(str(source))

        output = samples_dir / "mixed_skip_out.docx"
        DocxHandler().process(source, output, engine)

        assert FAKE_PHONE_1 not in table_text(output), "同表其他单元格被连带跳过了"

    def test_message正文与表格分开计数(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        assert "正文段落" in result.message
        assert "表格" in result.message

    def test_无跳过时不给表格提示(self, engine, samples_dir):
        """只有正文、没表格且无跳过时，不该有多余提示。"""
        document = Document()
        document.add_paragraph("普通正文")
        source = samples_dir / "plain.docx"
        document.save(str(source))

        output = samples_dir / "plain_out.docx"
        result = DocxHandler().process(source, output, engine)
        assert result.message is None


class TestSourceUnchanged:
    """源文件不变。"""

    def test_sha256一致(self, engine, samples_dir):
        source = make_table_sample()
        before = hashlib.sha256(source.read_bytes()).hexdigest()

        DocxHandler().process(source, samples_dir / "out.docx", engine)

        assert hashlib.sha256(source.read_bytes()).hexdigest() == before

    def test_dry_run不写文件(self, engine, samples_dir):
        source = make_table_sample()
        output = samples_dir / "out.docx"

        result = DocxHandler().process(source, output, engine, dry_run=True)

        assert not output.exists()
        assert result.output is None
        assert result.total_hits > 0


class TestSecurityBottomLine:
    """安全底线。"""

    def test_输出与源同路径被拒(self, engine, samples_dir):
        source = make_table_sample()
        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(source, source, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_SAME


class TestNotCovered:
    """边界：本轮之外的容器仍未处理，用测试钉住。"""

    def test_页眉页脚现已脱敏(self, engine, samples_dir):
        """回归：P3 时"页眉不处理"是边界，优先级 4 起应正常脱敏。"""
        document = Document()
        section = document.sections[0]
        section.header.is_linked_to_previous = False
        section.header.paragraphs[0].text = f"页眉电话 {FAKE_PHONE_1}"
        document.add_paragraph("正文")
        source = samples_dir / "header.docx"
        document.save(str(source))

        output = samples_dir / "header_out.docx"
        DocxHandler().process(source, output, engine)

        result = Document(str(output))
        assert FAKE_PHONE_1 not in result.sections[0].header.paragraphs[0].text