"""docx handler 单元测试（P1 第一版：正文段落 + 跨 run）。

重点验证：
    (a) 跨 run 手机号能被识别
    (b) 输出文件里原文消失
    (c) 未涉及的 run 格式保留（加粗还加粗、斜体还斜体）
    (d) 源文件字节级不变
    (e) 单 run 样例正常
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path
from typing import List

import pytest
from docx import Document
from docx.enum.text import WD_BREAK
from lxml import etree

from fixtures.make_docx_samples import (
    _set_split_phones,
    make_complex_sample,
    make_single_run_sample,
    make_split_runs_sample,
)
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
from redactor.handlers import get_handler
from redactor.handlers.base import HandlerError
from redactor.handlers.docx_handler import DocxHandler

BUILTIN_RULES = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


@pytest.fixture()
def engine():
    """内置规则引擎。"""
    return RedactionEngine(load_rules(BUILTIN_RULES))


@pytest.fixture()
def samples_dir(tmp_path, monkeypatch):
    """把样例生成到临时目录，避免污染仓库。"""
    import fixtures.make_docx_samples as module

    target = tmp_path / "samples"
    target.mkdir(parents=True, exist_ok=True)
    original = module.SAMPLES_DIR
    module.SAMPLES_DIR = target
    try:
        yield target
    finally:
        module.SAMPLES_DIR = original


def sha256(path: Path) -> str:
    """计算文件 SHA-256。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def all_text(path: Path) -> str:
    """读出 docx 全部正文段落文本（仅供测试断言）。"""
    document = Document(str(path))
    return "\n".join(p.text for p in document.paragraphs)


# WordprocessingML 命名空间
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 简单 run 允许的子元素（与 handler 的白名单一致）
_SIMPLE_RUN_CHILDREN = {"rPr", "t", "br", "tab", "cr"}


def find_empty_text_runs(docx_path: Path) -> List[List[str]]:
    """扫描整个 document.xml，找出"文本为空"的简单 run。

    判定条件（按需求约定）：
        1. run 的子元素只含 ``rPr`` / ``t`` / ``br`` / ``tab`` / ``cr``（简单 run）；
        2. 存在至少一个 ``<w:t>``；
        3. **所有** ``<w:t>`` 的 text 都为空或 None。

    满足以上三条即为"零宽度空 run"——它在 Word 里不渲染、选不中，
    用户感知就是"格式似乎存在却点不中"。

    实现用 lxml 解析而非正则：正则会依赖属性顺序、``xml:space`` 是否存在，
    且 ``\\s*`` 可能跨越标签误匹配。

    Args:
        docx_path: 待检查的 .docx 文件。

    Returns:
        发现的空 run 列表，每项是它的子元素名列表（便于定位）。
    """
    with zipfile.ZipFile(docx_path) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))

    offenders: List[List[str]] = []
    for run in root.iter(W_NS + "r"):
        child_names = [child.tag.replace(W_NS, "") for child in run]
        if not set(child_names) <= _SIMPLE_RUN_CHILDREN:
            continue  # 复杂 run（含图形/域代码等）不参与判定
        text_nodes = run.findall(W_NS + "t")
        if not text_nodes:
            continue
        if all((node.text or "") == "" for node in text_nodes):
            offenders.append(child_names)
    return offenders


