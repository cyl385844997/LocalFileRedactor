"""GUI 复用控件（v0.6.0 骨架版）。

每个控件都做**单一职责**的小型封装，方便 v0.6.1~v0.6.3 渐进替换实现。

本版（v0.6.0）只做骨架:
- :class:`DropZoneList` — :class:`QListView` 子类，仅**占位**+ 接受白名单扩展点
- :class:`LogPanel` — :class:`QPlainTextEdit`，仅**只读**日志（v0.6.2 替换为滚动）
- :class:`PreviewPanel` — :class:`QTabWidget`，含两个 tab："命中表"+"打开输出"占位
- :class:`StatusProgressBar` — :class:`QProgressBar`，固定高度用于状态栏

v0.6.0 所有控件**不连接信号** —— 仅构造即可。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

from PySide6 import QtCore, QtGui, QtWidgets


# ============================================================ 拖拽区
class DropZoneList(QtWidgets.QListView):
    """拖拽文件/文件夹占位列表。

    v0.6.0 仅构造 + 接受白名单扩展点。实际 dropEvent / dragEnterEvent
    在 v0.6.1 实现（T5 裁定）。
    """

    # 白名单：与 handlers/registry.py 同步（v0.6.1 改成从 registry 导入）
    DEFAULT_SUFFIXES: Sequence[str] = (
        ".docx", ".xlsx", ".pdf", ".pptx", ".txt",
    )

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)  # v0.6.1 在 dragEnterEvent 中校验白名单
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(True)
        self._model = QtGui.QStandardItemModel(self)
        self.setModel(self._model)
        self._placeholder = "拖入 .docx / .xlsx / .pdf / .pptx / .txt 文件或文件夹"

    # v0.6.0 骨架：模型空时显示 placeholder 文字
    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: D401
        super().paintEvent(event)
        if self._model.rowCount() != 0:
            return
        painter = QtGui.QPainter(self.viewport())
        painter.save()
        color = self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText)
        painter.setPen(color)
        painter.drawText(
            self.viewport().rect(),
            QtCore.Qt.AlignmentFlag.AlignCenter,
            self._placeholder,
        )
        painter.restore()

    # v0.6.0 占位：dragEnterEvent / dropEvent 留 hook，子类可覆盖
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:  # noqa: D401
        event.acceptProposedAction()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:  # noqa: D401
        event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:  # noqa: D401
        event.acceptProposedAction()

    def set_placeholder(self, text: str) -> None:
        """v0.6.1 调用，覆盖默认占位文字。"""
        self._placeholder = text
        self.viewport().update()

    # 公开 API：供 v0.6.1 worker 调用
    def add_paths(self, paths: Iterable[str]) -> int:
        added = 0
        for raw in paths:
            text = str(raw)
            if not text:
                continue
            item = QtGui.QStandardItem(text)
            item.setToolTip(text)
            self._model.appendRow(item)
            added += 1
        return added

    def all_paths(self) -> List[str]:
        return [
            self._model.item(r).text()
            for r in range(self._model.rowCount())
        ]

    def count(self) -> int:
        return self._model.rowCount()

    def clear(self) -> None:
        self._model.removeRows(0, self._model.rowCount())


# ============================================================ 日志区
class LogPanel(QtWidgets.QPlainTextEdit):
    """只读日志面板。

    v0.6.2 改为滚动追加 + 颜色（info/warn/error 三色）；
    v0.6.0 仅构造即可。
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)  # 防止用户长时间挂日志导致 OOM
        self.setPlaceholderText("处理日志将在此处显示")

    # v0.6.2 worker 调用
    def append_line(self, line: str) -> None:
        self.appendPlainText(line)


# ============================================================ 预览区
class PreviewPanel(QtWidgets.QTabWidget):
    """预览面板 —— 两个 tab 占位（命中表 + 打开输出）。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._hits_table = QtWidgets.QTableWidget(0, 3, self)
        self._hits_table.setHorizontalHeaderLabels(["文件", "规则", "位置"])
        self._hits_table.horizontalHeader().setStretchLastSection(True)
        self.addTab(self._hits_table, "命中表")

        self._open_output = QtWidgets.QListWidget(self)
        self._open_output.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self.addTab(self._open_output, "打开输出")

    # v0.6.3 调用
    def add_output_path(self, path: str) -> None:
        self._open_output.addItem(path)


# ============================================================ 状态栏进度
class StatusProgressBar(QtWidgets.QProgressBar):
    """固定高度进度条 —— 用于状态栏右侧。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaximumHeight(14)
        self.setMinimumWidth(180)
        self.setRange(0, 1)
        self.setValue(0)
        self.setTextVisible(True)


__all__ = [
    "DEFAULT_SUFFIXES",
    "DropZoneList",
    "LogPanel",
    "PreviewPanel",
    "StatusProgressBar",
]