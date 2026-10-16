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


# ============================================================== P2 表格识别
def make_unruled_pdf(path: Path) -> Path:
    """合成一个**无框线**的 4x3 表格（纯文本对齐）。"""
    doc = fitz.open()
    page = doc.new_page(width=500, height=340)
    rows = [("序号", "单位名称", "金额"),
            ("1", "重庆某某水务", "3106.64"),
            ("2", "某某监理", "8800.00"),
            ("3", "某某建工", "1200.00")]
    x0, y0, cw, ch = 50, 60, 130, 26
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            put_cn(page, (x0 + c * cw + 6, y0 + r * ch + 17), cell, size=10)
    doc.save(str(path))
    doc.close()
    return path


def make_toc_pdf(path: Path) -> Path:
    """合成一个**目录页**——它是 P2 的对抗性反例，不是表格。

    左侧章节名 + 右侧页码，形态与"无框线表格"完全一致：
    短文本、有数值列、无句读、首行像表头。四条文本判据全部满足。
    """
    doc = fitz.open()
    page = doc.new_page(width=500, height=340)
    items = [("第一章 项目概况", "1"), ("第二章 建设条件", "5"),
             ("第三章 工艺方案", "12"), ("第四章 投资估算", "23")]
    for i, (name, page_no) in enumerate(items):
        y = 70 + i * 22
        put_cn(page, (60, y), name, size=10)
        put_cn(page, (300, y), page_no, size=10)
    doc.save(str(path))
    doc.close()
    return path


