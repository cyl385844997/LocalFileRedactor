"""主窗口（v0.6.1 拖拽 + 批量接通版）。

布局:
    +---------------------------------------------------------+
    | 菜单栏 (File / Edit / Help)                              |
    +---------------------------------------------------------+
    | 工具栏 (Add / Clear / Dry-run / Start / Cancel)         |
    +---------------------+-----------------------------------+
    |                     | 日志 (LogPanel)                    |
    |  拖拽区             +-----------------------------------+
    |  (DropZoneList)     | 预览 (PreviewPanel: 命中表/打开输出) |
    |                     |                                    |
    +---------------------+-----------------------------------+
    | 状态栏 [进度条 StatusProgressBar]                        |
    +---------------------------------------------------------+

v0.6.1 增量（v0.6.0 → v0.6.1）:
    - Edit 菜单 "清空列表" 接通
    - 工具栏新增 "清空" 按钮
    - DropZoneList.files_added → 写日志 + 状态栏提示
    - DropZoneList.files_cleared → 写日志 + 状态栏归零
    - 公开 :meth:`clear_files` 供上述触发路径复用
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
        app_version: str = "0.6.1",
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

        # ---- 信号接通（v0.6.1 增量）----
        self._drop_zone.files_added.connect(self._on_files_added)
        self._drop_zone.files_cleared.connect(self._on_files_cleared)

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
        self._act_clear = m_edit.addAction("清空列表(&C)")
        # v0.6.1 接通
        self._act_clear.triggered.connect(self.clear_files)
        m_edit.addSeparator()
        m_edit.addAction("首选项…（v0.4 预留）").setEnabled(False)

        m_help = bar.addMenu("帮助(&H)")
        act_about = m_help.addAction("关于(&A)")
        act_about.triggered.connect(self._show_about)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("主工具栏")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(16, 16))

        # v0.6.1：新增 "清空" 按钮（与 Edit 菜单共用触发路径）
        self._act_clear_tb = QtGui.QAction("清空", self)
        self._act_clear_tb.setToolTip("清空待处理文件列表（v0.6.1）")
        self._act_clear_tb.triggered.connect(self.clear_files)
        tb.addAction(self._act_clear_tb)

        tb.addSeparator()

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
            "本地离线文件脱敏工具（docx / xlsx / pdf / txt）。<br>"
            "绝不上传、绝不覆盖原文件。<br><br>"
            "GUI v0.6.1：拖拽 + 批量（处理/预览留 v0.6.2/v0.6.3）。",
        )

    def _on_files_added(self, result: dict) -> None:
        """DropZoneList.files_added 槽：把 add_files 结果转成可读日志。"""
        added = result.get("added", 0)
        rejected = result.get("rejected", 0)
        duplicated = result.get("duplicated", 0)
        dirs = result.get("dirs", 0)
        reasons: list[dict] = result.get("rejected_reasons", [])  # type: ignore[assignment]

        # 拒绝 / 重复逐条写日志（dirs 单行汇总）
        for entry in reasons:
            file_ = entry.get("file", "")
            reason = entry.get("reason", "")
            self._log.append_line(f"已拒绝: {file_} ({reason})")
        if dirs > 0:
            self._log.append_line(f"已跳过 {dirs} 个文件夹（首版不递归）")

        # 状态栏：汇总一行
        parts = []
        if added > 0:
            parts.append(f"已添加 {added} 个")
        if duplicated > 0:
            parts.append(f"跳过重复 {duplicated} 个")
        if rejected > 0:
            parts.append(f"拒绝 {rejected} 个")
        if parts:
            self.statusBar().showMessage("，".join(parts) + f"（列表共 {self._drop_zone.count()} 个）")
        elif dirs > 0:
            # 只有目录被跳过：状态栏仍要给个反馈
            self.statusBar().showMessage(f"未添加任何文件（跳过 {dirs} 个文件夹）")

    def _on_files_cleared(self) -> None:
        """DropZoneList.files_cleared 槽：写日志 + 状态栏归零。"""
        self._log.append_line("已清空列表")
        self.statusBar().showMessage("就绪")

    # ---------------------------------------------------------- 公开方法
    def clear_files(self) -> None:
        """清空拖拽区列表（Edit 菜单 / 工具栏按钮共用此入口）。"""
        # 直接调 DropZoneList.clear() —— 它会发 files_cleared 信号，
        # 由 _on_files_cleared 写日志 / 状态栏，避免双写。
        self._drop_zone.clear()

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

    def clear_action(self) -> QtGui.QAction:
        """公开：Edit 菜单"清空列表"动作（供测试 / 外部触发用）。"""
        return self._act_clear


__all__ = ["MainWindow"]
