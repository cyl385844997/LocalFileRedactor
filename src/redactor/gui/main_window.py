"""主窗口（v0.6.0 骨架）。

布局:
    +---------------------------------------------------------+
    | 菜单栏 (File / Edit / Help)                              |
    +---------------------------------------------------------+
    | 工具栏 (Add / Remove / Clear / Dry-run / Start / Cancel)|
    +---------------------+-----------------------------------+
    |                     | 日志 (LogPanel)                    |
    |  拖拽区             +-----------------------------------+
    |  (DropZoneList)     | 预览 (PreviewPanel: 命中表/打开输出) |
    |                     |                                    |
    +---------------------+-----------------------------------+
    | 状态栏 [进度条 StatusProgressBar]                        |
    +---------------------------------------------------------+

v0.6.0 仅构造 + 不连信号；v0.6.1 起逐步接信号。
"""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from redactor.gui.widgets import (
    DropZoneList,
    LogPanel,
    PreviewPanel,
    StatusProgressBar,
)


class MainWindow(QtWidgets.QMainWindow):
    """主窗口。"""

    def __init__(
        self,
        *,
        app_name: str = "LocalFileRedactor",
        app_version: str = "0.6.0",
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._app_name = app_name
        self._app_version = app_version

        self.setWindowTitle(f"{app_name} {app_version}")
        self.setMinimumSize(960, 600)
        self.resize(1100, 700)

        # ---- 中央布局 ----
        self._drop_zone = DropZoneList(self)
        self._log = LogPanel(self)
        self._preview = PreviewPanel(self)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._wrap_with_header("待处理文件", self._drop_zone))
        right = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical, self)
        right.addWidget(self._wrap_with_header("日志", self._log))
        right.addWidget(self._wrap_with_header("预览", self._preview))
        right.setStretchFactor(0, 1)
        right.setStretchFactor(1, 2)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        # ---- 菜单 / 工具栏 / 状态栏 ----
        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()

    # ---------------------------------------------------------- 内部构造
    def _wrap_with_header(
        self, title: str, widget: QtWidgets.QWidget,
    ) -> QtWidgets.QWidget:
        """包一层带标题的容器，让 QSplitter 每个区域都有清晰标签。"""
        wrap = QtWidgets.QWidget(self)
        v = QtWidgets.QVBoxLayout(wrap)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)
        label = QtWidgets.QLabel(title, wrap)
        label.setStyleSheet("font-weight: 600; color: #444;")
        v.addWidget(label)
        v.addWidget(widget)
        return wrap

    def _build_menu(self) -> None:
        bar = self.menuBar()

        m_file = bar.addMenu("文件(&F)")
        act_add = m_file.addAction("添加文件…(&A)")
        act_add.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        m_file.addSeparator()
        act_quit = m_file.addAction("退出(&Q)")
        act_quit.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)

        m_edit = bar.addMenu("编辑(&E)")
        act_clear = m_edit.addAction("清空列表(&C)")
        m_edit.addSeparator()
        m_edit.addAction("首选项…（v0.4 预留）").setEnabled(False)

        m_help = bar.addMenu("帮助(&H)")
        act_about = m_help.addAction("关于(&A)")
        act_about.triggered.connect(self._show_about)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("主工具栏")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(16, 16))

        self._act_dry_run = QtGui.QAction("Dry-run", self)
        self._act_dry_run.setCheckable(True)
        self._act_dry_run.setChecked(False)
        self._act_dry_run.setToolTip("勾选后只扫描不写文件（v0.6.2 实装）")
        tb.addAction(self._act_dry_run)

        tb.addSeparator()

        # v0.6.0 占位 —— Start / Cancel 在 v0.6.2 接 QThread
        self._act_start = QtGui.QAction("开始", self)
        self._act_start.setEnabled(False)
        self._act_start.setToolTip("v0.6.2 实现")
        tb.addAction(self._act_start)

        self._act_cancel = QtGui.QAction("取消", self)
        self._act_cancel.setEnabled(False)
        self._act_cancel.setToolTip("v0.6.2 实现")
        tb.addAction(self._act_cancel)

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self._progress = StatusProgressBar(self)
        sb.addPermanentWidget(self._progress)
        sb.showMessage("就绪")

    # ---------------------------------------------------------- 槽
    def _show_about(self) -> None:
        QtWidgets.QMessageBox.about(
            self,
            f"关于 {self._app_name}",
            f"<b>{self._app_name}</b> {self._app_version}<br>"
            "本地离线文件脱敏工具（docx / xlsx / pdf / pptx）。<br>"
            "绝不上传、绝不覆盖原文件。<br><br>"
            "GUI 骨架版本（v0.6.0）。<br>"
            "功能在 v0.6.1~v0.6.3 渐进添加。",
        )

    # ---------------------------------------------------------- 公开 getter
    def drop_zone(self) -> DropZoneList:
        return self._drop_zone

    def log_panel(self) -> LogPanel:
        return self._log

    def preview_panel(self) -> PreviewPanel:
        return self._preview

    def progress_bar(self) -> StatusProgressBar:
        return self._progress

    def dry_run_action(self) -> QtGui.QAction:
        return self._act_dry_run


__all__ = ["MainWindow"]