class TestP2RuledOnly:
    """P2 核心决策：**行列号只给有框线表格**（路线 A）。

    背景（实测，见 ``.verify/probe_p2/``）：
        ``text`` 策略虽能"识别"无框线表格，但
        ① 行结构膨胀且两个口径矛盾（extract 视角 vs row.bbox 视角）
        ② 纯文本判据无法区分表格与列表——目录页/参数清单/两栏正文
           全都会被判成表格。
        因此**宁可少给信息，也不给错信息**。
    """

    def test_无框线表格不给行列号_这是有意的降级(self, tmp_path):
        """无框线表格应返回空 —— 不是缺陷，是路线 A 的取舍。"""
        pdf = make_unruled_pdf(tmp_path / "unruled.pdf")
        doc = fitz.open(str(pdf))
        cells = locate_cells(doc[0])
        doc.close()
        assert cells == [], (
            f"无框线表格**不应**给出行列号（路线 A），实际返回 "
            f"{len(cells)} 个单元格"
        )

    def test_目录页不给行列号(self, tmp_path):
        """对抗性反例：目录页绝不能被当表格编号。"""
        pdf = make_toc_pdf(tmp_path / "toc.pdf")
        doc = fitz.open(str(pdf))
        cells = locate_cells(doc[0])
        doc.close()
        assert cells == [], (
            f"目录页不是表格，不该被编号；实际返回 {len(cells)} 个单元格"
        )

    def test_有框线表格仍给行列号(self, tmp_path):
        """路线 A 不能把有框线表格一起砍掉。"""
        pdf = make_gridded_pdf(tmp_path / "ruled.pdf")
        doc = fitz.open(str(pdf))
        cells = locate_cells(doc[0])
        doc.close()
        assert cells, "有框线表格必须仍能识别出行列号"

    def test_显式传lines策略_不用text(self):
        """取证：源码里必须显式出现 lines 策略，且不出现 text 策略。

        这是**决策可检索**的保障——若有人改回 text 策略，
        本用例失败，强制其重新面对上面两条反例。
        """
        import inspect

        from redactor.handlers import pdf_common

        src = inspect.getsource(pdf_common.locate_cells)
        assert 'vertical_strategy="lines"' in src, \
            "locate_cells 必须显式传 vertical_strategy='lines'"
        assert 'horizontal_strategy="lines"' in src, \
            "locate_cells 必须显式传 horizontal_strategy='lines'"
        assert '"text"' not in src, (
            "locate_cells 不得使用 text 策略——"
            "实测它会把目录页/参数清单/两栏正文误判为表格"
        )

    def test_无框线表格里的命中仍被脱敏(self, tmp_path):
        """不给行列号 ≠ 不脱敏。脱敏绝不能因表格识别失败而受影响。"""
        pdf = make_unruled_pdf(tmp_path / "unruled2.pdf")
        # 无框线表格里塞一个手机号
        doc = fitz.open(str(pdf))
        put_cn(doc[0], (186, 145), "13800138000", size=10)
        doc.save(str(pdf), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        doc.close()

        out = tmp_path / "out.pdf"
        result = PdfHandler().process(pdf, out, engine())
        assert result.status == "ok"
        d = fitz.open(str(out))
        text = d[0].get_text()
        d.close()
        assert "13800138000" not in text, "无框线表格里的手机号必须被删掉"

    def test_原生提示被静音_子进程验证(self, tmp_path):
        """``find_tables()`` 的 C 层提示必须被吞掉（**子进程内**验证）。

        🔴 为什么必须开子进程（实测教训，勿改成同进程 capfd）：
            PyMuPDF 的提示 **每个进程只在第一次调用时打印一次**，实测：

                === 第1次调用 ===
                Consider using the pymupdf_layout package ...
                === 第2次调用 ===      <- 什么都不打
                === 第3次调用（新页）===  <- 什么都不打

            在 pytest 同进程里跑，前面的测试早已消费掉这唯一一次打印，
            于是 ``capfd`` 捕获为空 —— **用例恒真（空转断言）**。
            本用例最初正是这样写的，"去掉静音"的变异实验**没有失败**
            才被抓出来（见 ``.verify/probe_p2/probe_p2_mutation.txt`` M3）。
            且它写的是 **stdout**，不是 stderr（同见该文件）。

            故这里开一个**全新子进程**，并在其中：
              ① 裸跑一次 find_tables() -> 断言**能看到**提示（对照）
              ② 走 locate_cells()      -> 断言**看不到**提示（被测行为）
            两步在同一个子进程里，才能保证"提示本来会出现"这个前提成立。
        """
        import subprocess
        import sys

        script = (
            "import sys, fitz\n"
            "sys.path.insert(0, 'src')\n"
            "from redactor.handlers.pdf_common import locate_cells\n"
            "doc = fitz.open()\n"
            "p1 = doc.new_page()\n"
            "p1.insert_text((60,70),'x',fontname='china-s')\n"
            "p1.find_tables()\n"          # ① 裸跑，消费掉"只打印一次"的机会
            "p2 = doc.new_page()\n"
            "p2.insert_text((60,70),'y',fontname='china-s')\n"
            "locate_cells(p2)\n"          # ② 走被测函数
            "print('__DONE__')\n"
        )
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace",
        )
        combined = proc.stdout + proc.stderr
        assert "__DONE__" in combined, f"子进程未跑完：{combined!r}"
        assert "pymupdf_layout" in combined, (
            "对照失败：裸跑 find_tables() 应能看到提示，否则本用例无意义"
        )
        # 提示只出现 1 次（来自裸跑那一次）；locate_cells 不应再产生
        n = combined.count("pymupdf_layout")
        assert n == 1, (
            f"提示应只出现 1 次（裸跑那次），实际 {n} 次 —— "
            f"说明 locate_cells 的静音未生效：{combined!r}"
        )

    def test_静音后输出能恢复(self):
        """fd 级重定向必须恢复，否则会吞掉后续所有真实输出。"""
        import subprocess
        import sys

        script = (
            "import sys\n"
            "sys.path.insert(0, 'src')\n"
            "from redactor.handlers.pdf_common import _silence_native_noise\n"
            "with _silence_native_noise():\n"
            "    pass\n"
            "print('RESTORED_OUT')\n"
            "print('RESTORED_ERR', file=sys.stderr)\n"
        )
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace",
        )
        assert "RESTORED_OUT" in proc.stdout, (
            f"stdout 未恢复：stdout={proc.stdout!r}"
        )
        assert "RESTORED_ERR" in proc.stderr, (
            f"stderr 未恢复：stderr={proc.stderr!r}"
        )


