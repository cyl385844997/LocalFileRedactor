"""v0.6.1 端到端验证脚本（offline / offscreen）。

跑法（与本机其它 GUI 验证一致）：
    set PYTHONPATH=src;.verify_libs
    set QT_QPA_PLATFORM=offscreen
    "E:/Program Files/python/python.exe" scripts/e2e_v061.py

场景：拖拽 1 合法 + 1 非法 + 1 重复，验证 counts / 列表 / 日志 / 状态栏。

本脚本是 v0.6.1 交付的 e2e 验证（deliverable #2），不入产品包（src/redactor/gui/），
仅作开发期 / 验收期验证用。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from redactor.gui import APP_NAME, APP_VERSION, MainWindow  # noqa: E402


def main() -> int:
    print(f"=== {APP_NAME} v{APP_VERSION} 端到端验证（v0.6.1） ===")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    # ---- 准备临时文件 ----
    tmpdir = tempfile.mkdtemp(prefix="redactor_e2e_v061_")
    print(f"tmpdir: {tmpdir}")
    legal = Path(tmpdir) / "doc.docx"
    illegal = Path(tmpdir) / "image.zip"
    legal.write_bytes(b"x" * 256)  # 256 B
    illegal.write_bytes(b"x" * 100)

    # ---- 启动主窗口 ----
    w = MainWindow()
    w.show()
    app.processEvents()
    print(f"主窗口已创建：{w.windowTitle()}")

    drop = w.drop_zone()
    log = w.log_panel()
    sb = w.statusBar()

    # ---- 场景 1：拖入 1 合法 + 1 非法 ----
    print('\n--- 场景 1：1 合法 + 1 非法 ---')
    result1 = drop.add_files([str(legal), str(illegal)])
    app.processEvents()
    print(f"  add_files 返回: {result1}")
    print(f"  列表 count: {drop.count()}")
    print(f"  状态栏: {sb.currentMessage()!r}")
    print("  日志:")
    for line in log.toPlainText().splitlines():
        print(f"    | {line}")

    assert result1["added"] == 1, f"期望 added=1，实际 {result1['added']}"
    assert result1["rejected"] == 1, f"期望 rejected=1，实际 {result1['rejected']}"
    assert result1["duplicated"] == 0
    assert drop.count() == 1
    assert "已拒绝" in log.toPlainText()
    assert "image.zip" in log.toPlainText()
    assert "不支持的格式" in log.toPlainText()
    assert "已添加" in sb.currentMessage()

    # ---- 场景 2：再拖入 1 重复（legal 本身） + 1 新文件 ----
    print('\n--- 场景 2：1 重复 + 1 新文件（不同名 .xlsx）---')
    other = Path(tmpdir) / "sheet.xlsx"
    other.write_bytes(b"x" * 1024)
    result2 = drop.add_files([str(legal), str(other)])
    app.processEvents()
    print(f"  add_files 返回: {result2}")
    print(f"  列表 count: {drop.count()}")
    print(f"  状态栏: {sb.currentMessage()!r}")

    assert result2["added"] == 1, f"期望 added=1（.xlsx 新），实际 {result2['added']}"
    assert result2["duplicated"] == 1, f"期望 duplicated=1（.docx 重），实际 {result2['duplicated']}"
    assert result2["rejected"] == 0
    assert drop.count() == 2
    assert "重复" in log.toPlainText() or "已跳过" in log.toPlainText()
    assert "doc.docx" in log.toPlainText()

    # ---- 场景 3：清空 ----
    print('\n--- 场景 3：点 "清空" 按钮 ---')
    w.clear_files()
    app.processEvents()
    print(f"  列表 count: {drop.count()}")
    print(f"  状态栏: {sb.currentMessage()!r}")
    print("  日志最后一行:")
    log_lines = log.toPlainText().splitlines()
    for line in log_lines[-3:]:
        print(f"    | {line}")

    assert drop.count() == 0
    assert sb.currentMessage() == "就绪", f"期望 '就绪'，实际 {sb.currentMessage()!r}"
    assert "已清空列表" in log.toPlainText()

    # ---- 场景 4：Edit 菜单 "清空列表" 也接通 ----
    print('\n--- 场景 4：Edit 菜单 "清空列表" ---')
    drop.add_files([str(legal)])
    app.processEvents()
    assert drop.count() == 1
    act = w.clear_action()
    act.trigger()
    app.processEvents()
    assert drop.count() == 0
    print("  通过：Edit 菜单 trigger 后 count=0")

    # ---- 场景 5：白名单是 11 项（与 handlers REGISTRY 一致）----
    print("\n--- 场景 5：白名单动态同步 ---")
    from redactor.handlers import supported_extensions
    assert set(drop.suffixes()) == set(supported_extensions())
    print(f"  suffixes = {drop.suffixes()}")
    print(f"  handlers  = {sorted(supported_extensions())}")
    assert ".pptx" not in drop.suffixes(), "白名单不应含 .pptx（v0.6.1 修正 over-promise）"

    print("\n=== 端到端验证全部通过 ===")

    # ---- 清理 ----
    w.close()
    app.processEvents()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
