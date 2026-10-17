"""v0.6.3 预览命中表 + 打开输出 + 双击打开 全套测试。

6 组（与 v0.6.3_方案.md 第 7 节对齐）：
    A. TestPreviewPanel          命中表基础行为（行数 / 列名 / tooltip / 截断）
    B. TestPreviewSecurity       ★ 防敏感内容泄漏到 GUI（tooltip vs display）
    C. TestOpenPath              _open_path 封装（存在 / 不存在 / 目录 / 无默认程序）
    D. TestDoubleClickOpen       双击打开（已产出开输出 / 未产出开源源 / 回填）
    E. TestMainWindowPreview     MainWindow 集成（命中表填充 / Start 前清空 / 汇总）
    F. TestReverseValidation     变异后守护判据必须红

跑法：
    scripts\\test.bat tests/test_gui_preview.py
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List

import pytest

from PySide6 import QtCore, QtGui, QtWidgets

from redactor.core.models import FileResult, HitStat
from redactor.gui.widgets import DropZoneList, PreviewPanel


# ============================================================ fixture
@pytest.fixture
def qapp():
    """QApplication 单例（与 test_gui_process.py 同源；不依赖 pytest-qt）。"""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    app.processEvents()


def _make_docx(path: Path, text: str = "张三 13812345678") -> None:
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    d.save(str(path))


def _make_corrupted_docx(path: Path) -> None:
    path.write_bytes(b"this is not a docx")


def _wait_until(predicate, timeout_s: float = 15.0, step_ms: int = 50) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtWidgets.QApplication.processEvents(
            QtCore.QEventLoop.ProcessEventsFlag.AllEvents, step_ms
        )
    return predicate()



def _same_path(a: str, b: Path | str) -> bool:
    """路径等价比较（忽略分隔符差异 + Windows 大小写）。

    QUrl.toLocalFile() 返回正斜杠路径，``str(Path)`` 是反斜杠，
    直接比字符串会在 Windows 上假失败。
    """
    import os
    return os.path.normcase(os.path.normpath(str(a))) == \
        os.path.normcase(os.path.normpath(str(b)))


@pytest.fixture
def patch_openurl(monkeypatch):
    """替换 MainWindow._desktop_open（QDesktopServices 静态方法 patch 不生效）。

    返回记录到的本地路径列表。
    """
    from redactor.gui import MainWindow
    calls: List[str] = []
    monkeypatch.setattr(
        MainWindow, "_desktop_open",
        lambda self, url: (calls.append(str(Path(url.toLocalFile()))), True)[1],
    )
    return calls


@pytest.fixture
def patch_openurl_fail(monkeypatch):
    """_desktop_open 一律返回 False（模拟系统无默认程序）。"""
    from redactor.gui import MainWindow
    calls: List[str] = []
    monkeypatch.setattr(
        MainWindow, "_desktop_open",
        lambda self, url: (calls.append(str(Path(url.toLocalFile()))), False)[1],
    )
    return calls


# ============================================================ A. PreviewPanel 基础
class TestPreviewPanel:
    def test_默认命中表上限500(self, qapp):
        p = PreviewPanel()
        assert p.hit_limit() == 500
        assert PreviewPanel.DEFAULT_HIT_LIMIT == 500

    def test_命中表4列_列名正确(self, qapp):
        p = PreviewPanel()
        p.add_hit("a.docx", HitStat(rule_name="手机号", count=2))
        assert p.hit_count() == 1
        # 4 列：# / 文件 / 规则 / 位置
        assert p.cell_text(0, 0) == "1"
        assert p.cell_text(0, 1) == "a.docx"
        assert p.cell_text(0, 2) == "手机号"
        assert p.cell_text(0, 3) == "2 处"

    def test_列头文本(self, qapp):
        p = PreviewPanel()
        table = p._hits_table
        heads = [table.horizontalHeaderItem(i).text()
                 for i in range(table.columnCount())]
        assert heads == ["#", "文件", "规则", "位置"]

    def test_标记裁定1_locations不展开_收进tooltip(self, qapp):
        """位置列只显示"N 处"，具体位置在 tooltip。"""
        p = PreviewPanel()
        hit = HitStat(rule_name="手机号", count=3,
                      locations=("第1段", "第2段", "第3段"))
        p.add_hit("a.docx", hit)
        # 位置列：不展开
        assert p.cell_text(0, 3) == "3 处"
        assert "第1段" not in p.cell_text(0, 3)
        # tooltip：含 locations
        tip = p.cell_tooltip(0, 3)
        assert "第1段" in tip
        assert "第2段" in tip
        assert "第3段" in tip

    def test_clear_hits_清空计数与提示(self, qapp):
        p = PreviewPanel()
        for i in range(3):
            p.add_hit(f"f{i}.docx", HitStat(rule_name="手机号", count=1))
        assert p.hit_count() == 3
        p.clear_hits()
        assert p.hit_count() == 0
        assert p.hits_dropped() == 0
        assert p.notice_visible() is False

    def test_add_result_追加N行(self, qapp):
        p = PreviewPanel()
        result = FileResult(
            source="C:/tmp/a.docx",
            status="ok",
            hits=[
                HitStat(rule_name="手机号", count=2, locations=("第1段",)),
                HitStat(rule_name="身份证号", count=1, locations=("第3段",)),
            ],
        )
        added = p.add_result("C:/tmp/a.docx", result)
        assert added == 2
        assert p.hit_count() == 2
        assert p.cell_text(0, 1) == "a.docx"   # 只写文件名，不写绝对路径
        assert "C:" not in p.cell_text(0, 1)

    # ---------------- 500 行截断（裁定 4）----------------
    def test_标记裁定4_超500条截断且顶部提示(self, qapp):
        p = PreviewPanel()
        for i in range(520):
            p.add_hit("big.docx", HitStat(rule_name="手机号", count=1))
        assert p.hit_count() == 500, f"应截断到 500，实际 {p.hit_count()}"
        assert p.hits_dropped() == 20
        assert p.notice_visible() is True
        notice = p.notice_text()
        assert "仅展示前 500 条" in notice
        assert "520" in notice

    def test_截断后第501条不建行但计数增加(self, qapp):
        p = PreviewPanel()
        for i in range(500):
            p.add_hit("a.docx", HitStat(rule_name="手机号", count=1))
        assert p.hit_count() == 500
        assert p.hits_dropped() == 0
        assert p.notice_visible() is False
        # 第 501 条
        assert p.add_hit("a.docx", HitStat(rule_name="邮箱", count=1)) is False
        assert p.hit_count() == 500
        assert p.hits_dropped() == 1
        assert p.notice_visible() is True

    def test_set_hit_limit_可调(self, qapp):
        p = PreviewPanel()
        p.set_hit_limit(3)
        assert p.hit_limit() == 3
        for i in range(10):
            p.add_hit("a.docx", HitStat(rule_name="手机号", count=1))
        assert p.hit_count() == 3
        assert p.hits_dropped() == 7

    # ---------------- 输出列表 ----------------
    def test_add_output_path_与output_paths(self, qapp):
        p = PreviewPanel()
        p.add_output_path("C:/out/a_redacted.docx")
        p.add_output_path("C:/out/b_redacted.docx")
        assert p.output_count() == 2
        assert p.output_paths() == [
            "C:/out/a_redacted.docx", "C:/out/b_redacted.docx",
        ]

    def test_add_output_path_空串忽略(self, qapp):
        p = PreviewPanel()
        p.add_output_path("")
        assert p.output_count() == 0

    def test_activate_output_row_发信号(self, qapp):
        p = PreviewPanel()
        got: List[str] = []
        p.output_activated.connect(got.append)
        p.add_output_path("C:/out/a.docx")
        assert p.activate_output_row(0) == "C:/out/a.docx"
        assert got == ["C:/out/a.docx"]


# ============================================================ B. 安全（★ 不泄漏原文）
class TestPreviewSecurity:
    """GUI 里绝不出现任何原文 —— 由 HitStat 结构保证，这里守住它。"""

    PHONE = "13812345678"

    def test_标记表格display不含任何号码(self, qapp):
        p = PreviewPanel()
        hit = HitStat(rule_name="手机号", count=1,
                      locations=(f"第1段（{self.PHONE}）",))
        p.add_hit("a.docx", hit)
        # 全表扫一遍，任何单元格都不含原文
        for r in range(p.hit_count()):
            for c in range(4):
                assert self.PHONE not in p.cell_text(r, c), \
                    f"单元格({r},{c}) 泄漏了原文：{p.cell_text(r, c)!r}"

    def test_标记locations只进tooltip(self, qapp):
        """locations 文本（位置描述）应出现在 tooltip，不在 display。"""
        p = PreviewPanel()
        hit = HitStat(rule_name="手机号", count=1, locations=("第42段",))
        p.add_hit("a.docx", hit)
        # tooltip 里有
        assert "第42段" in p.cell_tooltip(0, 3)
        # display 里没有
        assert "第42段" not in p.cell_text(0, 3)

    def test_标记FileResult带原文消息也不进表格(self, qapp):
        """FileResult.message 可能含原文（如 corrupted 包的路径），不进表格。"""
        p = PreviewPanel()
        result = FileResult(
            source="C:/tmp/a.docx",
            status="failed",
            message=f"文件损坏：{self.PHONE}",
            hits=[HitStat(rule_name="手机号", count=1)],
        )
        p.add_result("C:/tmp/a.docx", result)
        for r in range(p.hit_count()):
            for c in range(4):
                assert self.PHONE not in p.cell_text(r, c)

    def test_标记变异_locations进display_测试真失败(self, qapp, monkeypatch):
        """反向对照：把 locations 改成进 display → 上面第 2 项必须红。

        本测试主动验证 ``test_locations只进tooltip`` 有鉴别力。
        """
        def evil_add_hit(self, file_name, hit, row_no=None):
            self._hits_seen += 1
            locations = tuple(getattr(hit, "locations", ()) or ())
            r = self._hits_table.rowCount()
            self._hits_table.insertRow(r)
            # 变异：把 locations 塞进 display
            pos_display = ", ".join(locations) if locations else ""
            for c, val in enumerate(["1", file_name,
                                     getattr(hit, "rule_name", ""), pos_display]):
                self._hits_table.setItem(r, c, QtWidgets.QTableWidgetItem(val))
            return True

        monkeypatch.setattr(PreviewPanel, "add_hit", evil_add_hit)

        p = PreviewPanel()
        hit = HitStat(rule_name="手机号", count=1, locations=("第42段",))
        p.add_hit("a.docx", hit)

        # 守护判据（= test_locations只进tooltip 的核心断言）
        guard = "第42段" not in p.cell_text(0, 3)
        assert not guard, (
            "❌ 守护测试无鉴别力：变异版把 locations 放进 display 后，"
            "断言 ``assert '第42段' not in p.cell_text(0,3)`` 竟然仍成立"
        )
        # 确认变异版真写进去了（不是空跑导致断言侥幸成立）
        assert "第42段" in p.cell_text(0, 3)


# ============================================================ C. _open_path
class TestOpenPath:
    def test_存在文件_返回True并记日志(self, qapp, tmp_path, patch_openurl):
        from redactor.gui import MainWindow
        w = MainWindow()
        f = tmp_path / "a.docx"
        _make_docx(f)
        assert w._open_path(str(f)) is True
        assert patch_openurl == [str(f)]
        assert "已打开" in w.log_panel().toPlainText()
        w.close()

    def test_标记不存在_返回False且日志含路径不存在(self, qapp, tmp_path,
                                                  patch_openurl):
        from redactor.gui import MainWindow
        w = MainWindow()
        ghost = tmp_path / "ghost.docx"
        assert w._open_path(str(ghost)) is False
        # 关键：openUrl 根本没被调用
        assert patch_openurl == []
        log = w.log_panel().toPlainText()
        assert "路径不存在" in log
        assert "ghost.docx" in log
        w.close()

    def test_目录_也能打开(self, qapp, tmp_path, patch_openurl):
        from redactor.gui import MainWindow
        w = MainWindow()
        d = tmp_path / "outdir"
        d.mkdir()
        assert w._open_path(str(d)) is True
        assert patch_openurl == [str(d)]
        w.close()

    def test_标记无默认程序_返回False且日志含未找到(self, qapp, tmp_path,
                                                patch_openurl_fail):
        from redactor.gui import MainWindow
        w = MainWindow()
        f = tmp_path / "a.docx"
        _make_docx(f)
        assert w._open_path(str(f)) is False
        assert patch_openurl_fail == [str(f)]   # 确实调了，只是返回 False
        log = w.log_panel().toPlainText()
        assert "未找到" in log
        w.close()

    def test_空串_返回False(self, qapp, patch_openurl):
        from redactor.gui import MainWindow
        w = MainWindow()
        assert w._open_path("") is False
        assert patch_openurl == []
        w.close()


# ============================================================ D. 双击打开
class TestDoubleClickOpen:
    def test_标记裁定2_已产出开输出(self, qapp, tmp_path, patch_openurl):
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "a.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])

        # 回填 output_path（模拟 _on_file_done 做过）。
        # ★ 输出文件必须**真实存在** —— _open_path 会对不存在的路径
        #   写"路径不存在"日志并返回 False，那样测的就不是"开输出"了。
        out_dir = tmp_path / "out"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / "a_redacted.docx"
        _make_docx(out)
        assert w.drop_zone().set_output_path(str(src), str(out)) is True

        w.drop_zone().activate_row(0)
        assert len(patch_openurl) == 1, f"应调用一次 openUrl，实际 {patch_openurl}"
        assert _same_path(patch_openurl[0], out), \
            f"应打开输出文件，实际打开了 {patch_openurl[0]}"
        w.close()

    def test_标记裁定2_未产出开源源(self, qapp, tmp_path, patch_openurl):
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "a.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])
        # 不回填 output_path（未处理 / 失败）
        assert w.drop_zone().output_path_for(str(src)) is None

        w.drop_zone().activate_row(0)
        assert len(patch_openurl) == 1, f"应调用一次 openUrl，实际 {patch_openurl}"
        assert _same_path(patch_openurl[0], src), \
            f"未产出应开源文件，实际打开了 {patch_openurl[0]}"
        w.close()

    def test_无UserRole不调用openUrl(self, qapp, tmp_path, patch_openurl):
        from redactor.gui import MainWindow
        w = MainWindow()
        # 空列表 + 越界行
        data = w.drop_zone().activate_row(0)
        assert data == {}
        assert patch_openurl == []
        w.close()

    def test_output_path_for_未回填返回None(self, qapp, tmp_path):
        z = DropZoneList()
        src = tmp_path / "a.docx"
        _make_docx(src)
        z.add_files([str(src)])
        assert z.output_path_for(str(src)) is None
        # 回填后可查到
        z.set_output_path(str(src), str(tmp_path / "a_redacted.docx"))
        assert z.output_path_for(str(src)).endswith("a_redacted.docx")

    def test_set_output_path_未找到行返回False(self, qapp, tmp_path):
        z = DropZoneList()
        ghost = tmp_path / "ghost.docx"
        assert z.set_output_path(str(ghost), "C:/out/x.docx") is False

    def test_输出列表双击打开(self, qapp, tmp_path, patch_openurl):
        from redactor.gui import MainWindow
        w = MainWindow()
        f = tmp_path / "a_redacted.docx"
        _make_docx(f)
        w.preview_panel().add_output_path(str(f))
        w.preview_panel().activate_output_row(0)
        assert patch_openurl == [str(f)]
        w.close()


# ============================================================ E. MainWindow 集成
class TestMainWindowPreview:
    def test_标记跑3文件_命中表填充_输出登记(self, qapp, tmp_path):
        from redactor.gui import MainWindow
        w = MainWindow()
        g1 = tmp_path / "in" / "g1.docx"
        g2 = tmp_path / "in" / "g2.docx"
        bad = tmp_path / "in" / "bad.docx"
        g1.parent.mkdir(exist_ok=True)
        _make_docx(g1)
        _make_docx(g2)
        _make_corrupted_docx(bad)

        w.drop_zone().add_files([str(g1), str(bad), str(g2)])
        out_root = tmp_path / "out"
        assert w.start_processing(output_root=out_root)
        assert _wait_until(lambda: w.state() == "done")

        pv = w.preview_panel()
        # 2 个成功文件各有 1 条手机号命中 → 2 行
        assert pv.hit_count() >= 2, f"命中表应至少有 2 行，实际 {pv.hit_count()}"
        # 输出列表登记 2 个（损坏文件不登记）
        assert pv.output_count() == 2, pv.output_paths()
        # 输出路径已回填
        assert w.drop_zone().output_path_for(str(g1)) is not None
        assert w.drop_zone().output_path_for(str(bad)) is None
        w.close()

    def test_标记Start前清空旧预览(self, qapp, tmp_path):
        from redactor.gui import MainWindow
        w = MainWindow()
        pv = w.preview_panel()
        # 先手工填一些
        pv.add_hit("old.docx", HitStat(rule_name="手机号", count=1))
        pv.add_output_path("C:/old/out.docx")
        pv.set_hit_limit(3)
        for i in range(10):
            pv.add_hit("old.docx", HitStat(rule_name="手机号", count=1))
        assert pv.hit_count() == 3
        assert pv.output_count() == 1

        src = tmp_path / "a.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])
        assert w.start_processing(output_root=tmp_path / "out")
        assert _wait_until(lambda: w.state() == "done")

        # 旧数据已清（只剩本次的 1 条命中）
        assert pv.hit_count() == 1, pv.hit_count()
        assert pv.hits_dropped() == 0
        assert pv.notice_visible() is False
        for p_ in pv.output_paths():
            assert "old" not in p_
        w.close()

    def test_标记清空列表_预览同步清空(self, qapp, tmp_path):
        from redactor.gui import MainWindow
        w = MainWindow()
        pv = w.preview_panel()
        pv.add_hit("a.docx", HitStat(rule_name="手机号", count=1))
        pv.add_output_path("C:/out/a.docx")
        src = tmp_path / "a.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])

        w.clear_files()
        assert pv.hit_count() == 0
        assert pv.output_count() == 0
        assert w.last_output_root() is None
        assert not w.open_out_dir_action().isEnabled()
        w.close()

    def test_标记裁定3_打开输出目录按钮_只记最近一次不弹窗(
            self, qapp, tmp_path, patch_openurl, monkeypatch
    ):
        """★ 关键：按钮点下去**不能弹 QFileDialog**，直接开 _last_output_root。"""
        from redactor.gui import MainWindow
        w = MainWindow()

        # 记录 QFileDialog 是否被调用
        dialog_calls: List[tuple] = []
        monkeypatch.setattr(
            QtWidgets.QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: (dialog_calls.append(a), "")[1]),
        )

        src = tmp_path / "a.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])
        out1 = tmp_path / "out1"
        assert w.start_processing(output_root=out1)
        assert _wait_until(lambda: w.state() == "done")
        assert w.last_output_root() == out1

        # 点按钮
        assert w.open_out_dir_action().isEnabled()
        w.open_out_dir_action().trigger()
        assert len(patch_openurl) == 1 and _same_path(patch_openurl[0], out1), \
            f"应直接打开 out1，实际 {patch_openurl}"
        assert dialog_calls == [], "★ 打开输出目录按钮不应弹 QFileDialog"

        # 跑第 2 批 → 按钮指向第 2 批
        src2 = tmp_path / "b.docx"
        _make_docx(src2)
        w.drop_zone().add_files([str(src2)])
        out2 = tmp_path / "out2"
        assert w.start_processing(output_root=out2)
        assert _wait_until(lambda: w.state() == "done")
        patch_openurl.clear()
        w.open_out_dir_action().trigger()
        assert len(patch_openurl) == 1 and _same_path(patch_openurl[0], out2), \
            f"应指向最近一次的 out2，实际 {patch_openurl}"
        assert dialog_calls == [], "第 2 次也不应弹窗"
        w.close()

    def test_标记未处理过_点按钮只写日志不弹窗(self, qapp, tmp_path,
                                            patch_openurl, monkeypatch):
        from redactor.gui import MainWindow
        w = MainWindow()
        dialog_calls: List[tuple] = []
        monkeypatch.setattr(
            QtWidgets.QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: (dialog_calls.append(a), "")[1]),
        )
        assert w.last_output_root() is None
        assert not w.open_out_dir_action().isEnabled()
        w._on_open_out_dir_clicked()
        assert patch_openurl == []
        assert dialog_calls == []
        assert "无输出目录" in w.log_panel().toPlainText()
        w.close()

    def test_finished_状态栏含汇总(self, qapp, tmp_path):
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "a.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])
        assert w.start_processing(output_root=tmp_path / "out")
        assert _wait_until(lambda: w.state() == "done")
        msg = w.statusBar().currentMessage()
        assert "命中" in msg, msg
        assert "输出" in msg, msg
        w.close()

    def test_dry_run_不登记输出路径(self, qapp, tmp_path):
        from redactor.gui import MainWindow
        w = MainWindow()
        w.dry_run_action().setChecked(True)
        src = tmp_path / "a.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])
        assert w.start_processing(output_root=tmp_path / "out")
        assert _wait_until(lambda: w.state() == "done")
        # dry-run 不写文件 → 不登记输出
        assert w.preview_panel().output_count() == 0
        assert w.drop_zone().output_path_for(str(src)) is None
        # 但命中表仍有内容
        assert w.preview_panel().hit_count() >= 1
        w.close()


# ============================================================ F. 反向验证
class TestReverseValidation:
    def test_标记打开输出目录改弹窗_守护测试真红(self, qapp, tmp_path,
                                                patch_openurl, monkeypatch):
        """变异：``_on_open_out_dir_clicked`` 改成每次弹 QFileDialog。

        ★ 裁定 3 的守护判据是"**不弹窗**"。本测试主动验证该判据有鉴别力。
        """
        from redactor.gui import MainWindow

        def popup_version(self) -> None:
            """变异版：每次都弹目录选择框。"""
            chosen = QtWidgets.QFileDialog.getExistingDirectory(
                self, "选择目录", str(Path.home()))
            if not chosen:
                return
            self._open_path(chosen)

        monkeypatch.setattr(MainWindow, "_on_open_out_dir_clicked", popup_version)

        w = MainWindow()
        dialog_calls: List[tuple] = []
        monkeypatch.setattr(
            QtWidgets.QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: (dialog_calls.append(a), str(tmp_path))[1]),
        )

        src = tmp_path / "a.docx"
        _make_docx(src)
        w.drop_zone().add_files([str(src)])
        out1 = tmp_path / "out1"
        assert w.start_processing(output_root=out1)
        assert _wait_until(lambda: w.state() == "done")

        w.open_out_dir_action().trigger()

        # ★ 守护判据（= 上面那个测试的核心断言）对弹窗版**必须不成立**
        guard = (dialog_calls == [])
        assert not guard, (
            "❌ 守护测试无鉴别力：弹窗变异版的 dialog_calls 竟然为空。"
            "``test_标记裁定3_打开输出目录按钮_只记最近一次不弹窗`` 的 "
            "``assert dialog_calls == []`` 对弹窗版**会红**"
        )
        # 确认变异版真弹了（不是空跑导致断言侥幸成立）
        assert len(dialog_calls) == 1, f"变异版应弹 1 次窗，实际 {len(dialog_calls)}"
        w.close()

    def test_标记add_result不追加_守护测试真红(self, qapp, monkeypatch):
        """变异：``add_result`` 改成什么都不做 → TestPreviewPanel 必须红。"""
        def noop_add_result(self, source, result) -> int:
            return 0

        monkeypatch.setattr(PreviewPanel, "add_result", noop_add_result)

        p = PreviewPanel()
        result = FileResult(
            source="C:/tmp/a.docx", status="ok",
            hits=[HitStat(rule_name="手机号", count=2),
                  HitStat(rule_name="身份证号", count=1)],
        )
        added = p.add_result("C:/tmp/a.docx", result)

        # 守护判据（= test_add_result_追加N行 的核心断言）
        guard = (added == 2 and p.hit_count() == 2)
        assert not guard, (
            "❌ 守护测试无鉴别力：noop 版竟然 added==2 且 hit_count==2"
        )
        assert added == 0
        assert p.hit_count() == 0