# ============================================================== P2 报告集成
def make_adversarial_pdf(path: Path, kind: str) -> Path:
    """合成 4 类**对抗性反例**——它们都不是表格，但形态像表格。

    这些反例的作用是**锁定"无框线不给行列号"这个决策**：
    若有人改成 text 策略，它们会被误判为表格，本文件的用例立刻失败。
    """
    doc = fitz.open()
    page = doc.new_page(width=500, height=400)
    if kind == "toc":
        items = [("第一章 项目概况", "1"), ("第二章 建设条件", "5"),
                 ("第三章 工艺方案", "12"), ("第四章 投资估算", "23")]
        for i, (name, no) in enumerate(items):
            put_cn(page, (60, 70 + i * 22), name, size=10)
            put_cn(page, (300, 70 + i * 22), no, size=10)
    elif kind == "numlist":
        items = [("1", "项目概况"), ("2", "建设条件"),
                 ("3", "工艺方案"), ("4", "投资估算")]
        for i, (no, name) in enumerate(items):
            put_cn(page, (60, 70 + i * 22), no, size=10)
            put_cn(page, (100, 70 + i * 22), name, size=10)
    elif kind == "params":
        items = [("扬程", "12.5"), ("流量", "800"),
                 ("功率", "7.5"), ("转速", "1450")]
        for i, (k, v) in enumerate(items):
            put_cn(page, (60, 70 + i * 22), k, size=10)
            put_cn(page, (200, 70 + i * 22), v, size=10)
    elif kind == "contact":
        items = [("单位", "重庆某某水务"), ("电话", "13800138000"),
                 ("地址", "渝北区某某路"), ("邮编", "401120")]
        for i, (k, v) in enumerate(items):
            put_cn(page, (60, 70 + i * 22), k, size=10)
            put_cn(page, (200, 70 + i * 22), v, size=10)
    else:
        raise ValueError(f"未知反例类型：{kind}")
    doc.save(str(path))
    doc.close()
    return path


class TestP2AdversarialNotTables:
    """对抗性反例必须**被正确分类为非表格**（不给行列号）。

    方法论（本项目铁律）：
        "反例太弱，等于自己出题自己答对。"
        只测正例（有框线能识别）是**不够的**——必须同时测
        "不该识别的不会被识别"。因此这里逐个跑 4 类反例。
    """

    @pytest.mark.parametrize("kind", ["toc", "numlist", "params", "contact"])
    def test_反例不给行列号(self, tmp_path, kind):
        pdf = make_adversarial_pdf(tmp_path / f"{kind}.pdf", kind)
        doc = fitz.open(str(pdf))
        cells = locate_cells(doc[0])
        doc.close()
        assert cells == [], (
            f"反例 {kind} 不是表格，绝不能给行列号；"
            f"实际返回 {len(cells)} 个单元格"
        )

    @pytest.mark.parametrize("kind", ["toc", "numlist", "params", "contact"])
    def test_反例里的敏感信息仍被脱敏(self, tmp_path, kind):
        """不给行列号 ≠ 不脱敏。反例里的手机号必须照删。"""
        pdf = make_adversarial_pdf(tmp_path / f"{kind}.pdf", kind)
        out = tmp_path / f"{kind}_out.pdf"
        result = PdfHandler().process(pdf, out, engine())
        assert result.status == "ok"
        d = fitz.open(str(out))
        text = d[0].get_text()
        d.close()
        assert "13800138000" not in text, f"反例 {kind} 里的手机号必须被删"


