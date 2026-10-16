"""PDF 处理器 P1.1/P1.2 的单元测试。

重点覆盖三条"关键要求"：
    1. **无文本层页面必须报错**，不静默通过
    2. **find_in_span 必须按 offset 换算全局坐标**（一个 span 可能只有1 个字）
    3. **必须遍历全部命中**（实测search_for 返回 3 个 rect 只处理 1 个会残留）
"""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.models import Match
from redactor.core.lexicon import load_lexicon_from_text
from redactor.core.rules import load_rules
from redactor.handlers.base import HandlerError
from redactor.handlers.pdf_common import (
    Rect,
    SpanInfo,
    TextBoxHit,
    extract_spans,
    merge_overlapping_rects,
    page_has_text_layer,
    rects_for_hits,
)
from redactor.handlers.pdf_handler import NO_TEXT_LAYER, PdfHandler

BUILTIN = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


def blank_pixmap() -> fitz.Pixmap:
    """构造一块灰色 Pixmap，用于模拟扫描件里的图像。

    不能用 ``insert_image(pixmap=None)`` —— 实测那会抛
    ``ValueError``，PyMuPDF 要求真实的 Pixmap 或文件。
    """
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40))
    pix.set_rect(pix.irect, (200, 200, 200))
    return pix


def put_cn(page: "fitz.Page", point: tuple, text: str, size: float = 12) -> None:
    """在页面上写中文。

    **必须显式指定 ``fontname="china-s"``**：PyMuPDF 默认用 Helvetica，
    它没有 CJK 字形，中文会被写成 ``··``（实测），
    导致"中文词条扫不到"这种假阴性。
    这与生产代码里写占位符用的是同一个内置字体。
    """
    page.insert_text(point, text, fontsize=size, fontname="china-s")


def make_pdf(path: Path, *, image_only: bool = False, pages: int = 1) -> Path:
    """合成一份测试 PDF。

    Args:
        path: 输出路径。
        image_only: True 时每页只放图像（模拟扫描件，无文本层）。
        pages: 页数。
    """
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        if image_only:
            page.insert_image(fitz.Rect(20, 20, 200, 200), pixmap=blank_pixmap())
        else:
            put_cn(page, (60, 100), "甲方重庆某某水务")
            put_cn(page, (60, 130), "电话 13800138000")
            put_cn(page, (60, 160), "邮箱 zhangsan@example.com")
    doc.save(str(path))
    doc.close()
    return path


def engine_with_lexicon():
    rules = load_rules(BUILTIN)
    entries = load_lexicon_from_text(
        "lexicon:\n  - name: 甲方单位\n    words: [重庆某某水务]\n",
        path=Path("<t>"),
    )
    return RedactionEngine(rules, lexicon_entries=entries)


# ============================================================== 分派
class TestDispatch:
    """扩展名分派。"""

    def test_pdf扩展名被识别(self, tmp_path):
        from redactor.handlers import get_handler

        assert isinstance(get_handler(tmp_path / "a.pdf"), PdfHandler)

    def test_pdf在支持列表中(self):
        from redactor.handlers import supported_extensions

        assert ".pdf" in supported_extensions()

    def test_can_handle大小写不敏感(self):
        assert PdfHandler.can_handle(Path("A.PDF"))
        assert PdfHandler.can_handle(Path("a.pdf"))


