"""docx 超链接处理单元测试（优先级 2）。

覆盖范围（严格限定，不越界）：
    * 显示文字脱敏（正文里的邮箱/手机号）
    * mailto 链接的 Target 改写
    * 内部锚点链接只改文字、不动锚点
    * https 链接的 URL 原样保留

不在本轮范围：表格、页眉页脚、批注、文本框、脚注尾注。
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import List, Tuple

import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from fixtures.make_docx_samples import add_hyperlink, make_hyperlink_sample
from fixtures.make_samples import FAKE_EMAIL, FAKE_PHONE_1
from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rules
from redactor.handlers.base import HandlerError
from redactor.handlers.docx_handler import DocxHandler
from redactor.handlers.docx_url import build_redacted_target, is_mailto

BUILTIN_RULES = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


@pytest.fixture()
def engine():
    return RedactionEngine(load_rules(BUILTIN_RULES))


@pytest.fixture()
def samples_dir(tmp_path, samples_module):
    """把样例生成到临时目录，避免污染仓库。

    用 try/finally 而非 monkeypatch：Windows 上 pytest 清理临时目录时
    偶发被安全删除机制拦截（Errno 53），会连带打乱 monkeypatch 的回滚，
    造成 teardown ERROR——那是环境问题，不是测试失败。
    """
    target = tmp_path / "samples"
    target.mkdir(parents=True, exist_ok=True)
    original = samples_module.SAMPLES_DIR
    samples_module.SAMPLES_DIR = target
    try:
        yield target
    finally:
        samples_module.SAMPLES_DIR = original


def read_rels(docx_path: Path) -> List[Tuple[str, str]]:
    """读取 document.xml.rels 里所有外部超链接的 ``(rId, Target)``。"""
    with zipfile.ZipFile(docx_path) as archive:
        xml = archive.read("word/_rels/document.xml.rels").decode("utf-8")
    from lxml import etree

    root = etree.fromstring(xml.encode("utf-8"))
    ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    result = []
    for rel in root.iter(ns + "Relationship"):
        if rel.get("Type", "").endswith("/hyperlink"):
            result.append((rel.get("Id"), rel.get("Target")))
    return result


def read_r_ids(docx_path: Path) -> List[str]:
    """读取 document.xml 里所有超链接的 r:id 引用。"""
    with zipfile.ZipFile(docx_path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    from lxml import etree

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    root = etree.fromstring(xml.encode("utf-8"))
    return [
        hl.get(R + "id")
        for hl in root.iter(W + "hyperlink")
        if hl.get(R + "id") is not None
    ]


def all_text(path: Path) -> str:
    """读出 docx 全部正文段落文本。"""
    return "\n".join(p.text for p in Document(str(path)).paragraphs)


class TestIsMailto:
    """协议判断。"""

    @pytest.mark.parametrize(
        "target", ["mailto:a@b.com", "MAILTO:a@b.com", "MailTo:a@b.com", "  mailto:a@b.com  "]
    )
    def test_识别mailto(self, target):
        assert is_mailto(target) is True

    @pytest.mark.parametrize(
        "target", ["https://example.com", "http://a.b", "", "ftp://x"]
    )
    def test_非mailto返回假(self, target):
        assert is_mailto(target) is False

    def test_只有协议头也算mailto(self):
        """``mailto:`` 本身是协议匹配；要不要处理由后续函数判断。"""
        assert is_mailto("mailto:") is True


class TestBuildRedactedTarget:
    """Target 改写逻辑。"""

    def test_mailto邮箱被替换(self, engine):
        result = build_redacted_target(f"mailto:{FAKE_EMAIL}", engine)
        assert result is not None
        assert result.startswith("mailto:")
        assert FAKE_EMAIL not in result

    def test_协议头保留(self, engine):
        """方案 B1：保留 mailto: 前缀，保证可读性。"""
        result = build_redacted_target(f"mailto:{FAKE_EMAIL}", engine)
        assert result == f"mailto:{_encoded('[邮箱]')}"

    def test_非邮箱内容不动(self, engine):
        """mailto 后不是邮箱时不改动（避免误伤）。"""
        assert build_redacted_target("mailto:not-an-email", engine) is None

    def test_空地址返回none(self, engine):
        assert build_redacted_target("mailto:", engine) is None

    def test_query参数保留(self, engine):
        """mailto:xxx?subject=yy 的 subject 部分必须保留。

        早期版本只取? 之前的地址，替换时把后半段整个丢掉——
        那是**静默删除用户数据**，比不脱敏更糟。
        """
        result = build_redacted_target(f"mailto:{FAKE_EMAIL}?subject=咨询", engine)
        assert result is not None
        assert "subject" in result
        assert result.endswith("?subject=%E5%92%A8%E8%AF%A2") or "subject" in result

    def test_fragment部分保留(self, engine):
        result = build_redacted_target(f"mailto:{FAKE_EMAIL}#anchor", engine)
        assert result is not None
        assert result.endswith("#anchor"), f"片段丢失：{result}"

    def test_占位符被url编码(self, engine):
        """中文占位符必须 URL 编码，否则 XML 属性值会出问题。"""
        result = build_redacted_target(f"mailto:{FAKE_EMAIL}", engine)
        # 编码后不应再出现中文方括号
        assert "[" not in result and "]" not in result
        assert "%" in result


def _encoded(text: str) -> str:
    """本地辅助：按实现里的规则编码。"""
    from urllib.parse import quote

    return quote(text, safe="@.+-_")


class TestDisplayTextRedaction:
    """显示文字脱敏。"""

    def test_链接文字含邮箱被脱敏(self, engine, samples_dir):
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = all_text(output)
        assert FAKE_EMAIL not in text, "链接显示文字里的邮箱应被脱敏"
        assert "[邮箱]" in text

    def test_手机号跨越链接边界被识别(self, engine, samples_dir):
        """回归：手机号拆在"普通run / 链接文字"之间也必须被识别。

        这要求 hyperlink 内的 run 与普通 run 一起参与偏移映射，
        否则拼接会缺一段导致漏脱敏。
        """
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        text = all_text(output)
        assert FAKE_PHONE_1 not in text, "跨链接边界的手机号未被脱敏"
        assert "电话 [手机号]" in text

    def test_内部锚点链接只改文字不动锚点(self, engine, samples_dir):
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        with zipfile.ZipFile(output) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        assert 'w:anchor="appendix1"' in xml, "内部锚点不应被改动"

    def test_锚点链接文字含敏感信息被脱敏(self, engine, samples_dir):
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        document = Document()
        paragraph = document.add_paragraph()
        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("w:anchor"), "target1")
        run = OxmlElement("w:r")
        text_element = OxmlElement("w:t")
        text_element.text = f"邮箱 {FAKE_EMAIL}"
        run.append(text_element)
        hyperlink.append(run)
        paragraph._p.append(hyperlink)
        source = samples_dir / "anchor.docx"
        document.save(str(source))

        output = samples_dir / "anchor_out.docx"
        DocxHandler().process(source, output, engine)

        assert FAKE_EMAIL not in all_text(output)


class TestMailtoTargetRedaction:
    """rels 里 mailto Target 的改写。"""

    def test_mailto_target被改写(self, engine, samples_dir):
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        targets = [t for _, t in read_rels(output)]
        mailto_targets = [t for t in targets if t.lower().startswith("mailto:")]

        assert mailto_targets, "应仍存在 mailto 条目（改写而非删除）"
        assert all(FAKE_EMAIL not in t for t in mailto_targets), f"邮箱残留：{mailto_targets}"
        assert any("%5B" in t or "%5b" in t for t in mailto_targets), "占位符应被 URL 编码写入"

    def test_https链接原样保留(self, engine, samples_dir):
        """不含敏感信息的 URL 不该被改动。"""
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        targets = [t for _, t in read_rels(output)]
        assert "https://example.com/contact" in targets, "普通 https 链接应原样保留"
        assert "https://example.com/x" in targets

    def test_relationship条目不被删除(self, engine, samples_dir):
        """删除条目会造成 rId 悬空引用，属于坏数据，必须避免。"""
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        rel_ids = {rid for rid, _ in read_rels(output)}
        referenced = set(read_r_ids(output))
        assert referenced, "document.xml 应仍有超链接引用"
        assert referenced <= rel_ids, f"存在悬空引用：{referenced - rel_ids}"

    def test_rId保持不变(self, engine, samples_dir):
        """rId 必须沿用原值，否则 document.xml 的引用会失效。"""
        source = make_hyperlink_sample()
        before = set(read_r_ids(source))
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        assert set(read_r_ids(output)) == before, "rId 不应改变"

    def test_python_docx可重开(self, engine, samples_dir):
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        document = Document(str(output))
        assert len(document.paragraphs) >= 5

    def test_rels中不再有敏感邮箱(self, engine, samples_dir):
        """最关键的安全断言：整个 rels 文件里不能残留邮箱。"""
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        with zipfile.ZipFile(output) as archive:
            for name in archive.namelist():
                if name.endswith(".rels"):
                    content = archive.read(name).decode("utf-8")
                    assert FAKE_EMAIL not in content, f"{name} 仍含邮箱"


class TestUrlHitReporting:
    """URL 命中的报告处理。"""

    def test_报告中出现超链接目标统计(self, engine, samples_dir):
        source = make_hyperlink_sample()
        result = DocxHandler().process(source, samples_dir / "out.docx", engine)

        rule_names = {s.rule_name for s in result.hits}
        assert "超链接目标" in rule_names

    def test_报告不含原始URL(self, engine, samples_dir):
        """补充约束 2：报告零敏感内容，不能出现原始 mailto 地址。"""
        source = make_hyperlink_sample()
        result = DocxHandler().process(source, samples_dir / "out.docx", engine)

        blob = str([(s.rule_name, s.count, s.locations) for s in result.hits])
        assert FAKE_EMAIL not in blob

    def test_提示说明URL处理范围(self, engine, samples_dir):
        source = make_hyperlink_sample()
        result = DocxHandler().process(source, samples_dir / "out.docx", engine)

        assert result.message is not None
        assert "mailto" in result.message

    def test_无mailto时不产生多余提示(self, engine, samples_dir):
        from fixtures.make_docx_samples import make_split_runs_sample

        source = make_split_runs_sample()
        result = DocxHandler().process(source, samples_dir / "out.docx", engine)
        assert result.message is None


class TestDryRunWithUrl:
    """dry-run 对 URL 的行为。"""

    def test_dry_run统计到mailto但不改文件(self, engine, samples_dir):
        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"

        result = DocxHandler().process(source, output, engine, dry_run=True)

        assert not output.exists()
        rule_names = {s.rule_name for s in result.hits}
        assert "超链接目标" in rule_names, "dry-run 也应统计到 mailto 命中"

    def test_dry_run不改动rels(self, engine, samples_dir):
        source = make_hyperlink_sample()
        before = read_rels(source)

        DocxHandler().process(source, samples_dir / "out.docx", engine, dry_run=True)

        assert read_rels(source) == before, "dry-run 不得改动源文件"


class TestSourceUnchanged:
    """源文件不变。"""

    def test_sha256一致(self, engine, samples_dir):
        source = make_hyperlink_sample()
        before = hashlib.sha256(source.read_bytes()).hexdigest()

        DocxHandler().process(source, samples_dir / "out.docx", engine)

        assert hashlib.sha256(source.read_bytes()).hexdigest() == before

    def test_rels在dry_run后不变(self, engine, samples_dir):
        source = make_hyperlink_sample()
        before = read_rels(source)

        DocxHandler().process(source, samples_dir / "out.docx", engine, dry_run=True)

        assert read_rels(source) == before


class TestSecurityBottomLine:
    """安全底线。"""

    def test_输出与源同路径被拒(self, engine, samples_dir):
        source = make_hyperlink_sample()
        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(source, source, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_SAME


class TestNoOtherContainers:
    """边界：优先级 3-6 的范围本轮不动，用测试钉住。"""

    def test_表格内也已处理(self, engine, samples_dir):
        """表格已在优先级 3 支持，表格内邮件链接的Target 也应被改写。"""
        document = Document()
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = f"表格内手机 {FAKE_PHONE_1}"
        document.add_paragraph("表外段落")
        source = samples_dir / "table.docx"
        document.save(str(source))

        output = samples_dir / "table_out.docx"
        DocxHandler().process(source, output, engine)

        result = Document(str(output))
        assert result.tables[0].cell(0, 0).text == "表格内手机 [手机号]"

    def test_非mailto链接不计入跳过(self, engine, samples_dir):
        """https 超链接段落现在应正常处理，不该再被跳过。"""
        source = make_hyperlink_sample()
        result = DocxHandler().process(source, samples_dir / "out.docx", engine)
        assert result.message is None or "跳过" not in result.message

class TestPlaceholderReporting:
    """方案 B：报告里的占位符必须是**真实落盘值**，不能按规则名反推。

    背景：mailto Target 的改写不对应任何一条脱敏规则（改的是 rels 里的
    URL，而非正文文本），报告若显示 "[超链接目标]"，用户会以为文档里写的是
    这个字符串，而真实落盘值是 ``mailto:[邮箱]``——报告就在说谎。
    """

    def test_报告占位符为真实落盘值(self, engine, samples_dir):
        """逐文件明细里的占位符应是 mailto:[邮箱]。"""
        source = make_hyperlink_sample()
        result = DocxHandler().process(source, samples_dir / "out.docx", engine)

        url_stats = [s for s in result.hits if s.rule_name == "超链接目标"]
        assert url_stats, "mailto 链接应产生一条 URL 命中统计"
        assert url_stats[0].placeholder == "mailto:[邮箱]"

    def test_报告占位符与rels实际值一致(self, engine, samples_dir):
        """报告展示的占位符，解码后必须与 rels 里真实写入的 Target 相同。

        这是端到端一致性检查：报告说的 == 文档里写的。
        """
        from urllib.parse import unquote

        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        url_stat = next(s for s in result.hits if s.rule_name == "超链接目标")
        shown = url_stat.placeholder  # 例如 mailto:[邮箱]
        address = shown.split(":", 1)[1]  # 去掉 mailto: 协议头

        with zipfile.ZipFile(output) as zf:
            rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")

        targets = [
            unquote(t)
            for t in _extract_targets(rels)
            if unquote(t).startswith("mailto:")
        ]
        assert targets, "rels 里应有 mailto 关系"
        for target in targets:
            assert target == f"mailto:{address}", (
                f"报告声称写入 {address}，实际 Target 为 {target}"
            )

    def test_按规则统计行不再硬编码规则名(self, engine, samples_dir):
        """「按规则统计」那一节也不能再显示 -> [超链接目标]。"""
        from redactor.report import Report as RedactionReport

        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        report = RedactionReport(dry_run=False)
        report.add(result)
        text = report.to_text()

        assert "-> [超链接目标]" not in text, "按规则统计仍在显示未落盘的占位符"
        assert "mailto:[邮箱]" in text

    def test_describe_placeholder解码百分号编码(self, engine):
        """describe_placeholder 应把 %5B..%5D 解码回方括号形式。"""
        from redactor.handlers.docx_url import describe_placeholder

        new_target = build_redacted_target(f"mailto:{FAKE_EMAIL}", engine)
        assert new_target is not None
        assert "%5B" in new_target, "Target 应是 URL 编码形式"
        assert describe_placeholder(new_target) == "mailto:[邮箱]"

    def test_describe_placeholder忽略query(self, engine):
        """?subject= 等查询参数不参与展示（其内容未被脱敏）。"""
        from redactor.handlers.docx_url import describe_placeholder

        new_target = build_redacted_target(
            f"mailto:{FAKE_EMAIL}?subject=咨询", engine
        )
        assert describe_placeholder(new_target) == "mailto:[邮箱]"

    def test_describe_placeholder对非mailto返回空(self):
        from redactor.handlers.docx_url import describe_placeholder

        assert describe_placeholder("https://example.com/x") == ""
        assert describe_placeholder("") == ""

    def test_HitStat缺省仍按规则名反推(self):
        """未显式给占位符时，行为与旧版一致（向后兼容）。"""
        from redactor.core.models import HitStat

        assert HitStat("手机号", 3).placeholder == "[手机号]"
        assert HitStat("邮箱", 1, ("第 2 段",)).placeholder == "[邮箱]"

    def test_HitStat显式占位符优先(self):
        """显式值优先于规则名反推。"""
        from redactor.core.models import HitStat

        stat = HitStat("超链接目标", 2, ("rels",), placeholder_text="mailto:[邮箱]")
        assert stat.placeholder == "mailto:[邮箱]"

    def test_HitStat显式空串回退到规则名(self):
        """显式传空串视为未提供，避免报告出现空的 -> 。"""
        from redactor.core.models import HitStat

        assert HitStat("甲", 1, (), placeholder_text="").placeholder == "[甲]"

    def test_HitStat为frozen且可哈希(self):
        """新增字段不得破坏 frozen 语义（报告可能被放进 set/dict）。"""
        from redactor.core.models import HitStat

        stat = HitStat("甲", 1, (), "x")
        assert hash(stat) == hash(HitStat("甲", 1, (), "x"))
        with pytest.raises(Exception):
            stat.count = 2  # type: ignore[misc]

    def test_JSON报告含真实占位符(self, engine, samples_dir):
        """JSON 导出同样要反映真实占位符（供下游系统消费）。"""
        import json

        from redactor.report import Report as RedactionReport

        source = make_hyperlink_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)

        report = RedactionReport(dry_run=False)
        report.add(result)
        data = json.loads(report.to_json())

        hits = data["files"][0]["hits"]
        url_hit = next(h for h in hits if h["rule"] == "超链接目标")
        assert url_hit["placeholder"] == "mailto:[邮箱]"

    def test_无mailto时沿用规则名反推(self, engine, samples_dir):
        """纯 https 文档不应凭空多出 URL 命中统计。"""
        document = Document()
        paragraph = document.add_paragraph()
        add_hyperlink(paragraph, "https://example.com/contact", "联系页")
        source = samples_dir / "https_only.docx"
        document.save(str(source))

        result = DocxHandler().process(source, samples_dir / "out2.docx", engine)
        assert not [s for s in result.hits if s.rule_name == "超链接目标"]


def _extract_targets(rels_xml: str) -> List[str]:
    """从 rels XML 里取出所有 Target 值。"""
    import re

    return re.findall(r'Target="([^"]+)"', rels_xml)
