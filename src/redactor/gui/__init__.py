"""GUI 层（PySide6）—— P4 阶段实现。

**唯一允许 import PySide6 的包**。
core / recognizers / redactors / handlers / report 均不得依赖 GUI，
以保证核心库可被 CLI、GUI、以及将来的其他前端复用。

模块清单（v0.6.0 骨架）:
- :mod:`redactor.gui.widgets` — 复用控件（拖拽区 / 日志区 / 预览区）
- :mod:`redactor.gui.main_window` — :class:`QMainWindow` 子类
- :mod:`redactor.gui.main` — :func:`main` 入口（QApplication 启动）
- :mod:`redactor.gui.__main__` — ``python -m redactor.gui`` 入口

裁定点（T1~T7，见 ``.verify/probe_gui/GUI_方案.md`` v1）:
- T1 PySide6、 T3 QThread+Signal/Slot、 T5 白名单、 T6 dry-run+打开输出
- 入口 ``redactor-gui``（pyproject.toml ``[project.scripts]``）

🚧 v0.6.0 = 空骨架：窗口可开可关、菜单/工具栏/状态栏齐全，
    文件列表、日志、预览、进度条均为空 placeholder。
    实际功能在 v0.6.1~v0.6.3 渐进添加。
"""
from __future__ import annotations

__all__ = [
    "APP_NAME",
    "APP_ORG",
    "APP_VERSION",
    "MainWindow",
    "main",
]

APP_NAME = "LocalFileRedactor"
APP_ORG = "LocalFileRedactor"
APP_VERSION = "0.6.0"

# PySide6 imports are deferred to submodules so this package import never crashes
# even if PySide6 is not installed. The submodule-level imports fail loudly only
# when the GUI is actually launched or GUI call fails.
from redactor.gui.main_window import MainWindow  # noqa: E402, F401
from redactor.gui.main import main  # noqa: E402, F401