# ============================================================== 关键要求 1
class TestNoTextLayer:
    """无文本层（扫描件）必须报错，绝不静默通过。"""

    def test_纯扫描件报错(self, tmp_path):
        pdf = make_pdf(tmp_path / "scan.pdf", image_only=True)
        with pytest.raises(HandlerError) as exc:
            PdfHandler().process(pdf, tmp_path / "out.pdf", engine_with_lexicon())
        assert exc.value.code == NO_TEXT_LAYER
        assert "扫描件" in exc.value.message
        assert "OCR" in exc.value.message, "提示应告诉用户怎么办"

    def test_混合文档报错并指出页码(self, tmp_path):
        """第1 页有文本、第 2 页是图 —— 整份必须报错，不能只处理有文本的页。"""
        doc = fitz.open()
        p1 = doc.new_page()
        put_cn(p1, (60, 100), "电话 13800138000")
        p2 = doc.new_page()
        p2.insert_image(fitz.Rect(20, 20, 200, 200), pixmap=blank_pixmap())
        path = tmp_path / "mixed.pdf"
        doc.save(str(path))
        doc.close()

        with pytest.raises(HandlerError) as exc:
            PdfHandler().process(path, tmp_path / "out.pdf", engine_with_lexicon())
        assert exc.value.code == NO_TEXT_LAYER
        assert "第 2 页" in exc.value.message, "应指出是哪几页没有文本层"

    def test_报错时不产生输出文件(self, tmp_path):
        pdf = make_pdf(tmp_path / "scan.pdf", image_only=True)
        out = tmp_path / "out.pdf"
        with pytest.raises(HandlerError):
            PdfHandler().process(pdf, out, engine_with_lexicon())
        assert not out.exists(), "失败时不应留下输出文件"

    def test_加密PDF报错(self, tmp_path):
        doc = fitz.open()
        doc.new_page().insert_text((60, 100), "secret", fontsize=12)
        path = tmp_path / "enc.pdf"
        doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256,
                  owner_pw="o", user_pw="p")
        doc.close()
        with pytest.raises(HandlerError) as exc:
            PdfHandler().process(path, tmp_path / "out.pdf", engine_with_lexicon())
        assert exc.value.code == "encrypted_file"
        assert "密码" in exc.value.message

    def test_损坏文件报错(self, tmp_path):
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"not a pdf at all")
        with pytest.raises(HandlerError) as exc:
            PdfHandler().process(bad, tmp_path / "out.pdf", engine_with_lexicon())
        assert exc.value.code == "corrupted_file"


