"""主窗口（v0.6.3 预览 + 打开输出接通版）。

布局:
    +---------------------------------------------------------+
    | 菜单栏 (File / Edit / Help)                              |
    +---------------------------------------------------------+
    | 工具栏 (Clear / Dry-run / Start / Cancel / 打开输出目录) |
    +---------------------+-----------------------------------+
    |                     | 日志 (LogPanel)                    |
    |  拖拽区             +-----------------------------------+
    |  (DropZoneList)     | 预览 (PreviewPanel: 命中表/打开输出) |
    |                     |                                    |
    +---------------------+-----------------------------------+
    | 状态栏 [进度条 StatusProgressBar]                        |
    +---------------------------------------------------------+

v0.6.2 增量（v0.6.1 → v0.6.2）:
    - 工具栏 "开始" 接通 → :meth:`_on_start_clicked`
    - 工具栏 "取消" 接通 → Controller.cancel()
    - 工具栏 "Dry-run" checkbox 接通 → 传给 Worker
    - 状态栏进度条接 Controller.progress 信号 → setValue + setFormat
    - LogPanel 接 Controller.log_message → 实时追加
    - DropZoneList 文件项状态接 Controller.file_done → "待处理" → "成功/失败/跳过"
    - 按钮状态机 4 态：空载 / 就绪 / 运行中 / 完成（:meth:`_set_state`）
    - Start 时弹 QFileDialog.getExistingDirectory 让用户选输出根目录

v0.6.3 增量（v0.6.2 → v0.6.3）:
    - PreviewPanel 接通：4 列命中表（# / 文件 / 规则 / 位置）+ 500 行截断 + 顶部提示
    - :meth:`_open_path` 封装 QDesktopServices（补"路径不存在"日志）
    - :meth:`_desktop_open` 单独抽出作为唯一 QDesktopServices 出口（可测）
    - 拖拽区双击 → :meth:`_on_drop_zone_activated`（已产出开输出 / 未产出开源源）
    - PreviewPanel 输出列表双击 → :meth:`_on_output_activated`
    - 工具栏 "打开输出目录" → 只用 ``_last_output_root``，**不弹窗**
    - Controller.finished → 状态栏汇总"命中 N 条 / 输出 M 个 / 失败 K 个"
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from redactor.core.models import FileResult
from redactor.core.scopes import (
    DEFAULT_TIER,
    HIGH_SUFFIXES,
    LOW_SUFFIXES,
    ScopeEntry,
)
from redactor.gui.controller import RedactorController
from redactor.recognizers.name_recognizer import jieba_available
from redactor.gui.widgets import (
    DropZoneList,
    LogPanel,
    PreviewPanel,
    StatusProgressBar,
    normalize_path,
)


# 按钮状态机的 4 态
_STATE_EMPTY = "empty"          # 列表空
_STATE_READY = "ready"          # 有文件未跑
_STATE_RUNNING = "running"      # 运行中
_STATE_DONE = "done"            # 完成 / 取消


class MainWindow(QtWidgets.QMainWindow):
    """主窗口。"""

    def __init__(
        self,
        *,
        app_name: str = "LocalFileRedactor",
        app_version: str = "1.0.0",
        controller: Optional[RedactorController] = None,
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

        # ---- 控制器 ----
        # 默认 new 一个；测试可注入 mock controller
        self._controller = controller if controller is not None else RedactorController(parent=self)

        # v0.6.3：最近一次的输出根目录（"打开输出目录"按钮用它，不弹窗）
        self._last_output_root: Optional[Path] = None
        # v0.6.3：本次任务统计（finished 时汇总到状态栏）
        self._stats_ok = 0
        self._stats_failed = 0
        self._stats_hits = 0
        # P7：范围脱敏条目（由设置对话框写入；为空 = 未启用）
        self._scope_entries: List[ScopeEntry] = []

        # ---- 信号接通 ----
        self._drop_zone.files_added.connect(self._on_files_added)
        self._drop_zone.files_cleared.connect(self._on_files_cleared)
        self._drop_zone.item_activated.connect(self._on_drop_zone_activated)
        # v0.6.3：PreviewPanel 输出列表双击 → 打开
        self._preview.output_activated.connect(self._on_output_activated)
        # Controller 信号 → MainWindow 槽
        self._controller.started.connect(self._on_controller_started)
        self._controller.progress.connect(self._on_progress)
        self._controller.file_done.connect(self._on_file_done)
        self._controller.log_message.connect(self._on_log_message)
        self._controller.cancelled.connect(self._on_controller_cancelled)
        self._controller.finished.connect(self._on_controller_finished)

        # 初始状态：空载
        self._set_state(_STATE_EMPTY)

    # ---------------------------------------------------------- 内部构造
    def _wrap_with_header(
        self, title: str, widget: QtWidgets.QWidget,
    ) -> QtWidgets.QWidget:
        """包一层带标题的容器，让 QSplitter 每个区域都有清晰标签。"""
        wrap = QtWidgets.QWidget(self)
        v = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom, wrap)
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

        # v0.6.1：清空按钮
        self._act_clear_tb = QtGui.QAction("清空", self)
        self._act_clear_tb.setToolTip("清空待处理文件列表（v0.6.1）")
        self._act_clear_tb.triggered.connect(self.clear_files)
        tb.addAction(self._act_clear_tb)

        tb.addSeparator()

        # v0.6.2：Dry-run checkbox
        self._act_dry_run = QtGui.QAction("Dry-run", self)
        self._act_dry_run.setCheckable(True)
        self._act_dry_run.setChecked(False)
        self._act_dry_run.setToolTip("勾选后只扫描不写文件（v0.6.2 接通）")
        tb.addAction(self._act_dry_run)

        tb.addSeparator()

        # v0.6.2：Start 接通
        self._act_start = QtGui.QAction("开始", self)
        self._act_start.setShortcut(QtGui.QKeySequence("Ctrl+Return"))
        self._act_start.setToolTip("开始处理（Ctrl+Enter）")
        self._act_start.triggered.connect(self._on_start_clicked)
        tb.addAction(self._act_start)

        # v0.6.2：Cancel 接通
        self._act_cancel = QtGui.QAction("取消", self)
        self._act_cancel.setToolTip("取消当前任务（不打断正在处理的文件）")
        self._act_cancel.triggered.connect(self._on_cancel_clicked)
        tb.addAction(self._act_cancel)

        # v0.6.3：打开输出目录（只记最近一次的 output_root，不弹窗）
        self._act_open_out_dir = QtGui.QAction("打开输出目录", self)
        self._act_open_out_dir.setToolTip(
            "打开最近一次处理的输出目录（v0.6.3）"
        )
        self._act_open_out_dir.setEnabled(False)
        self._act_open_out_dir.triggered.connect(self._on_open_out_dir_clicked)
        tb.addAction(self._act_open_out_dir)

        tb.addSeparator()

        # P7：范围脱敏设置（独立对话框，裁定 3）
        self._act_scopes = QtGui.QAction("范围脱敏", self)
        self._act_scopes.setToolTip(
            "范围脱敏设置：输入后缀自动匹配带该后缀的整个实体（v0.7.0）"
        )
        self._act_scopes.setCheckable(True)
        self._act_scopes.setChecked(False)
        self._act_scopes.triggered.connect(self._on_scopes_clicked)
        tb.addAction(self._act_scopes)

        tb.addSeparator()

        # P8：人名识别开关（**默认关闭**）
        self._act_names = QtGui.QAction("人名识别", self)
        self._act_names.setToolTip(
            "用 jieba 词性识别人名并脱敏（v0.8.0）。"
            "误报率高于格式规则，故默认关闭"
        )
        self._act_names.setCheckable(True)
        self._act_names.setChecked(False)
        self._act_names.toggled.connect(self._on_names_toggled)
        tb.addAction(self._act_names)

        # 大字金额剔除（只有开了人名识别才有意义）
        self._act_strip_da_zi = QtGui.QAction("剔除大字金额", self)
        self._act_strip_da_zi.setToolTip(
            "剔除被误判成人名的大字金额片段（伍拾/壹佰…）。默认开启"
        )
        self._act_strip_da_zi.setCheckable(True)
        self._act_strip_da_zi.setChecked(True)
        self._act_strip_da_zi.setEnabled(False)
        self._act_strip_da_zi.toggled.connect(self._on_strip_da_zi_toggled)
        tb.addAction(self._act_strip_da_zi)

    def _on_names_toggled(self, checked: bool) -> None:
        """P8 开关：人名识别。"""
        self._act_strip_da_zi.setEnabled(checked)
        self._controller.set_names(
            enabled=checked,
            strip_da_zi=self._act_strip_da_zi.isChecked(),
        )
        msg = "人名识别已启用" if checked else "人名识别已关闭"
        if checked and not jieba_available():
            msg += "（★ jieba 未安装，本次不会生效）"
        self._log.append_line(msg)

    def _on_strip_da_zi_toggled(self, checked: bool) -> None:
        """P8 开关：剔除大字金额。"""
        self._controller.set_names(
            enabled=self._act_names.isChecked(),
            strip_da_zi=checked,
        )

    # ---------------------------------------------------------- P7 范围脱敏
    def _on_scopes_clicked(self) -> None:
        """打开范围脱敏设置对话框（裁定 3：独立对话框，不与词库混）。"""
        from redactor.gui.dialogs.scope_dialog import ScopeDialog

        dlg = ScopeDialog(self)
        if self._scope_entries:
            dlg.set_suffixes(
                sorted({s for e in self._scope_entries for s in e.suffixes})
            )
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        entries = dlg.entries()
        if not entries or not entries[0].suffixes:
            # 用户没填后缀 = 关闭范围脱敏
            self._scope_entries = []
            self._act_scopes.setChecked(False)
            self._log.append_line("范围脱敏：未配置任何后缀，已关闭")
            self._apply_scope_to_controller()
            return

        self._scope_entries = entries
        self._act_scopes.setChecked(True)
        self._apply_scope_to_controller()
        self._log.append_line(
            f"范围脱敏已启用：{len(entries[0].suffixes)} 个后缀"
            f"（严格度={dlg.current_tier()}"
            f"{'，首字黑名单开' if dlg.strict_lead_enabled() else ''}）"
        )

    def _apply_scope_to_controller(self) -> None:
        """把当前范围配置写进 Controller（下一次 start 时生效）。"""
        tier = None
        strict = False
        for e in self._scope_entries:
            tier = e.tier
            strict = e.strict_lead
        self._controller.set_scope(
            self._scope_entries, tier=tier, strict_lead=strict
        )

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self._progress = StatusProgressBar(self)
        sb.addPermanentWidget(self._progress)
        sb.showMessage("就绪")

    # ---------------------------------------------------------- 打开路径
    def _desktop_open(self, url: QtCore.QUrl) -> bool:
        """调用系统默认程序打开 url —— **唯一的** :class:`QDesktopServices` 出口。

        单独抽出来是为了可测：``QDesktopServices.openUrl`` 是 PySide6 静态方法，
        monkeypatch 它在部分版本上不生效。测试改 patch 本方法。
        """
        return bool(QtGui.QDesktopServices.openUrl(url))

    def _open_path(self, path: str) -> bool:
        """用系统默认程序打开 path（文件**或目录**）。

        为什么自己封装而不是直接调 ``QDesktopServices.openUrl``：
        它对不存在的路径**静默**返回 False，用户只看到"点了没反应"。
        封装后写日志，用户知道为什么没打开。

        Returns:
            True  成功发起打开
            False 路径不存在 / 系统无对应默认程序（日志已记录）
        """
        if not path:
            self._log.append_line("无法打开：路径为空")
            return False

        p = Path(path)
        # 目录也要能打开（用户双击输出目录行时）
        if not p.exists():
            self._log.append_line(f"无法打开（路径不存在）：{path}")
            return False

        url = QtCore.QUrl.fromLocalFile(str(p))
        if self._desktop_open(url):
            self._log.append_line(f"已打开：{path}")
            return True

        kind = "目录" if p.is_dir() else (p.suffix or "文件")
        self._log.append_line(f"系统未找到可打开{kind}的默认程序：{path}")
        return False

    def _on_open_out_dir_clicked(self) -> None:
        """工具栏"打开输出目录"：直接打开**最近一次**的 output_root。

        ★ 裁定 3：只记最近一次，**不弹目录选择框** ——
        跑完批处理用户想看的就是这批的输出目录，每次弹窗太烦。
        """
        if self._last_output_root is None:
            self._log.append_line("尚未处理过文件，无输出目录可打开")
            return
        self._open_path(str(self._last_output_root))

    def _on_drop_zone_activated(self, index: QtCore.QModelIndex) -> None:
        """拖拽区双击：★ 裁定 2 —— 已产出开输出，未产出开源文件。

        用户改完文件想立刻对比效果，所以优先开输出；
        失败 / 未跑时源文件才是他想看的。
        """
        data = self._drop_zone.item_data(index)
        if not data:
            return
        out = data.get("output_path")
        src = data.get("path")
        if out:
            self._open_path(str(out))
        elif src:
            self._open_path(str(src))

    def _on_output_activated(self, path: str) -> None:
        """PreviewPanel 输出列表双击：打开那个路径。"""
        self._open_path(path)

    # ---------------------------------------------------------- 状态机
    def _set_state(self, state: str) -> None:
        """按钮状态机 4 态切换（§9 裁定）。

        详见 v0.6.2_方案.md 第 9 节。
        """
        self._state = state
        if state == _STATE_EMPTY:
            self._act_start.setEnabled(False)
            self._act_cancel.setEnabled(False)
            self._act_dry_run.setEnabled(True)
            self._act_clear_tb.setEnabled(False)
        elif state == _STATE_READY:
            self._act_start.setEnabled(True)
            self._act_cancel.setEnabled(False)
            self._act_dry_run.setEnabled(True)
            self._act_clear_tb.setEnabled(True)
        elif state == _STATE_RUNNING:
            self._act_start.setEnabled(False)
            self._act_cancel.setEnabled(True)
            self._act_dry_run.setEnabled(False)  # 运行中不允许切换
            self._act_clear_tb.setEnabled(False)
        elif state == _STATE_DONE:
            self._act_start.setEnabled(True)
            self._act_cancel.setEnabled(False)
            self._act_dry_run.setEnabled(True)
            self._act_clear_tb.setEnabled(True)

    # ---------------------------------------------------------- 槽
    def _show_about(self) -> None:
        QtWidgets.QMessageBox.about(
            self,
            f"关于 {self._app_name}",
            f"<b>{self._app_name}</b> {self._app_version}<br>"
            "本地离线文件脱敏工具（docx / xlsx / pdf / txt）。<br>"
            "绝不上传、绝不覆盖原文件。<br><br>"
            "GUI v0.6.3：拖拽 + 批量 + 处理进度 + 命中预览。",
        )

    def _on_files_added(self, result: dict) -> None:
        """DropZoneList.files_added 槽：把 add_files 结果转成可读日志。"""
        added = result.get("added", 0)
        rejected = result.get("rejected", 0)
        duplicated = result.get("duplicated", 0)
        dirs = result.get("dirs", 0)
        reasons: list[dict] = result.get("rejected_reasons", [])  # type: ignore[assignment]

        # 拒绝 / 重复逐条写日志
        for entry in reasons:
            file_ = entry.get("file", "")
            reason = entry.get("reason", "")
            self._log.append_line(f"已拒绝: {file_} ({reason})")
        if dirs > 0:
            self._log.append_line(f"已跳过 {dirs} 个文件夹（首版不递归）")

        # 状态栏汇总
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
            self.statusBar().showMessage(f"未添加任何文件（跳过 {dirs} 个文件夹）")

        # 状态机：列表非空 → 就绪
        if self._drop_zone.count() > 0:
            if self._state != _STATE_RUNNING:
                self._set_state(_STATE_READY)
        else:
            self._set_state(_STATE_EMPTY)

    def _on_files_cleared(self) -> None:
        """DropZoneList.files_cleared 槽：写日志 + 状态栏归零 + 清预览。"""
        self._log.append_line("已清空列表")
        self.statusBar().showMessage("就绪")
        self._set_state(_STATE_EMPTY)
        # v0.6.3：清预览（命中表 + 输出列表 + 输出目录按钮）
        self._preview.clear_hits()
        self._preview.clear_outputs()
        self._last_output_root = None
        self._act_open_out_dir.setEnabled(False)
        self._progress.setValue(0)
        self._progress.setFormat("")

    # --------- v0.6.2 增量：Start / Cancel --------
    def _on_start_clicked(self) -> None:
        """用户点 Start：弹 QFileDialog 选输出目录 → Controller.start。"""
        if self._controller.is_running():
            return
        paths = self._drop_zone.file_paths()
        if not paths:
            return

        # §8.1 裁定：输出目录每次必弹（首版不记忆）
        out_dir_str = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "选择输出根目录（脱敏后的文件将写入此目录下）",
            str(Path.home()),
        )
        if not out_dir_str:
            self._log.append_line("已取消选择输出目录（未开始）")
            return
        output_root = Path(out_dir_str)

        dry_run = self._act_dry_run.isChecked()
        mode_str = "Dry-run（不写文件）" if dry_run else "Real-run"
        self._log.append_line(f"开始 {mode_str}：{len(paths)} 个文件 → {output_root}")

        # v0.6.3：记下本次 output_root（"打开输出目录"按钮用它）+ 清旧预览
        self._prepare_run(output_root)

        try:
            self._controller.start(
                paths=paths,
                output_root=output_root,
                dry_run=dry_run,
                overwrite_output=True,
            )
        except RuntimeError as exc:
            self._log.append_line(f"启动失败：{exc}")

    def _on_cancel_clicked(self) -> None:
        """用户点 Cancel：调 Controller.cancel()。"""
        if not self._controller.is_running():
            return
        self._log.append_line("请求取消（当前文件跑完后停止）…")
        self._controller.cancel()

    # --------- v0.6.2 增量：Controller 信号槽 --------
    def _on_controller_started(self, total: int) -> None:
        self._set_state(_STATE_RUNNING)
        self._progress.setRange(0, max(total, 1))
        self._progress.setValue(0)
        self._progress.setFormat(f"0/{total} - 准备中…")
        self.statusBar().showMessage(f"运行中（0 / {total}）")

    def _on_progress(self, idx: int, total: int, name: str) -> None:
        # idx 是 0-based；显示成 1-based 更直观
        self._progress.setValue(idx)
        self._progress.setFormat(f"{idx}/{total} - 当前：{name}")
        self.statusBar().showMessage(f"运行中（{idx} / {total} - {name}）")

    def _on_file_done(self, result: FileResult) -> None:
        """每文件跑完：更新列表项状态 + 填命中表 + 登记输出路径。

        关键：**用户要求每个文件跑完立刻 emit**，避免程序崩溃丢结果。
        v0.6.3 起同时逐行填命中表（表格看着"长出来"）。
        """
        self._update_file_item_status(result.source, result.status)
        self._preview.add_result(result.source, result)

        # 统计（finished 时汇总到状态栏）
        if result.status in ("ok", "redacted"):
            self._stats_ok += 1
            self._stats_hits += getattr(result, "total_hits", 0)
        elif result.status == "failed":
            self._stats_failed += 1

        # 成功且非 dry-run：算输出路径 + 回填列表项 + 登记到"打开输出"tab
        if result.status in ("ok", "redacted"):
            out = self._compute_output_path(result.source)
            if out is not None and not self._is_dry_run():
                self._drop_zone.set_output_path(result.source, str(out))
                self._preview.add_output_path(str(out))

    def _on_log_message(self, line: str) -> None:
        """Worker 日志实时写到 LogPanel（线程安全靠 Qt QueuedConnection）。"""
        self._log.append_line(line)

    def _on_controller_cancelled(self) -> None:
        self._progress.setFormat(f"已取消（{self._progress.value()}/{self._progress.maximum()}）")
        self.statusBar().showMessage("已取消")

    def _on_controller_finished(self) -> None:
        # 自然完成（cancelled 信号先发后 finished 也发）
        if self._state == _STATE_RUNNING:
            self._progress.setValue(self._progress.maximum())
            self._progress.setFormat("完成")
            self._set_state(_STATE_DONE)

            # v0.6.3：状态栏汇总（命中 / 输出 / 失败）
            shown = self._preview.hit_count()
            hit_txt = f"命中 {self._stats_hits} 条"
            if self._preview.hits_dropped() > 0:
                hit_txt += f"（表格仅展示前 {shown} 条）"
            out_txt = f"输出 {self._preview.output_count()} 个"
            fail_txt = f"失败 {self._stats_failed} 个" if self._stats_failed else "无失败"
            self.statusBar().showMessage(f"完成：{hit_txt}，{out_txt}，{fail_txt}")
            if self._preview.hits_dropped() > 0:
                self._log.append_line(
                    f"命中表已截断：共 {self._stats_hits} 条，"
                    f"仅展示前 {shown} 条（省略 {self._preview.hits_dropped()} 条）"
                )

    def _compute_output_path(self, source: str) -> Optional[Path]:
        """算某源文件的输出路径（与 Worker 的 ``_relative_output_path`` 同源）。

        不重复实现路径规则 —— 直接复用 :mod:`redactor.gui.worker` 里的函数，
        保证 GUI 显示的路径与实际落盘路径**永远一致**。
        """
        if self._last_output_root is None:
            return None
        from redactor.gui.worker import _relative_output_path
        src = Path(source)
        return _relative_output_path(src, src.parent, self._last_output_root)

    def _is_dry_run(self) -> bool:
        """当前 Dry-run checkbox 是否勾选（用于决定要不要登记输出路径）。"""
        return self._act_dry_run.isChecked()

    # ---------------------------------------------------------- 辅助
    def _update_file_item_status(self, source_path: str, status: str) -> None:
        """把 DropZoneList 中对应路径的项状态改成成功/失败/跳过。"""
        from redactor.gui.widgets import normalize_path
        try:
            target = normalize_path(source_path)
        except OSError:
            target = source_path

        status_zh = {
            "ok": "成功",
            "redacted": "成功",
            "skipped": "跳过",
            "failed": "失败",
        }.get(status, status)

        model = self._drop_zone.model()
        for r in range(model.rowCount()):
            data = model.item(r).data(QtCore.Qt.ItemDataRole.UserRole)
            if data and data.get("normalized") == target:
                item = model.item(r)
                # 解析原显示文本 "name    SIZE    STATUS"
                try:
                    parts = item.text().split("    ")
                    if len(parts) >= 2:
                        size_str = parts[1]
                    else:
                        size_str = ""
                except (AttributeError, IndexError):
                    size_str = ""
                item.setText(f"{Path(source_path).name}    {size_str:>10}    {status_zh}")
                item.setData({**data, "status": status_zh}, QtCore.Qt.ItemDataRole.UserRole)
                break

    # ---------------------------------------------------------- 公开方法
    def clear_files(self) -> None:
        """清空拖拽区列表（Edit 菜单 / 工具栏按钮共用此入口）。"""
        self._drop_zone.clear()

    def start_processing(self, output_root: Optional[Path] = None) -> bool:
        """测试 / 自动化入口：用预设路径跳过 QFileDialog。

        Returns:
            True 启动成功；False 启动被拒（已有任务在跑 / 列表空）。
        """
        if self._controller.is_running():
            return False
        paths = self._drop_zone.file_paths()
        if not paths:
            return False
        if output_root is None:
            return False
        out_path = Path(output_root)
        self._prepare_run(out_path)
        try:
            self._controller.start(
                paths=paths,
                output_root=out_path,
                dry_run=self._act_dry_run.isChecked(),
                overwrite_output=True,
            )
            return True
        except RuntimeError:
            return False

    def _prepare_run(self, output_root: Path) -> None:
        """开跑前的统一前置动作（两条 start 路径共用，避免漂移）。

        记 output_root / 清旧预览 / 启用"打开输出目录" / 统计归零。
        """
        self._last_output_root = Path(output_root)
        self._preview.clear_hits()
        self._preview.clear_outputs()
        self._act_open_out_dir.setEnabled(True)
        self._stats_ok = 0
        self._stats_failed = 0
        self._stats_hits = 0

    # ---------------------------------------------------------- 公开 getter
    def drop_zone(self) -> DropZoneList:
        return self._drop_zone

    def log_panel(self) -> LogPanel:
        return self._log

    def preview_panel(self) -> PreviewPanel:
        return self._preview

    def progress_bar(self) -> StatusProgressBar:
        return self._progress

    def controller(self) -> RedactorController:
        return self._controller

    def dry_run_action(self) -> QtGui.QAction:
        return self._act_dry_run

    def clear_action(self) -> QtGui.QAction:
        """公开：Edit 菜单"清空列表"动作（供测试 / 外部 trigger）。"""
        return self._act_clear

    def start_action(self) -> QtGui.QAction:
        """公开：工具栏"开始"按钮。"""
        return self._act_start

    def cancel_action(self) -> QtGui.QAction:
        """公开：工具栏"取消"按钮。"""
        return self._act_cancel

    def open_out_dir_action(self) -> QtGui.QAction:
        """公开：工具栏"打开输出目录"按钮（v0.6.3）。"""
        return self._act_open_out_dir

    def last_output_root(self) -> Optional[Path]:
        """公开：最近一次的输出根目录；未处理过则 None。"""
        return self._last_output_root

    def scope_entries(self) -> List[ScopeEntry]:
        """公开：当前范围脱敏条目（P7；空列表 = 未启用）。"""
        return list(self._scope_entries)

    def scopes_action(self) -> QtGui.QAction:
        """公开：工具栏"范围脱敏"按钮（P7）。"""
        return self._act_scopes

    def names_action(self) -> QtGui.QAction:
        """公开：工具栏"人名识别"开关（P8）。"""
        return self._act_names

    def strip_da_zi_action(self) -> QtGui.QAction:
        """公开：工具栏"剔除大字金额"开关（P8）。"""
        return self._act_strip_da_zi

    def apply_scope_entries(
        self, entries: Sequence[ScopeEntry], *, enable: bool = True
    ) -> None:
        """公开（测试 / 自动化）：直接设置范围脱敏条目，跳过对话框。

        与真实用户操作走同一条应用路径（:meth:`_apply_scope_to_controller`）。
        """
        self._scope_entries = list(entries) if enable else []
        self._act_scopes.setChecked(bool(self._scope_entries))
        self._apply_scope_to_controller()

    def state(self) -> str:
        """公开：当前按钮状态机（供测试断言）。"""
        return self._state


__all__ = ["MainWindow"]
