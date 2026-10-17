"""v0.6.3 端到端验证脚本（offline / offscreen）。

跑法：
    set PYTHONPATH=src;.verify_libs
    set QT_QPA_PLATFORM=offscreen
    "E:/Program Files/python/python.exe" scripts/e2e_v063.py

场景（5 个）：
    1. 命中表 4 列填充（跑 3 文件，含 1 损坏）
    2. 双击已产出 → 打开输出文件
    3. 双击未产出 → 打开源文件
    4. ★裁定 3："打开输出目录"按钮直接开最近一次目录，**不弹窗**
    5. ★裁定 4：超 500 条命中截断 + 顶部提示"仅展示前 500 条"

本脚本是 v0.6.3 交付的 e2e 验证（deliverable #2），不入产品包。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from redactor.core.models import HitStat  # noqa: E402
from redactor.gui import APP_NAME, APP_VERSION, MainWindow  # noqa: E402
from redactor.gui.widgets import PreviewPanel  # noqa: E402


def _make_docx(path: Path, text: str = "张三 13812345678") -> None:
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    d.save(str(path))


def _make_corrupted_docx(path: Path) -> None:
    path.write_bytes(b"this is not a docx")


def _wait_until(predicate, timeout_s: float = 20.0, step_ms: int = 50) -> bool:
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtWidgets.QApplication.processEvents(
            QtCore.QEventLoop.ProcessEventsFlag.AllEvents, step_ms
        )
    return predicate()


def main() -> int:
    print(f"=== {APP_NAME} v{APP_VERSION} 端到端验证（v0.6.3） ===")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    tmpdir = Path(tempfile.mkdtemp(prefix="redactor_e2e_v063_"))
    src_dir = tmpdir / "src"
    src_dir.mkdir(exist_ok=True)
    print(f"tmpdir: {tmpdir}")

    # 把 _desktop_open 换成记录器（offscreen 下真实 openUrl 会弹系统窗口）
    # 注意：必须赋**普通函数**（不能 staticmethod）——它是实例方法，
    # 赋普通函数后 Python 描述符协议会把 self 绑上。
    opened: list[str] = []

    def _fake_desktop_open(self, url):
        opened.append(url.toLocalFile())
        return True

    MainWindow._desktop_open = _fake_desktop_open  # type: ignore[assignment]

    # 把 QFileDialog 换成记录器（用于检测"打开输出目录"是否弹窗）
    dialog_calls: list[tuple] = []

    def _fake_get_dir(*a, **k):
        dialog_calls.append(a)
        return ""

    QtWidgets.QFileDialog.getExistingDirectory = _fake_get_dir  # type: ignore[assignment]

    w = MainWindow()
    w.show()
    app.processEvents()
    print(f"主窗口已创建：{w.windowTitle()}")

    pv = w.preview_panel()
    drop = w.drop_zone()

    # ============================================================
    # 场景 1：跑 3 文件（1 损坏）→ 命中表 4 列 + 输出登记
    # ============================================================
    print("\n--- 场景 1：3 文件（1 损坏），命中表 4 列填充 ---")
    g1 = src_dir / "report.docx"
    g2 = src_dir / "summary.docx"
    bad = src_dir / "broken.docx"
    _make_docx(g1, "张三 13812345678")
    _make_docx(g2, "李四 13887654321")
    _make_corrupted_docx(bad)

    drop.add_files([str(g1), str(bad), str(g2)])
    app.processEvents()
    out1 = tmpdir / "out1"
    assert w.start_processing(output_root=out1)
    assert _wait_until(lambda: w.state() == "done")
    app.processEvents()

    print(f"  命中表行数: {pv.hit_count()}")
    print(f"  命中表列头: {[pv._hits_table.horizontalHeaderItem(i).text() for i in range(pv._hits_table.columnCount())]}")
    for r in range(pv.hit_count()):
        print(f"    行{r}: {[pv.cell_text(r, c) for c in range(4)]}")
    print(f"  输出登记: {pv.output_count()} 个")
    print(f"  状态栏: {w.statusBar().currentMessage()!r}")
    assert pv.hit_count() >= 2
    assert pv.cell_text(0, 0) == "1"
    assert pv.cell_text(0, 1) == "report.docx"
    assert pv.cell_text(0, 3).endswith("处")
    assert pv.output_count() == 2
    assert "命中" in w.statusBar().currentMessage()
    print("  ✅ 场景 1 通过：4 列命中表 + 输出登记 2 个")

    # ============================================================
    # 场景 2：双击已产出 → 打开输出文件
    # ============================================================
    print("\n--- 场景 2：双击已产出 → 打开输出文件 ---")
    opened.clear()
    out_path = drop.output_path_for(str(g1))
    print(f"  g1 的 output_path: {out_path}")
    assert out_path is not None and Path(out_path).exists()

    drop.activate_row(0)
    app.processEvents()
    print(f"  实际打开: {opened}")
    assert len(opened) == 1
    assert Path(opened[0]).name == "report_redacted.docx", \
        f"应打开输出文件，实际 {opened[0]}"
    assert "report_redacted" in w.log_panel().toPlainText()
    print("  ✅ 场景 2 通过：双击已产出 → 打开输出文件")

    # ============================================================
    # 场景 3：双击未产出（损坏文件）→ 打开源文件
    # ============================================================
    print("\n--- 场景 3：双击未产出 → 打开源文件 ---")
    opened.clear()
    bad_row = None
    for r in range(drop.model().rowCount()):
        d = drop.model().item(r).data(QtCore.Qt.ItemDataRole.UserRole)
        if d["path"].endswith("broken.docx"):
            bad_row = r
            break
    assert bad_row is not None
    print(f"  broken.docx 在第 {bad_row} 行，output_path = {drop.output_path_for(str(bad))}")

    drop.activate_row(bad_row)
    app.processEvents()
    print(f"  实际打开: {opened}")
    assert len(opened) == 1
    assert Path(opened[0]).name == "broken.docx", \
        f"未产出应开源文件，实际 {opened[0]}"
    print("  ✅ 场景 3 通过：双击未产出 → 打开源文件")

    # ============================================================
    # 场景 4：★裁定 3 —— "打开输出目录"只记最近一次，不弹窗
    # ============================================================
    print("\n--- 场景 4：打开输出目录按钮（不弹窗） ---")
    opened.clear()
    dialog_calls.clear()
    print(f"  last_output_root = {w.last_output_root()}")
    assert w.last_output_root() == out1
    assert w.open_out_dir_action().isEnabled()

    w.open_out_dir_action().trigger()
    app.processEvents()
    print(f"  实际打开: {opened}")
    print(f"  QFileDialog 调用次数: {len(dialog_calls)}")
    assert len(opened) == 1 and Path(opened[0]).name == "out1"
    assert dialog_calls == [], "★ 不应弹 QFileDialog"

    # 跑第 2 批 → 按钮应指向第 2 批
    g3 = src_dir / "extra.docx"
    _make_docx(g3, "王五 13900139000")
    drop.add_files([str(g3)])
    app.processEvents()
    out2 = tmpdir / "out2"
    assert w.start_processing(output_root=out2)
    assert _wait_until(lambda: w.state() == "done")
    app.processEvents()
    print(f"  第 2 批后 last_output_root = {w.last_output_root()}")

    opened.clear()
    dialog_calls.clear()
    w.open_out_dir_action().trigger()
    app.processEvents()
    print(f"  实际打开: {opened}")
    assert len(opened) == 1 and Path(opened[0]).name == "out2", \
        f"应指向最近一次的 out2，实际 {opened}"
    assert dialog_calls == [], "第 2 次也不应弹窗"
    print("  ✅ 场景 4 通过：只记最近一次、两次都不弹窗")

    # ============================================================
    # 场景 5：★裁定 4 —— 超 500 条命中截断 + 顶部提示
    # ============================================================
    print("\n--- 场景 5：超 500 条命中截断 ---")
    pv2 = PreviewPanel()
    print(f"  默认 hit_limit = {pv2.hit_limit()}")
    assert pv2.hit_limit() == 500

    for i in range(523):
        pv2.add_hit("big.docx", HitStat(rule_name="手机号", count=1,
                                        locations=(f"第{i}段",)))
    print(f"  加了 523 条 → 实际行数 {pv2.hit_count()}")
    print(f"  被丢弃: {pv2.hits_dropped()}")
    print(f"  顶部提示可见: {pv2.notice_visible()}")
    print(f"  顶部提示文本: {pv2.notice_text()!r}")
    assert pv2.hit_count() == 500
    assert pv2.hits_dropped() == 23
    assert pv2.notice_visible() is True
    assert "仅展示前 500 条" in pv2.notice_text()
    assert "523" in pv2.notice_text()

    # 顺带验证"仅展示前 500 条"的最后一行确实存在
    assert pv2.cell_text(499, 0) == "500"
    print("  ✅ 场景 5 通过：截断到 500 行 + 顶部提示")

    # ============================================================
    # 附带：清空列表 → 预览同步清空
    # ============================================================
    print("\n--- 附带：清空列表 → 预览同步清空 ---")
    w.clear_files()
    app.processEvents()
    print(f"  命中表: {pv.hit_count()} 行，输出: {pv.output_count()} 个")
    print(f"  last_output_root: {w.last_output_root()}")
    print(f"  打开输出目录按钮 enabled: {w.open_out_dir_action().isEnabled()}")
    assert pv.hit_count() == 0
    assert pv.output_count() == 0
    assert w.last_output_root() is None
    assert not w.open_out_dir_action().isEnabled()
    print("  ✅ 附带验证通过")

    print("\n=== 端到端验证全部通过 ===")

    w.close()
    app.processEvents()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())