# ============================================================== 关键要求 2
class TestFindInSpan:
    """find_in_span 的坐标换算。"""

    def test_offset为0时等同find(self):
        engine = engine_with_lexicon()
        rec = engine.recognizers[0]
        direct = rec.find("电话 13800138000")
        via = rec.find_in_span("电话 13800138000", 0)
        assert [(m.start, m.end) for m in direct] == [(m.start, m.end) for m in via]

    def test_offset被加到start_end(self):
        engine = engine_with_lexicon()
        rec = engine.recognizers[0]
        direct = rec.find("电话 13800138000")
        shifted = rec.find_in_span("电话 13800138000", 1000)
        assert shifted[0].start == direct[0].start + 1000
        assert shifted[0].end == direct[0].end + 1000
        assert shifted[0].raw == direct[0].raw, "raw 不应随offset 改变"

    def test_空片段返回空(self):
        engine = engine_with_lexicon()
        assert engine.recognizers[0].find_in_span("", 100) == []

    def test_单字span也能匹配(self, tmp_path):
        """实测坑：真实 PDF 里一个 span 可能只有 1 个字（如单独成 span 的'年'）。

        注意：PyMuPDF 会把**相邻且同字体**的 span 合并，因此无法用
        insert_text 合成单字 span。故这里直接构造 :class:`SpanInfo` 对象，
        以真实的单字 span 形态验证 ``find_in_span`` 的换算逻辑——
        这才是 handler 实际会遇到的输入。
        """
        engine = engine_with_lexicon()
        phone_rec = next(r for r in engine.recognizers if r.find("13800138000"))

        # 模拟：11 个单字 span 拼成手机号，但它们【不】连续
        digits = "13800138000"
        spans = [
            SpanInfo(
                bbox=Rect(60 + i * 8, 100, 68 + i * 8, 112),
                text=ch,
                font="SimSun",
                size=12.0,
                offset=i,
            )
            for i, ch in enumerate(digits)
        ]
        assert all(len(s.text) == 1 for s in spans)

        # 逐 span 匹配：单字 span 匹配不到 11 位号码，符合预期
        per_span = [len(phone_rec.find_in_span(s.text, s.offset)) for s in spans]
        assert sum(per_span) == 0, "单字 span 内不该匹配到完整手机号"

        # 但若这些单字在页内是连续的，拼起来后仍应能定位到全局偏移
        joined = "".join(s.text for s in spans)
        hits = phone_rec.find(joined)
        assert len(hits) == 1
        assert hits[0].start == 0 and hits[0].end == 11

        # **跨 span 的命中找不到"单个完整覆盖它的 span"是正确行为**：
        # 11 字符的命中分散在 11 个单字 span 里。
        # 这正是 P1.3 的设计依据 —— redaction 必须自己把
        # "覆盖该命中的所有 span" 合并成一个大矩形，
        # 而不能只涂某一个 span（否则只盖住 1/11 的号码）。
        covered = [s for s in spans
                   if s.offset < hits[0].end and hits[0].start < s.end]
        assert len(covered) == 11, "该命中应横跨全部 11 个单字 span"
        merged = merge_overlapping_rects([s.bbox for s in covered])
        assert len(merged) == 1, "11 个 span 应合并成 1 个矩形"
        assert merged[0].x0 == spans[0].bbox.x0
        assert merged[0].x1 == spans[-1].bbox.x1

    def test_span为单字时find_in_span不崩(self):
        """单字 span 不能把 handler 打崩，哪怕匹配不到也要正常返回。"""
        engine = engine_with_lexicon()
        rec = engine.recognizers[0]
        one = SpanInfo(bbox=Rect(0, 0, 10, 10), text="年", font="SimSun",
                       size=11.0, offset=42)
        result = rec.find_in_span(one.text, one.offset)
        assert isinstance(result, list)
        for m in result:
            assert m.start >= one.offset, "全局坐标不得小于 span 起点"

    def test_span偏移连续无空洞(self, tmp_path):
        pdf = tmp_path / "t.pdf"
        make_pdf(pdf)
        doc = fitz.open(str(pdf))
        spans, whole = extract_spans(doc[0])
        doc.close()
        cursor = 0
        for span in spans:
            assert span.offset == cursor, "偏移必须连续递增"
            cursor += len(span.text)
        assert cursor == len(whole)

    def test_文本层判定(self, tmp_path):
        text_pdf = tmp_path / "t.pdf"
        make_pdf(text_pdf)
        scan_pdf = tmp_path / "s.pdf"
        make_pdf(scan_pdf, image_only=True)
        d1 = fitz.open(str(text_pdf))
        d2 = fitz.open(str(scan_pdf))
        assert page_has_text_layer(d1[0]) is True
        assert page_has_text_layer(d2[0]) is False
        d1.close()
        d2.close()