class TestSplitRunsRecognition:
    """(a) 跨 run 识别。"""

    def test_能识别跨run手机号(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        rules = {s.rule_name for s in DocxHandler().process(source, samples_dir / "o2.docx", engine).hits}

        assert "手机号" in rules, "拆在 3 个 run 里的手机号必须被识别"

    def test_拆分点确实在run内部(self, samples_dir):
        """先确认样例本身的构造是对的：单个 run 内放不下完整号码。"""
        source = make_split_runs_sample()
        document = Document(str(source))
        paragraph = document.paragraphs[1]  # 标题占 paragraphs[0]

        texts = [run.text for run in paragraph.runs]
        #没有任何单个 run 能独立匹配 11 位手机号
        assert all(FAKE_PHONE_1 not in t for t in texts)
        # 但拼接后能还原出完整号码
        assert FAKE_PHONE_1 in "".join(texts)

    def test_单个run匹配确实会失败(self, engine, samples_dir):
        """反证：证明"逐 run 匹配"这条错误路线确实抓不到跨 run 的号码。

        这是本handler 存在意义的根据。若哪天 Word 改行为使这条断言失败，
        说明可以简化实现，但不应据此删除跨 run 逻辑。
        """
        document = Document(str(make_split_runs_sample()))
        paragraph = document.paragraphs[1]

        per_run_hits = 0
        for run in paragraph.runs:
            per_run_hits += len(engine.scan(run.text))
        assert per_run_hits == 0, "逐 run 匹配抓不到，说明跨 run 处理确有必要"

    def test_跨run邮箱也被识别(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"
        result = DocxHandler().process(source, output, engine)
        assert "邮箱" in {s.rule_name for s in result.hits}


class TestSplitRunsOutput:
    """(b) 输出文件里原文消失。"""

    def test_原文完全消失(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        text = all_text(output)

        assert FAKE_PHONE_1 not in text, "跨 run 手机号残留 = 漏脱敏"
        assert FAKE_PHONE_2 not in text
        assert FAKE_EMAIL not in text
        assert FAKE_ID_VALID not in text
        assert FAKE_BANK_CARD not in text
        assert FAKE_IP not in text

    def test_占位符正确写入(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        text = all_text(output)

        assert "[手机号]" in text
        assert "[邮箱]" in text
        assert "[身份证号]" in text
        assert "[银行卡号]" in text
        assert "[IP地址]" in text

    def test_占位符只出现一次不重复(self, engine, samples_dir):
        """占位符若被写进多个 run，会变成 "[手机号][手机号]"，必须避免。"""
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        text = all_text(output)

        # 样例中手机号共 2 处（拆run 的 + 单run 的），不应出现 3 次
        assert text.count("[手机号]") == 2, f"占位符重复：{text.count('[手机号]')}"

    def test_周围文字未被破坏(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        text = all_text(output)

        assert "联系人手机：" in text
        assert "（该号码被刻意拆成 3 个 run）" in text
        assert "身份证 [身份证号]，" in text

    def test_无敏感信息的段落完全不变(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        text = all_text(output)

        assert "本段落不含任何敏感信息，用于验证未被改动的段落保持原样。" in text
        assert "【格式对照组：这段加粗文字不含敏感信息，应保持加粗】" in text


class TestMultipleMatchesSameRun:
    """回归测试：同一 run 内多处命中（偏移基准漂移）。

    早期实现逐个命中处理，每处理一个就把该 run 的文本改短，
    导致后续命中的局部下标算在错误基准上——
    表现为长数字被"从中间切断"，敏感信息残留在输出里。
    这类失败最危险：文件看起来处理过了，实际仍有敏感信息。
    """

    @pytest.fixture()
    def multi_sample(self, samples_dir):
        """构造一个单run 内含4 处敏感信息的段落（顺序：身份证/银行卡/IP/手机）。"""
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run(
            f"身份证 {FAKE_ID_VALID}，银行卡 {FAKE_BANK_CARD}，"
            f"服务器 {FAKE_IP}，备用手机 {FAKE_PHONE_2}"
        )
        path = samples_dir / "multi.docx"
        document.save(str(path))
        return path

    def test_同run内多处命中全部正确替换(self, engine, multi_sample, samples_dir):
        output = samples_dir / "multi_out.docx"
        DocxHandler().process(multi_sample, output, engine)
        text = all_text(output)

        assert text == "身份证 [身份证号]，银行卡 [银行卡号]，服务器 [IP地址]，备用手机 [手机号]"

    def test_同run内多处命中无残留(self, engine, multi_sample, samples_dir):
        """这是本组用例的核心断言：任何一个号码都不能被切断残留。"""
        output = samples_dir / "multi_out.docx"
        DocxHandler().process(multi_sample, output, engine)
        text = all_text(output)

        for value in (FAKE_ID_VALID, FAKE_BANK_CARD, FAKE_IP, FAKE_PHONE_2):
            assert value not in text, f"敏感值残留（可能被从中间切断）：{value[:4]}..."

        # 特别检查银行卡与 IP 的衔接处：这里曾因偏移漂移而残留数字
        assert "银行卡 [银行卡号]，服务器 [IP地址]" in text, "银行卡与 IP 之间不应有残留数字"

    def test_同run内相邻命中的边界正确(self, engine, samples_dir):
        """两个敏感值紧邻时，中间不能留下空白或多余字符。"""
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run(f"甲{FAKE_PHONE_1}乙{FAKE_PHONE_2}丙")
        source = samples_dir / "adjacent.docx"
        document.save(str(source))

        output = samples_dir / "adjacent_out.docx"
        DocxHandler().process(source, output, engine)

        assert all_text(output) == "甲[手机号]乙[手机号]丙"

    def test_跨run与同run混合场景(self, engine, samples_dir):
        """跨 run 命中 + 同 run 多处命中同时存在时也不应错位。"""
        document = Document()
        p1 = document.add_paragraph()
        p1.add_run("联系")
        _set_split_phones(p1, FAKE_PHONE_1)  # 跨 run
        p1.add_run(f"，邮箱 {FAKE_EMAIL}")  # 同 run 内另一处

        p2 = document.add_paragraph()
        p2.add_run(f"身份证 {FAKE_ID_VALID} 银行卡 {FAKE_BANK_CARD}")

        source = samples_dir / "mixed.docx"
        document.save(str(source))

        output = samples_dir / "mixed_out.docx"
        DocxHandler().process(source, output, engine)

        assert "联系[手机号]，邮箱 [邮箱]" in all_text(output)
        assert "身份证 [身份证号] 银行卡 [银行卡号]" in all_text(output)


class TestFormatPreservation:
    """(c) 未涉及的 run 格式保留。"""

    def test_未涉及的加粗run仍是加粗(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        document = Document(str(output))

        bold_runs = [
            run
            for p in document.paragraphs
            for run in p.runs
            if run.bold and "格式对照组" in run.text
        ]
        assert bold_runs, "未被命中的加粗对照 run 应保持加粗"

    def test_未涉及的run数量减少只因空run被移除(self, engine, samples_dir):
        """跨 run 替换后，被清空的中间 run 会被移除，run 总数因此减少。

        这是**预期行为**：空 run 是零宽度的，留在文档里只会造成
        "格式看似存在却选不中"的困惑（用户实测发现的问题）。
        未被命中的 run 数量必须保持不变。
        """
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        before_doc = Document(str(source))
        before = [len(p.runs) for p in before_doc.paragraphs]
        DocxHandler().process(source, output, engine)
        after = [len(p.runs) for p in Document(str(output)).paragraphs]

        # 段 2（索引 1）跨 run 手机号：3个 run 中中间两个被清空并移除
        assert after[1] == before[1] - 2, f"段2 run数应减少 2（空 run 被移除）：{before[1]} -> {after[1]}"
        # 纯 run 段落（无命中）结构不变
        assert after[5] == before[5], "无命中的段落 run 数不应变化"
        assert after[0] == before[0], "标题段落 run 数不应变化"

    def test_跨run替换后占位符继承首个run的格式(self, engine, samples_dir):
        """占位符写进第一个涉及的 run，应继承那个 run 的加粗格式。"""
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        document = Document(str(output))

        paragraph = document.paragraphs[1]
        placeholder_runs = [run for run in paragraph.runs if "[手机号]" in run.text]
        assert len(placeholder_runs) == 1
        assert placeholder_runs[0].bold is True, "占位符应继承首个 run 的加粗"

    def test_下划线run在邮箱跨run替换后不再留空壳(self, engine, samples_dir):
        """邮箱拆成加粗+下划线两 run，下划线那个文本被清空后应被移除。

        注意 ``paragraph.text`` 会把 ``<w:br/>` 转成 ``\\n``，
        所以段落下标要与 :func:`all_text` 的行号对齐：段1 是标题，
        邮箱在第 4 个段落（索引 3）。
        """
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        document = Document(str(output))

        email_paragraph = document.paragraphs[3]
        texts = [run.text for run in email_paragraph.runs]
        # 加粗 run 承载占位符，下划线 run 被清空后移除，不应留下空文本
        assert any("[邮箱]" in t for t in texts)
        assert not any(t == "" for t in texts), f"不应残留空 run：{texts}"

    def test_跨run替换后不留空文本run(self, engine, samples_dir):
        """跨 run 替换时，中间/末尾的 run 文本被清空，这些 run 应被移除。

        保留零宽度空壳会让Word 里出现"格式似乎存在、却选不中"的困惑。
        """
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        document = Document(str(output))

        phone_paragraph = document.paragraphs[1]
        assert not any(run.text == "" for run in phone_paragraph.runs), "不应残留空文本 run"

    def test_字体颜色等其他格式不受影响(self, samples_dir):
        """未命中的 run 若有颜色设置，处理后应保持。"""
        from docx.shared import RGBColor

        document = Document()
        paragraph = document.add_paragraph()
        safe = paragraph.add_run("安全文字")
        safe.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)
        secret = paragraph.add_run(f" 手机 {FAKE_PHONE_1}")
        source = samples_dir / "color.docx"
        document.save(str(source))

        engine = RedactionEngine(load_rules(BUILTIN_RULES))
        output = samples_dir / "color_out.docx"
        DocxHandler().process(source, output, engine)

        result = Document(str(output))
        colored = [r for p in result.paragraphs for r in p.runs if r.font.color and r.font.color.rgb]
        assert colored, "红色 run 的颜色设置应保留"
        assert str(colored[0].font.color.rgb) == "FF0000"


class TestSingleRun:
    """(e) 单 run 样例（对照组）。"""

    def test_单run手机号被脱敏(self, engine, samples_dir):
        source = make_single_run_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        text = all_text(output)

        assert FAKE_PHONE_1 not in text
        assert "[手机号]" in text

    def test_单run场景占位符只写一次(self, engine, samples_dir):
        source = make_single_run_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        assert all_text(output).count("[手机号]") == 1

    def test_单run的加粗格式保留(self, engine, samples_dir):
        source = make_single_run_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        document = Document(str(output))

        bold = [r for p in document.paragraphs for r in p.runs if r.bold and "[手机号]" in r.text]
        assert bold, "单 run 被替换后应保持加粗"


class TestSourceUnchanged:
    """(d) 源文件字节级不变。"""

    def test_sha256处理前后一致(self, engine, samples_dir):
        source = make_split_runs_sample()
        before = sha256(source)

        DocxHandler().process(source, samples_dir / "out.docx", engine)
        assert sha256(source) == before, "源文件被修改了"

    def test_mtime不变(self, engine, samples_dir):
        source = make_split_runs_sample()
        before = source.stat().st_mtime_ns

        DocxHandler().process(source, samples_dir / "out.docx", engine)
        assert source.stat().st_mtime_ns == before

    def test_dry_run后源文件不变(self, engine, samples_dir):
        source = make_split_runs_sample()
        before = sha256(source)

        DocxHandler().process(source, samples_dir / "out.docx", engine, dry_run=True)
        assert sha256(source) == before


class TestDryRun:
    """dry-run 只统计不写文件。"""

    def test_dry_run不产生文件(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "never.docx"

        result = DocxHandler().process(source, output, engine, dry_run=True)

        assert not output.exists()
        assert result.output is None
        assert result.total_hits > 0, "dry-run 仍应给出完整统计"

    def test_dry_run统计与正式一致(self, engine, samples_dir):
        source = make_split_runs_sample()

        dry = DocxHandler().process(source, samples_dir / "a.docx", engine, dry_run=True)
        real = DocxHandler().process(source, samples_dir / "b.docx", engine)

        assert {s.rule_name: s.count for s in dry.hits} == {s.rule_name: s.count for s in real.hits}

    def test_dry_run不修改文档对象(self, engine, samples_dir):
        source = make_split_runs_sample()
        before = all_text(source)

        DocxHandler().process(source, samples_dir / "a.docx", engine, dry_run=True)
        assert all_text(source) == before


class TestBrTabHandling:
    """含 ``<w:br/>`` / ``<w:tab/>`` 的段落现在**正常处理**（优先级 1）。

    早期版本把这类段落整个跳过，导致真实文档里约 14% 段落未脱敏。
    现在：这些元素在文本流里各占 1 个占位字符，保证偏移对齐，
    替换时元素本身原样保留。
    """

    def test_含换行符段落中的手机号被脱敏(self, engine, samples_dir):
        """验收 (a)：含 br 的段落里，手机号能被识别并替换。"""
        source = make_complex_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        text = all_text(output)

        assert FAKE_PHONE_1 not in text, "含 br 的段落现在应当被脱敏"

    def test_换行符本身不被破坏(self, engine, samples_dir):
        """验收 (b)：br 换行还在。"""
        source = make_complex_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        document = Document(str(output))

        # paragraph.text 会把 <w:br/> 渲染成 \n，据此确认换行仍存在
        br_para = document.paragraphs[1].text
        assert "\n" in br_para, "<w:br/> 换行被破坏了"

    def test_换行符前后文字位置正确(self, engine, samples_dir):
        """验收 (c)：br 前后的文字顺序与位置正确。"""
        source = make_complex_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        document = Document(str(output))

        br_para = document.paragraphs[1].text
        assert "第一行文字" in br_para
        assert "换行符后面的手机 [手机号]" in br_para
        assert "最后一行" in br_para
        # 换行把段落分成三部分，顺序不能乱
        assert br_para.index("第一行文字") < br_para.index("换行符后面") < br_para.index("最后一行")

    def test_含制表符段落被脱敏且制表符保留(self, engine, samples_dir):
        source = make_complex_sample()
        output = samples_dir / "out.docx"

        DocxHandler().process(source, output, engine)
        document = Document(str(output))

        tab_para = document.paragraphs[2].text
        assert "\t" in tab_para, "<w:tab/> 制表符被破坏了"
        assert "制表符前" in tab_para
        assert FAKE_PHONE_2 not in tab_para
        assert "制表符后手机 [手机号]" in tab_para

    def test_复杂样例不再报告跳过(self, engine, samples_dir):
        """整个 br/tab 样例现在都应处理完毕，不该再提示"跳过"。"""
        source = make_complex_sample()
        output = samples_dir / "out.docx"

        result = DocxHandler().process(source, output, engine)
        assert result.message is None, f"br/tab 已支持，不该再提示跳过：{result.message}"

    def test_纯run文档不报告跳过(self, engine, samples_dir):
        source = make_split_runs_sample()
        result = DocxHandler().process(source, samples_dir / "out.docx", engine)
        assert result.message is None, "没有跳过任何段落时不应输出提示"

    def test_换行前后的手机号各自被正确替换(self, engine, samples_dir):
        """换行分隔的**两个独立**手机号应各自识别，换行保留。

        注意：号码本身被 ``<w:br/>`` 拆成两半时（``1380013\\n8000``）**不会**被识别——
        这是正确行为，因为跨换行的数字串不构成有效手机号，强行匹配反而会误伤。
        真实文档里不会存在被换行拆开的手机号。
        """
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run(f"第一个 {FAKE_PHONE_1}")
        paragraph.add_run().add_break(WD_BREAK.LINE)
        paragraph.add_run(f"第二个 {FAKE_PHONE_2}")
        source = samples_dir / "br_two.docx"
        document.save(str(source))

        output = samples_dir / "br_two_out.docx"
        DocxHandler().process(source, output, engine)
        result = Document(str(output))

        text = result.paragraphs[0].text
        assert FAKE_PHONE_1 not in text and FAKE_PHONE_2 not in text
        assert text.count("[手机号]") == 2, f"两个号码都应被替换：{text!r}"
        assert "\n" in text, "换行应保留"
        assert text.index("第一个") < text.index("第二个"), "换行前后顺序不能颠倒"

    def test_被换行拆开的号码不被误识别(self, engine, samples_dir):
        """锁住上面那条边界：跨换行的数字串不应被当成手机号。

        防止未来有人"为了提高召回率"放宽正则，导致误伤。
        """
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run("电话 1380013")
        paragraph.add_run().add_break(WD_BREAK.LINE)
        paragraph.add_run("8000 结束")
        source = samples_dir / "br_split.docx"
        document.save(str(source))

        output = samples_dir / "br_split_out.docx"
        result = DocxHandler().process(source, output, engine)

        # 不应产生命中，也不应改动内容
        assert result.total_hits == 0
        text = Document(str(output)).paragraphs[0].text
        assert "1380013" in text and "8000" in text, "未命中的内容不应被改动"
        assert "\n" in text


class TestEmptyRunRemoved:
    """回归测试：被删空的 run 必须从 XML 移除，不能留零宽度空壳。

    用户在 Word 中发现：手机号拆成"加粗/斜体/常规"三run 后，
    中间的斜体 run 文本被清空，I 按钮不高亮，看起来像"格式丢了"。
    实际是那个 run 变成零宽度、选不中。修复方式是删除空 run。
    """

    def test_被清空的run从xml中移除(self, engine, samples_dir):
        """跨 run 替换后，被清空的 run 不应留在 XML 里。

        **用 XML 解析而非正则**：正则依赖属性顺序、``xml:space`` 是否存在，
        且``\\s*`` 会跨越标签误匹配（实测曾把正常段落误报为空 run）。
        """
        source = make_split_runs_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)

        empty_runs = find_empty_text_runs(output)
        assert not empty_runs, f"仍有零宽度空 run：{empty_runs}"

    def test_全文档扫描无空run(self, engine, samples_dir):
        """遍历整个 document.xml，任何"只含 rPr/t 且文本为空"的 run 都算失败。

        这条比上面那条更严：覆盖整份文档的所有段落（含 br/tab 段落），
        不依赖"先找到手机号再截取段落"这种定位方式。
        """
        for builder in (make_split_runs_sample, make_single_run_sample, make_complex_sample):
            source = builder()
            output = samples_dir / f"{builder.__name__}.docx"
            DocxHandler().process(source, output, engine)

            empty_runs = find_empty_text_runs(output)
            assert not empty_runs, f"{builder.__name__} 产出空 run：{empty_runs}"

    def test_加粗占位符仍在(self, engine, samples_dir):
        """移除空 run 后，占位符本身与其加粗格式不能受影响。"""
        from docx import Document as _Doc

        source = make_split_runs_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        document = _Doc(str(output))

        placeholder = [r for p in document.paragraphs for r in p.runs if "[手机号]" in r.text]
        assert placeholder, "占位符应仍在"
        assert placeholder[0].bold is True, "占位符应保持加粗（继承首个 run）"

    def test_未命中的加粗run不受影响(self, engine, samples_dir):
        from docx import Document as _Doc

        source = make_split_runs_sample()
        output = samples_dir / "out.docx"
        DocxHandler().process(source, output, engine)
        document = _Doc(str(output))

        bold = [r for p in document.paragraphs for r in p.runs if r.bold and "格式对照组" in r.text]
        assert bold, "未被命中的加粗对照 run 应保持加粗"


class TestSecurityBottomLine:
    """安全底线。"""

    def test_输出与源同路径被拒(self, engine, samples_dir):
        source = make_split_runs_sample()
        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(source, source, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_SAME

    def test_输出已存在默认拒绝(self, engine, samples_dir):
        source = make_split_runs_sample()
        output = samples_dir / "exists.docx"
        output.write_bytes(b"placeholder")

        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(source, output, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_EXISTS

    def test_源文件损坏时归类为corrupted(self, engine, samples_dir):
        broken = samples_dir / "broken.docx"
        broken.write_bytes(b"this is definitely not a docx file")

        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(broken, samples_dir / "out.docx", engine)
        assert excinfo.value.code in (HandlerError.CORRUPTED, HandlerError.ENCRYPTED)

    def test_旧版doc被拒绝(self, engine, samples_dir):
        old = samples_dir / "old.doc"
        old.write_bytes(b"\xd0\xcf\x11\xe0legacy")

        with pytest.raises(HandlerError) as excinfo:
            DocxHandler().process(old, samples_dir / "out.docx", engine)
        assert excinfo.value.code in (HandlerError.CORRUPTED, HandlerError.ENCRYPTED)


class TestRegistry:
    """分派注册。"""

    def test_docx已注册(self):
        assert isinstance(get_handler(Path("a.docx")), DocxHandler)

    def test_大写扩展名也识别(self):
        assert isinstance(get_handler(Path("A.DOCX")), DocxHandler)

    def test_doc仍未支持(self):
        with pytest.raises(HandlerError):
            get_handler(Path("a.doc"))


class TestNoTableProcessing:
    """P1 第一版明确不处理表格——本组用例把这个边界钉死，避免误以为已覆盖。"""

    def test_表格内现已脱敏(self, engine, samples_dir):
        """回归：P1/P2 时"表格不处理"是边界，优先级 3 起应正常脱敏。

        同时锁定一个 python-docx 事实：
        ``document.paragraphs`` **不包含表格内的段落**，
        所以表格必须靠独立遍历才能覆盖。
        """
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
        # 确认 document.paragraphs 确实看不到表格内容（说明必须独立遍历）
        assert all(FAKE_PHONE_1 not in p.text for p in result.paragraphs)