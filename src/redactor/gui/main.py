"""GUI 入口（v0.6.0 骨架）。

- ``redactor-gui`` 子命令调用 :func:`main`
- :func:`main` 创建 :class:`QApplication` + :class:`MainWindow` 并启动事件循环
- ``QT_QPA_PLATFORM=offscreen`` 时不创建真实窗口（CI 测试用）

为什么 main() 单独一个文件：
- :mod:`redactor.gui.__init__` 只导出符号，不能有副作用
- :func:`main` 是唯一允许 ``sys.exit`` 的入口
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Sequence

from PySide6 import QtWidgets

from redactor.gui import APP_NAME, APP_ORG, APP_VERSION
from redactor.gui.main_window import MainWindow


def _build_app(argv: Sequence[str]) -> QtWidgets.QApplication:
    """构造 :class:`QApplication`，每个进程只允许一个实例。"""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(list(argv))
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setApplicationVersion(APP_VERSION)
    return app


def main(argv: Optional[Sequence[str]] = None) -> int:
    """GUI 主入口。

    Args:
        argv: 命令行参数；None 时用 :data:`sys.argv`。

    Returns:
        进程退出码（由 :class:`QApplication.exec` 返回）。
    """
    if argv is None:
        argv = sys.argv

    app = _build_app(argv)

    # 允许 offscreen 模式（CI 烟雾测试）：不创建真实窗口
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        # 不 show，调用 processEvents 让 layout 生效即可
        win = MainWindow()
        win.show()
        app.processEvents()
        win.close()
        return 0

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))