# ============================================================== 命中遍历
class TestScanHits:
    """扫描命中，覆盖规则与词库。"""

    def test_规则与词库都被扫到(self, tmp_path):
        pdf = make_pdf(tmp_path / "t.pdf")
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine_with_lexicon(), dry_run=True
        )
        names = {s.rule_name for s in result.hits}
        assert "甲方单位" in names, "词库词应被扫到"
        assert "手机号" in names
        assert "邮箱" in names

    def test_位置标签包含页码且每处唯一(self, tmp_path):
        """位置标签必须每处唯一 —— aggregate 会按标签去重，
        同页多处若都标"第 1 页"，count 会被压成 1（实现时踩过）。"""
        pdf = tmp_path / "t.pdf"
        make_pdf(pdf)
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine_with_lexicon(), dry_run=True
        )
        total = 0
        for stat in result.hits:
            for loc in stat.locations:
                assert "页" in loc, loc
            # 同一规则内的位置标签不得重复
            assert len(set(stat.locations)) == len(stat.locations), (
                f"{stat.rule_name} 位置标签重复: {stat.locations}"
            )
            total += stat.count
        assert total >= 3, "应收集到全部命中"

    def test_重复命中要全部收集(self, tmp_path):
        """实测坑 3：同一串出现多次时，每次都必须收集到，不能只取第一个。"""
        pdf = tmp_path / "rep.pdf"
        doc = fitz.open()
        page = doc.new_page()
        for i in range(4):
            page.insert_text((60, 80 + i * 20), "电话 13800138000", fontsize=12)
        doc.save(str(pdf))
        doc.close()

        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine_with_lexicon(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        assert stat.count == 4, f"应收集 4 处，实际 {stat.count}"

    def test_报告不含原文(self, tmp_path):
        """命中统计绝不能带原文。"""
        pdf = make_pdf(tmp_path / "t.pdf")
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine_with_lexicon(), dry_run=True
        )
        for stat in result.hits:
            assert "13800138000" not in repr(stat)
            assert "zhangsan@example.com" not in repr(stat)

    def test_P1_3应落盘(self, tmp_path):
        """P1.3 起真正产出 PDF。"""
        pdf = make_pdf(tmp_path / "t.pdf")
        out = tmp_path / "out.pdf"
        result = PdfHandler().process(pdf, out, engine_with_lexicon())
        assert out.exists(), "P1.3 起应产生输出文件"
        assert result.output == str(out)
        assert fitz.open(str(out)).page_count == 1

    def test_输出与源不同名且源未被改(self, tmp_path):
        pdf = make_pdf(tmp_path / "t.pdf")
        src_bytes = pdf.read_bytes()
        out = tmp_path / "out.pdf"
        PdfHandler().process(pdf, out, engine_with_lexicon())
        assert pdf.read_bytes() == src_bytes, "绝不能修改原文件"
        assert out.resolve() != pdf.resolve()

    def test_输出已存在时报错(self, tmp_path):
        pdf = make_pdf(tmp_path / "t.pdf")
        out = tmp_path / "out.pdf"
        out.write_bytes(b"existing")
        with pytest.raises(HandlerError) as exc:
            PdfHandler().process(pdf, out, engine_with_lexicon())
        assert exc.value.code == "output_exists"
        assert out.read_bytes() == b"existing", "报错时不得覆盖已有文件"

    def test_allow覆盖时才覆盖(self, tmp_path):
        pdf = make_pdf(tmp_path / "t.pdf")
        out = tmp_path / "out.pdf"
        out.write_bytes(b"existing")
        PdfHandler().process(
            pdf, out, engine_with_lexicon(), overwrite_output=True
        )
        assert out.read_bytes() != b"existing"


