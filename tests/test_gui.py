"""GUI 烟雾测试（v0.6.0 骨架）。

跑法：
    pytest -o addopts= -p no:cacheprovider --basetemp=.pt_probe tests/test_gui.py

约定：
- 所有测试在 :envvar:`QT_QPA_PLATFORM=offscreen` 下跑（无真实窗口）
- 每个测试自己构造 :class:`QApplication`（用 :func:`pytest-qt` 的 qtbot 或自己造）
- 关键断言：构造 / 关键控件存在 / 公开 API 工作 / 关闭不崩

v0.6.0 不引入 pytest-qt（额外依赖），用纯 PySide6 + offscreen。
"""

from __future__ import annotations

import os

import pytest

# PySide6 必须可用 —— 这些测试在无 PySide6 环境下应被自动跳过
pytest.importorskip("PySide6", reason="GUI 测试需要 PySide6")

# 必须在 import PySide6 之前设置 offscreen
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from redactor.gui import APP_NAME, APP_ORG, APP_VERSION, MainWindow  # noqa: E402
from redactor.gui.widgets import (  # noqa: E402
    DropZoneList,
    LogPanel,
    PreviewPanel,
    StatusProgressBar,
)


# ---------------------------------------------------------- fixture
@pytest.fixture
def qapp() -> QtWidgets.QApplication:
    """每个测试一个 QApplication（offscreen）。"""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def window(qapp: QtWidgets.QApplication) -> MainWindow:
    w = MainWindow()
    yield w
    w.close()
    qapp.processEvents()


# ---------------------------------------------------------- 模块级
class TestGuiModule:
    def test_包常量(self):
        assert APP_NAME == "LocalFileRedactor"
        assert APP_VERSION == "0.6.1"
        assert APP_ORG

    def test_MainWindow_可从包顶层导入(self):
        from redactor.gui import MainWindow as MW
        assert MW is MainWindow


# ---------------------------------------------------------- 控件
class TestDropZone:
    def test_白名单与handlers_registry同步(self, qapp):
        """白名单 = REGISTRY 当前 keys（v0.6.1 修正：去掉 .pptx over-promise）。"""
        from redactor.handlers import supported_extensions
        z = DropZoneList()
        assert set(z.suffixes()) == set(supported_extensions())

    def test_核心白名单包含4种且无pptx(self, qapp):
        z = DropZoneList()
        suffixes = set(z.suffixes())
        # 核心 4 种 + .pptx 明确不在（避免 GUI 接受 .pptx 但引擎不收的伪承诺）
        assert ".docx" in suffixes
        assert ".xlsx" in suffixes
        assert ".pdf" in suffixes
        assert ".txt" in suffixes
        assert ".pptx" not in suffixes

    def test_构造后count为零(self, qapp):
        z = DropZoneList()
        assert z.count() == 0
        assert z.all_paths() == []

    def test_add_paths_后count正确(self, qapp, tmp_path):
        z = DropZoneList()
        a = tmp_path / "a.docx"
        b = tmp_path / "b.xlsx"
        c = tmp_path / "c.pdf"
        for p in (a, b, c):
            p.write_bytes(b"x")
        n = z.add_paths([str(a), str(b), str(c)])
        assert n == 3
        assert z.count() == 3
        assert len(z.all_paths()) == 3

    def test_clear_后归零(self, qapp):
        z = DropZoneList()
        z.add_paths(["a.docx", "b.xlsx"])
        z.clear()
        assert z.count() == 0

    def test_接受拖拽(self, qapp):
        z = DropZoneList()
        assert z.acceptDrops() is True

    def test_占位文字可改(self, qapp):
        z = DropZoneList()
        z.set_placeholder("新提示")
        # 不报错即为通过（paintEvent 在 model 空时才显示）


class TestLogPanel:
    def test_只读(self, qapp):
        log = LogPanel()
        assert log.isReadOnly() is True

    def test_追加行不报错(self, qapp):
        log = LogPanel()
        log.append_line("hello")
        log.append_line("world")
        # 不崩即为通过


class TestPreviewPanel:
    def test_两个tab(self, qapp):
        p = PreviewPanel()
        assert p.count() == 2
        labels = [p.tabText(i) for i in range(p.count())]
        assert "命中表" in labels
        assert "打开输出" in labels

    def test_add_output_path_不报错(self, qapp):
        p = PreviewPanel()
        p.add_output_path("C:/tmp/out.docx")
        p.add_output_path("C:/tmp/out2.xlsx")
        assert p.count() == 2  # 两个 tab 不变


class TestStatusProgressBar:
    def test_初始值与范围(self, qapp):
        pb = StatusProgressBar()
        assert pb.maximum() == 1
        assert pb.value() == 0


# ---------------------------------------------------------- 主窗口
class TestMainWindow:
    def test_标题(self, window):
        assert APP_NAME in window.windowTitle()
        assert APP_VERSION in window.windowTitle()

    def test_3个菜单_File_Edit_Help(self, window):
        bar = window.menuBar()
        titles = [a.text() for a in bar.actions()]
        assert any("文件" in t for t in titles), f"菜单: {titles}"
        assert any("编辑" in t for t in titles), f"菜单: {titles}"
        assert any("帮助" in t for t in titles), f"菜单: {titles}"

    def test_工具栏存在且含dry_run(self, window):
        tb = window.findChildren(QtWidgets.QToolBar)
        assert tb, "主工具栏缺失"
        actions_text = [a.text() for a in tb[0].actions()]
        assert "Dry-run" in actions_text, f"工具栏动作: {actions_text}"

    def test_关键控件_getter存在(self, window):
        assert isinstance(window.drop_zone(), DropZoneList)
        assert isinstance(window.log_panel(), LogPanel)
        assert isinstance(window.preview_panel(), PreviewPanel)
        assert isinstance(window.progress_bar(), StatusProgressBar)
        assert isinstance(window.dry_run_action(), QtGui.QAction)

    def test_dry_run_action_默认未勾选(self, window):
        assert window.dry_run_action().isChecked() is False

    def test_dry_run_action_可勾选(self, window):
        a = window.dry_run_action()
        a.setChecked(True)
        assert a.isChecked() is True
        a.setChecked(False)

    def test_中央widget是QSplitter(self, window):
        assert isinstance(window.centralWidget(), QtWidgets.QSplitter)

    def test_状态栏含进度条(self, window):
        sb = window.statusBar()
        bars = sb.findChildren(StatusProgressBar)
        assert bars, "状态栏缺进度条"

    def test_关闭不崩(self, qapp):
        w = MainWindow()
        w.show()
        qapp.processEvents()
        w.close()
        qapp.processEvents()
        # 不崩即为通过

    def test_drop_zone_通过公开API可填充(self, window, tmp_path):
        a = tmp_path / "x.docx"
        b = tmp_path / "y.xlsx"
        for p in (a, b):
            p.write_bytes(b"x")
        n = window.drop_zone().add_paths([str(a), str(b)])
        assert n == 2
        assert window.drop_zone().count() == 2