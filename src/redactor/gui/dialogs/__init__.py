"""GUI 对话框子包（PySide6）。

每个对话框对应一个独立功能，工具栏按钮触发：

- :mod:`redactor.gui.dialogs.scope_dialog` — P7 范围脱敏设置（v0.7.0）

**分层原因**：对话框依赖 widgets 层，但不参与主窗口布局，
放独立子包避免 :mod:`redactor.gui.widgets` 无限膨胀。
"""
from __future__ import annotations

__all__ = ["scope_dialog"]