# ============================================================== P1.3 redaction
class TestRedaction:
    """落盘脱敏与三级验证。"""

    def _run(self, tmp_path, src: Path) -> Path:
        out = tmp_path / "out.pdf"
        PdfHandler().process(src, out, engine_with_lexicon())
        return out

    def test_L1_get_text无原文(self, tmp_path):
        """L1：文本层读不出原文。"""
        src = make_pdf(tmp_path / "t.pdf")
        out = self._run(tmp_path, src)
        doc = fitz.open(str(out))
        text = doc[0].get_text()
        doc.close()
        assert "13800138000" not in text
        assert "zhangsan@example.com" not in text
        assert "重庆某某水务" not in text

    def test_L2_search_for无命中(self, tmp_path):
        """L2：搜索索引里也找不到。"""
        src = make_pdf(tmp_path / "t.pdf")
        out = self._run(tmp_path, src)
        doc = fitz.open(str(out))
        assert len(doc[0].search_for("13800138000")) == 0
        assert len(doc[0].search_for("重庆某某水务")) == 0
        doc.close()

    def test_L3_字节流三种编码均无原文(self, tmp_path):
        """L3：底层字节流里也没有——这才是"真删"的证据。"""
        src = make_pdf(tmp_path / "t.pdf")
        out = self._run(tmp_path, src)
        raw = out.read_bytes()
        for needle in ("13800138000", "zhangsan@example.com"):
            for enc in ("utf-8", "gbk", "utf-16-be"):
                assert raw.count(needle.encode(enc)) == 0, (
                    f"{needle} 以 {enc} 编码仍残留在字节流中"
                )

    def test_占位符已写入(self, tmp_path):
        src = make_pdf(tmp_path / "t.pdf")
        out = self._run(tmp_path, src)
        doc = fitz.open(str(out))
        text = doc[0].get_text()
        doc.close()
        for ph in ("[手机号]", "[邮箱]", "[甲方单位]"):
            assert ph in text, f"占位符 {ph} 未写入"

    def test_多处命中全部处理(self, tmp_path):
        """硬要求 4：必须遍历全部 rect。"""
        pdf = tmp_path / "rep.pdf"
        doc = fitz.open()
        page = doc.new_page()
        for i in range(5):
            page.insert_text((60, 80 + i * 25), "电话 13800138000",
                             fontsize=11, fontname="china-s")
        doc.save(str(pdf))
        doc.close()

        out = self._run(tmp_path, pdf)
        d = fitz.open(str(out))
        text = d[0].get_text()
        assert "13800138000" not in text, "5 处应全部删除"
        assert text.count("[手机号]") == 5, f"应写入 5 个占位符，实际 {text.count('[手机号]')}"
        d.close()
        raw = out.read_bytes()
        assert raw.count("13800138000".encode("utf-8")) == 0

    def test_多页PDF逐页处理(self, tmp_path):
        pdf = tmp_path / "multi.pdf"
        doc = fitz.open()
        for i in range(3):
            page = doc.new_page()
            page.insert_text((60, 100), "电话 13800138000",
                             fontsize=11, fontname="china-s")
        doc.save(str(pdf))
        doc.close()

        out = self._run(tmp_path, pdf)
        d = fitz.open(str(out))
        assert d.page_count == 3
        for i in range(3):
            assert "13800138000" not in d[i].get_text(), f"第 {i+1} 页未处理"
        d.close()

    def test_跨span命中覆盖完整(self, tmp_path):
        """硬要求 5：命中横跨多个 span 时必须合并矩形，不能只涂第一个。"""
        # 手工构造：一个命中跨 3 个 span
        from redactor.handlers.pdf_common import SpanInfo as SI

        spans = [
            SI(bbox=Rect(10, 10, 30, 22), text="13800", font="f",
               size=12.0, offset=0),
            SI(bbox=Rect(30, 10, 50, 22), text="1380", font="f",
               size=12.0, offset=5),
            SI(bbox=Rect(50, 10, 80, 22), text="00", font="f",
               size=12.0, offset=10),
        ]
        joined = "".join(s.text for s in spans)
        hit = TextBoxHit(
            rule_name="X",
            placeholder="[X]",
            span=spans[0],
            start_in_span=0,
            end_in_span=11,
            match=Match("X", "[X]", 0, 11, joined),
            covering_spans=tuple(spans),
        )
        rects = rects_for_hits([hit])
        assert len(rects) == 1, "3 个同行 span 应合并为 1 个矩形"
        r = rects[0]
        assert r.x0 == 10 and r.x1 == 80, (
            f"合并矩形应覆盖全部 span，实际 ({r.x0}, {r.x1})"
        )

    def test_占位符比原串长也能写入(self, tmp_path):
        """自适应扩框：极窄原框 + 长占位符。

        注意 ``insert_textbox`` 会在框宽不足时**自动折行**，
        所以读回可能是 ``'[甲方单位名\\n称]'``（中间带换行）。
        故此处校验"去掉换行后等于占位符"，而非直接比对原字符串。
        """
        pdf = tmp_path / "narrow.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((60, 60), "甲", fontsize=11, fontname="china-s")
        doc.save(str(pdf))
        doc.close()

        from redactor.handlers.pdf_common import write_placeholder
        doc = fitz.open(str(pdf))
        page = doc[0]
        target = page.search_for("甲")[0]
        assert target.x1 - target.x0 < 20, "原框应很窄"
        page.add_redact_annot(target)
        page.apply_redactions()
        ok = write_placeholder(
            page, target, "[甲方单位名称]", page_rect=page.rect
        )
        assert ok, "自适应扩框应能写入长占位符"
        written = page.get_text().replace("\n", "")
        assert "[甲方单位名称]" in written, f"实际写入 {written!r}"
        assert "甲\n" not in page.get_text(), "原字应已删除"
        doc.close()

    def test_同一span内多个命中的占位符不重叠(self, tmp_path):
        """整行只有 1 个 span 时，同行多个命中必须错开写入。

        实测踩过：整行 1 个 span（``'重复出现：13800138000 与13800138000'``）
        里两个手机号命中同一span，若都按合并矩形写占位符，
        会写到**同一位置**，渲染图上只剩一个。
        修法是占位符按子串位置定位（``placeholder_anchor``）。
        """
        pdf = tmp_path / "same_span.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((60, 100), "重复：13800138000 与 13800138000",
                         fontsize=11, fontname="china-s")
        doc.save(str(pdf))
        doc.close()

        out = tmp_path / "out.pdf"
        PdfHandler().process(pdf, out, engine_with_lexicon())

        d = fitz.open(str(out))
        spans, _ = extract_spans(d[0])
        d.close()
        placeholders = [s for s in spans if "[手机号]" in s.text]
        assert len(placeholders) == 2, f"应写入 2 个占位符，实际 {len(placeholders)}"
        xs = sorted(s.bbox.x0 for s in placeholders)
        assert xs[1] - xs[0] > 20, (
            f"两个占位符应错开，实际 x={xs} —— 写到了同一位置"
        )

    def test_写入失败返回False而非静默(self):
        """硬要求 2：insert_textbox 失败必须能被调用方察觉。"""
        from redactor.handlers.pdf_common import write_placeholder

        doc = fitz.open()
        page = doc.new_page(width=100, height=60)
        page.insert_text((10, 30), "abc", fontsize=11)
        # 给一个极小且被页面边界夹住的框
        tiny = Rect(0, 0, 3, 3)
        ok = write_placeholder(page, tiny, "[非常长的占位符文本]", page_rect=page.rect)
        assert ok is False, "放不下时必须返回 False，不能静默"
        doc.close()


# ============================================================== 矩形合并
class TestMergeRects:
    def test_空列表返回空(self):
        assert merge_overlapping_rects([]) == []

    def test_同行相邻合并(self):
        a = Rect(10, 10, 30, 20)
        b = Rect(30, 10, 50, 20)
        merged = merge_overlapping_rects([a, b])
        assert len(merged) == 1
        assert merged[0].x0 == 10 and merged[0].x1 == 50

    def test_同行分离不合并(self):
        a = Rect(10, 10, 20, 20)
        b = Rect(100, 10, 120, 20)
        assert len(merge_overlapping_rects([a, b])) == 2

    def test_跨行不合并(self):
        """跨行合并会连带盖掉别的行，绝对不能合。"""
        a = Rect(10, 10, 100, 20)
        b = Rect(10, 30, 100, 40)
        assert len(merge_overlapping_rects([a, b])) == 2

    def test_重叠矩形合并(self):
        a = Rect(10, 10, 40, 20)
        b = Rect(30, 10, 60, 20)
        merged = merge_overlapping_rects([a, b])
        assert len(merged) == 1
        assert merged[0].x0 == 10 and merged[0].x1 == 60
