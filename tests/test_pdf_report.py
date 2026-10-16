"""P1.4 + P1.5 的单元测试。

重点：
    - **跨页同偏移不得撞键少报**（Marvis 独立复现出来的缺陷）
    - **键不一致不得静默少报**（aggregate 的 strict 守卫）
    - 表格定位（find_tables）能给出行列号，且失败时静默降级
"""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.models import Match
from redactor.core.rules import load_rules
from redactor.handlers.pdf_common import (
    CellLocation,
    Rect,
    cell_for_rect,
    locate_cells,
)
from redactor.handlers.pdf_handler import PdfHandler, location_key

BUILTIN = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


def engine():
    return RedactionEngine(load_rules(BUILTIN))


def put_cn(page, point, text, size=11):
    page.insert_text(point, text, fontsize=size, fontname="china-s")


def make_multipage(path: Path, pages: int, *, same_layout: bool = True) -> Path:
    """合成多页 PDF。

    ``same_layout=True`` 时每页内容**完全相同**——这正是触发
    "跨页同偏移撞键"的情形：每页的手机号偏移都是同一个值。
    """
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        put_cn(page, (60, 100), "电话 13800138000")
        if not same_layout:
            put_cn(page, (60, 130), "页码标记 %d" % (i + 1))
    doc.save(str(path))
    doc.close()
    return path


def make_gridded_pdf(path: Path) -> Path:
    """合成一个带 3x2 框线表格的 PDF。"""
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    x0, y0, cw, ch = 50, 50, 100, 30
    for r in range(3):
        page.draw_line(fitz.Point(x0, y0 + r * ch),
                       fitz.Point(x0 + cw * 2, y0 + r * ch))
    for c in range(3):
        page.draw_line(fitz.Point(x0 + c * cw, y0),
                       fitz.Point(x0 + c * cw, y0 + ch * 2))
    put_cn(page, (60, 70), "甲方名称")
    put_cn(page, (160, 70), "13800138000")
    doc.save(str(path))
    doc.close()
    return path


# ============================================================== P1.5 跨页键
class TestCrossPageKey:
    """Marvis 缺陷：locations 键不含页码，跨页同布局会覆盖。"""

    def test_三页同布局应报三处(self, tmp_path):
        """二元键（不含页码）在本样本上只报 1 处；三元键报 3 处。"""
        pdf = make_multipage(tmp_path / "same.pdf", 3, same_layout=True)
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        assert stat.count == 3, (
            f"3 页同布局应报 3 处，实际 {stat.count} —— 位置键撞了"
        )
        assert len(stat.locations) == 3, "应有 3 个不同的位置标签"

    def test_十二页同布局应报十二处(self, tmp_path):
        """12 页同布局是最能暴露撞键的规模（合成样本）。"""
        pdf = make_multipage(tmp_path / "m12.pdf", 12, same_layout=True)
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        assert stat.count == 12, (
            f"12 页同布局应报 12 处，实际 {stat.count}"
        )
        page_nums = {loc.split()[1] for loc in stat.locations}
        assert len(page_nums) == 12, f"应覆盖 12 个不同页码，实际 {page_nums}"

    def test_位置标签含正确页码(self, tmp_path):
        pdf = make_multipage(tmp_path / "p3.pdf", 3, same_layout=True)
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        for expected in ("第 1 页", "第 2 页", "第 3 页"):
            assert any(expected in loc for loc in stat.locations), (
                f"位置标签应含 {expected}，实际 {stat.locations}"
            )

    @pytest.mark.parametrize("pages", [10, 11, 12])
    def test_十页以上页码不串(self, tmp_path, pages):
        """第 10 页以后页码不能被误读。

        曾经用字符串反解页码（从"第 N 页"里截数字），第 10 页会被
        截成第 1 页，导致多页文档只处理了第 1 页。
        """
        pdf = make_multipage(tmp_path / f"p{pages}.pdf", pages,
                             same_layout=True)
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        assert stat.count == pages
        assert any(f"第 {pages} 页" in loc for loc in stat.locations), (
            f"应有第 {pages} 页的标签，实际 {stat.locations}"
        )

    def test_location_key含页码(self):
        """键函数本身必须把 page_index 放进去。"""
        m0 = Match("手机号", "[手机号]", 100, 111, "13800138000", page_index=0)
        m1 = Match("手机号", "[手机号]", 100, 111, "13800138000", page_index=1)
        assert location_key(m0) != location_key(m1), (
            "同偏移不同页的键必须不同，否则会互相覆盖"
        )
        assert len(location_key(m0)) == 3

    def test_aggregate用三元键不压计数(self):
        """直接验证 aggregate：键含页码时，跨页同偏移不会被压成 1。"""
        from redactor.core.engine import RedactionEngine as RE

        matches = [
            Match("手机号", "[手机号]", 5, 16, "13800138000", page_index=i)
            for i in range(4)
        ]
        locations = {
            location_key(m): f"第 {i + 1} 页" for i, m in enumerate(matches)
        }
        stats = {
            s.rule_name: s
            for s in RE.aggregate(matches, locations, key_of=location_key)
        }
        assert stats["手机号"].count == 4

    def test_aggregate默认键行为不变(self):
        """不传 key_of 时保持原语义（默认键 = ``(rule_name, start)``）。

        注意：**只有 txt_handler 走这条默认键路径**。
        docx_handler 不调用 ``aggregate``，它用自建的 ``_merge_hits``。
        """
        from redactor.core.engine import RedactionEngine as RE

        matches = [
            Match("手机号", "[手机号]", 0, 11, "13800138000"),
            Match("手机号", "[手机号]", 50, 61, "13800138001"),
        ]
        locations = {("手机号", 0): "第 1 行", ("手机号", 50): "第 2 行"}
        stats = {s.rule_name: s for s in RE.aggregate(matches, locations)}
        assert stats["手机号"].count == 2
        assert stats["手机号"].locations == ("第 1 行", "第 2 行")