class TestP2ReportTwoModes:
    """报告必须区分两种定位模式。"""

    def test_有框线表格报告给出行列号(self, tmp_path):
        pdf = make_gridded_pdf(tmp_path / "ruled.pdf")
        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        assert any("行" in loc and "列" in loc for loc in stat.locations), (
            f"有框线表格应给行列号，实际 {stat.locations}"
        )
        assert "定位" in (result.message or ""), (
            f"报告应说明定位模式，实际 message={result.message!r}"
        )
        assert "行列号" in (result.message or ""), (
            f"报告应提到行列号，实际 message={result.message!r}"
        )

    def test_无框线报告给坐标且说明不推断(self, tmp_path):
        pdf = tmp_path / "plain.pdf"
        doc = fitz.open()
        put_cn(doc.new_page(), (60, 100), "电话 13800138000")
        doc.save(str(pdf))
        doc.close()

        result = PdfHandler().process(
            pdf, tmp_path / "out.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        for loc in stat.locations:
            assert "页" in loc, f"至少要有页码：{loc}"
            assert "坐标" in loc, f"无框线应给坐标：{loc}"
            assert "行" not in loc or "列" not in loc, (
                f"无框线不得给行列号：{loc}"
            )
        msg = result.message or ""
        assert "坐标" in msg, f"报告应说明给了坐标，实际 {msg!r}"
        assert "不推断行列号" in msg, (
            f"报告应说明为何不给行列号，实际 {msg!r}"
        )

    def test_关闭表格模式时报告不定位(self, tmp_path):
        """table_mode=False 时不应出现定位说明。"""
        pdf = make_gridded_pdf(tmp_path / "ruled2.pdf")
        doc = fitz.open(str(pdf))
        handler = PdfHandler()
        _, _, _, diag = handler._scan(doc, engine(), pdf, table_mode=False)
        doc.close()
        assert handler.describe_location_mode(diag) == ""

    def test_坐标取自命中矩形_而非整页或文本块(self, tmp_path):
        """取证：坐标必须来自**实际被涂改的矩形**。

        为什么不给"估算的精确子串坐标"：
            比例估算（按字符数在 span 内插值）在**非等宽字体**上会偏，
            给出一个"看似精确、实际是错的"位置 —— 违反本项目铁律。
            因此只报**实际被涂改的区域**：它在任何字体下都不会错。

        为什么不用"文本块 bbox"：
            实测（``.verify/probe_p2/probe_p2_blocks.txt``）文本块**会重叠**：
            框线表格样本里块 1（整行）与块 4（手机号）都包含命中中心，
            用块坐标有歧义。

        本用例断言坐标等于 ``rects_for_hits`` 的结果（同一来源），
        且不等于整页。
        """
        from redactor.handlers.pdf_common import extract_spans

        pdf2 = tmp_path / "wide.pdf"
        doc = fitz.open()
        page = doc.new_page(width=900, height=200)
        put_cn(page, (60, 100), "备注：" + "x" * 40 + " 13800138000", size=10)
        doc.save(str(pdf2))
        doc.close()

        result = PdfHandler().process(
            pdf2, tmp_path / "out2.pdf", engine(), dry_run=True
        )
        stat = next(s for s in result.hits if s.rule_name == "手机号")
        loc = stat.locations[0]
        assert "坐标" in loc, f"应给坐标：{loc}"

        import re as _re

        m = _re.search(r"\((-?\d+),(-?\d+)\)-\((-?\d+),(-?\d+)\)", loc)
        assert m, f"坐标格式异常：{loc}"
        x0, y0, x1, y1 = (int(g) for g in m.groups())

        # 坐标必须与"实际被涂改的矩形"一致 —— 独立重算一遍做对照
        doc = fitz.open(str(pdf2))
        page = doc[0]
        spans, _ = extract_spans(page)
        span = next(s for s in spans if "13800138000" in s.text)
        page_rect = page.rect
        doc.close()

        assert abs(x0 - span.bbox.x0) <= 1 and abs(x1 - span.bbox.x1) <= 1, (
            f"坐标 {x0},{x1} 与命中 span 矩形 "
            f"{span.bbox.x0:.0f},{span.bbox.x1:.0f} 不符"
        )
        # 且不能是整页（否则定位毫无意义）
        assert not (x0 <= page_rect.x0 + 1 and x1 >= page_rect.x1 - 1
                    and y0 <= page_rect.y0 + 1 and y1 >= page_rect.y1 - 1), (
            f"坐标疑似整页，定位无意义：{loc}"
        )
        # 行高应当合理（说明取的是行内区域而非整页高度）
        assert (y1 - y0) < 40, f"坐标高度 {y1 - y0}pt 过大：{loc}"


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