# ============================================================== aggregate 守卫
class TestAggregateGuard:
    """键不一致必须报错，不得静默少报（Marvis 发现的残留同类失效）。"""

    def test_键不一致必须抛错(self):
        """传三元键 locations 但忘传 key_of —— 必须抛错，不能静默丢命中。

        实测原始行为：24 处命中的报告直接变成 0 处，无异常、无告警。
        """
        from redactor.core.engine import RedactionEngine as RE

        matches = [
            Match("手机号", "[手机号]", 5, 16, "13800138000", page_index=i)
            for i in range(4)
        ]
        # locations 用三元键，但 key_of 缺省（二元键）-> 全部取不到标签
        locations = {
            location_key(m): f"第 {i + 1} 页" for i, m in enumerate(matches)
        }
        with pytest.raises(ValueError, match="取不到位置标签"):
            RE.aggregate(matches, locations)

    def test_部分命中取不到标签也要抛错(self):
        """哪怕只有一处对不上，也必须报错 —— 否则就是静默少报。"""
        from redactor.core.engine import RedactionEngine as RE

        matches = [
            Match("手机号", "[手机号]", 0, 11, "13800138000"),
            Match("邮箱", "[邮箱]", 50, 61, "a@b.com"),
        ]
        # 只给手机号标签，邮箱漏了
        locations = {("手机号", 0): "第 1 行"}
        with pytest.raises(ValueError, match="取不到位置标签"):
            RE.aggregate(matches, locations)

    def test_strict为False时降级但不影响count(self):
        """显式放宽时允许缺标签，但 count 仍必须是命中数。"""
        from redactor.core.engine import RedactionEngine as RE

        matches = [
            Match("手机号", "[手机号]", 0, 11, "13800138000"),
            Match("邮箱", "[邮箱]", 50, 61, "a@b.com"),
        ]
        locations = {("手机号", 0): "第 1 行"}
        stats = {s.rule_name: s for s in RE.aggregate(
            matches, locations, strict=False)}
        assert stats["手机号"].count == 1
        assert stats["邮箱"].count == 1, "缺标签也不得让 count 归零"

    def test_不传locations时不受影响(self):
        """locations=None 是合法用法（只要计数），不得抛错。"""
        from redactor.core.engine import RedactionEngine as RE

        matches = [
            Match("手机号", "[手机号]", 0, 11, "13800138000"),
            Match("手机号", "[手机号]", 50, 61, "13800138001"),
        ]
        stats = RE.aggregate(matches)
        assert stats[0].count == 2
        assert stats[0].locations == ()

    def test_同一标签多处命中count不缩水(self):
        """标签相同不等于命中相同 —— count 必须按命中数算。

        合成样本复现：一行内两个手机号，标签都是"第 1 行"，
        旧实现把 count 压成 1（实际删了 2 处）。
        """
        from redactor.core.engine import RedactionEngine as RE

        matches = [
            Match("手机号", "[手机号]", 0, 11, "13800138000"),
            Match("手机号", "[手机号]", 20, 31, "13900139000"),
        ]
        locations = {("手机号", 0): "第 1 行", ("手机号", 20): "第 1 行"}
        stats = RE.aggregate(matches, locations)
        assert stats[0].count == 2, "同一标签的两处命中不得被压成 1"
        assert stats[0].locations == ("第 1 行",), "标签仍应去重展示"

    def test_txt_handler同行两处命中不缩水(self, tmp_path):
        """端到端：txt 同行两个手机号，报告 count 必须是 2。"""
        from redactor.handlers.txt_handler import TxtHandler

        src = tmp_path / "two.txt"
        src.write_text("联系人 13800138000 备用 13900139000\n",
                       encoding="utf-8")
        out = tmp_path / "two_out.txt"
        result = TxtHandler().process(src, out, engine())
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        assert stat.count == 2, f"实际删了 2 处，报告 count={stat.count}"
        assert out.read_text(encoding="utf-8").count("[手机号]") == 2


# ============================================================== P1.4 表格
class TestTableLocation:
    """find_tables() 的行列号定位。"""

    def test_locate_cells空页面返回空(self):
        doc = fitz.open()
        doc.new_page()
        assert locate_cells(doc[0]) == []
        doc.close()

    def test_带框线表格能识别行列(self, tmp_path):
        pdf = make_gridded_pdf(tmp_path / "table.pdf")
        doc = fitz.open(str(pdf))
        cells = locate_cells(doc[0])
        doc.close()
        assert cells, "带框线表格应能识别出单元格"
        rows = {c.row for c in cells}
        cols = {c.col for c in cells}
        assert len(rows) >= 2, f"应识别出多行，实际 {sorted(rows)}"
        assert len(cols) >= 2, f"应识别出多列，实际 {sorted(cols)}"

    def test_cell_label用一基(self):
        assert CellLocation(0, 0, Rect(0, 0, 10, 10)).label() == "第 1 行第 1 列"
        assert CellLocation(2, 3, Rect(0, 0, 10, 10)).label() == "第 3 行第 4 列"

    def test_cell_for_rect命中与不命中(self):
        cells = [
            CellLocation(0, 0, Rect(0, 0, 100, 20)),
            CellLocation(0, 1, Rect(100, 0, 200, 20)),
        ]
        assert cell_for_rect(cells, Rect(10, 5, 30, 15)).col == 0
        assert cell_for_rect(cells, Rect(110, 5, 130, 15)).col == 1
        assert cell_for_rect(cells, Rect(500, 500, 600, 600)) is None
        assert cell_for_rect([], Rect(0, 0, 1, 1)) is None

    def test_表格识别失败不影响脱敏(self, tmp_path):
        """无框线页面 -> 表格识别不到，但脱敏必须照常。"""
        pdf = tmp_path / "plain.pdf"
        doc = fitz.open()
        put_cn(doc.new_page(), (60, 100), "电话 13800138000")
        doc.save(str(pdf))
        doc.close()

        out = tmp_path / "out.pdf"
        result = PdfHandler().process(pdf, out, engine())
        assert result.status == "ok"
        d = fitz.open(str(out))
        assert "13800138000" not in d[0].get_text()
        d.close()

    def test_表格内命中的位置标签(self, tmp_path):
        pdf = make_gridded_pdf(tmp_path / "t2.pdf")
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        for loc in stat.locations:
            assert "页" in loc, f"标签至少要含页码：{loc}"
        has_cell = any("行" in loc and "列" in loc for loc in stat.locations)
        assert has_cell, (
            f"带框线表格里应能给出行列号，实际 {stat.locations}"
        )


# ============================================================== 报告集成
class TestReportIntegration:
    def test_统计不含原文(self, tmp_path):
        pdf = make_multipage(tmp_path / "s.pdf", 2)
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        for stat in result.hits:
            assert "13800138000" not in repr(stat)

    def test_跨页多命中总数正确(self, tmp_path):
        pdf = make_multipage(tmp_path / "s.pdf", 5)
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        assert stat.count == 5

    def test_落盘后跨页仍全部删除(self, tmp_path):
        """报告对了还不够，落盘也必须对。"""
        pdf = make_multipage(tmp_path / "s.pdf", 12, same_layout=True)
        out = tmp_path / "out.pdf"
        PdfHandler().process(pdf, out, engine())
        d = fitz.open(str(out))
        assert d.page_count == 12
        for i in range(12):
            assert "13800138000" not in d[i].get_text(), f"第 {i+1} 页未删净"
        d.close()
        # L3 差分验证：源可检出 + 输出为 0（不是"原始字节搜明文"那种空转断言）
        from redactor.handlers.pdf_verify import verify_residue

        check = verify_residue(pdf, out, ["13800138000"])[0]
        assert check.detectable_in_source, "源应可检出"
        assert check.ok, check.describe()