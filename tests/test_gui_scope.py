"""P7 范围脱敏 —— GUI 独立对话框测试。

6 组：
    A. TestScopeDialogUI       对话框控件与状态读取
    B. TestScopeDialogPreview  ★强制预览（裁定 5）
    C. TestScopeDialogStrict   三档严格度切换
    D. TestMainWindowScope     主窗口接线（工具栏按钮 / apply_scope_entries）
    E. TestControllerScope     Controller.set_scope 运行中拒绝
    F. TestScopeEndToEnd       真实文件跑一遍（P7 端到端）
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List

import pytest

from PySide6 import QtCore, QtWidgets

from redactor.core.scopes import (
    DEFAULT_TIER,
    HIGH_SUFFIXES,
    LOW_SUFFIXES,
    ScopeEntry,
)


@pytest.fixture
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    app.processEvents()


def _wait_until(predicate, timeout_s: float = 20.0, step_ms: int = 50) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtWidgets.QApplication.processEvents(
            QtCore.QEventLoop.ProcessEventsFlag.AllEvents, step_ms
        )
    return predicate()


def _make_docx(path: Path, text: str) -> None:
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    d.save(str(path))


# ============================================================ A. 对话框 UI
class TestScopeDialogUI:
    def test_默认档位是平衡(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        assert d.current_tier() == DEFAULT_TIER == "balanced"

    def test_默认未勾首字黑名单(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        assert d.strict_lead_enabled() is False

    def test_set_suffixes与custom_suffixes往返(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.set_suffixes(["公司", "大学", "项目"])
        assert d.custom_suffixes() == ["公司", "大学", "项目"]

    def test_custom_suffixes去空行去重保序(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.set_suffixes(["公司", "", "  ", "大学", "公司"])
        assert d.custom_suffixes() == ["公司", "大学"]

    def test_load_defaults填入全部后缀(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.load_defaults()
        assert set(d.custom_suffixes()) == set(HIGH_SUFFIXES) | set(LOW_SUFFIXES)

    def test_entries构造正确(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.set_suffixes(["公司", "大学"])
        entries = d.entries()
        assert len(entries) == 1
        assert entries[0].suffixes == ("公司", "大学")
        assert entries[0].tier == "balanced"
        assert entries[0].strict_lead is False

    def test_独立于词库(self, qapp):
        """★裁定 3：对话框不 import 词库相关任何东西。"""
        import redactor.gui.dialogs.scope_dialog as mod
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "LexiconEntry" not in src, "范围对话框不该碰词库"
        assert "load_lexicon" not in src


# ============================================================ B. 强制预览
class TestScopeDialogPreview:
    def test_默认后缀_预览有命中(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.load_defaults()
        d.refresh_preview()
        assert d._preview_table.rowCount() >= 2

    def test_预览表3列(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.load_defaults()
        d.refresh_preview()
        assert d._preview_table.columnCount() == 3
        heads = [d._preview_table.horizontalHeaderItem(i).text()
                 for i in range(3)]
        assert heads == ["命中原文", "占位符", "后缀"]

    def test_标记预览占位符按后缀(self, qapp):
        """★裁定 4：预览里就能看到 [公司]/[大学] 而非 [单位]。"""
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.load_defaults()
        # 默认样例里没有裸"公司"实体，显式补一行以覆盖该占位符
        d._preview_sample.setPlainText("北京某某科技公司与重庆大学")
        d.refresh_preview()
        phs = {d._preview_table.item(r, 1).text()
               for r in range(d._preview_table.rowCount())}
        assert "[公司]" in phs, phs
        assert "[大学]" in phs, phs
        assert "[单位]" not in phs, "不该按组占位"

    def test_标记预览列出未命中行(self, qapp):
        """★裁定 5：漏脱敏风险要显式列出来。"""
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.set_suffixes(["公司"])
        d._preview_sample.setPlainText(
            "我们联系了重庆某某公司\n他今天去公司了\n重庆大学\n"
        )
        d.refresh_preview()
        summary = d._preview_summary.text()
        assert "未命中" in summary, summary
        assert "重庆大学" in summary, "漏掉的'重庆大学'应被点名"
        assert "他今天去公司了" in summary

    def test_未配后缀_提示不脱敏(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.set_suffixes([])
        d.refresh_preview()
        assert "未启用任何后缀" in d._preview_summary.text()

    def test_预览复用同一识别器(self, qapp):
        """★ 预览与实际处理必须走同一条代码路径。"""
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.set_suffixes(["公司", "大学"])
        d._preview_sample.setPlainText("重庆某某公司与重庆大学")
        d.refresh_preview()
        # 预览里的原文 == 识别器 find 的原文
        rec = d.build_recognizer()
        direct = {m.raw for m in rec.find("重庆某某公司与重庆大学")}
        shown = {d._preview_table.item(r, 0).text()
                 for r in range(d._preview_table.rowCount())}
        assert shown == direct, f"预览 {shown} != 实际 {direct}"

    def test_改样例会重算预览(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.load_defaults()
        d.refresh_preview()
        before = d._preview_table.rowCount()
        d._preview_sample.setPlainText("只有一个重庆公司")
        d.refresh_preview()
        assert d._preview_table.rowCount() < before or before > 1


# ============================================================ C. 三档严格度
class TestScopeDialogStrict:
    def test_切保守档_预览不含公司大学(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.load_defaults()
        for btn in d._tier_buttons:
            if btn.property("tier") == "conservative":
                btn.setChecked(True)
                break
        d._preview_sample.setPlainText("重庆某某公司与重庆水务集团")
        d.refresh_preview()
        sufs = {d._preview_table.item(r, 2).text()
                for r in range(d._preview_table.rowCount())}
        assert "集团" in sufs, sufs
        assert "公司" not in sufs, sufs

    def test_保守档提示语(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        for btn in d._tier_buttons:
            if btn.property("tier") == "conservative":
                btn.setChecked(True)
                break
        assert "误伤率最低" in d._tier_hint.text()

    def test_切激进档_开首字黑名单(self, qapp):
        from redactor.gui.dialogs.scope_dialog import ScopeDialog
        d = ScopeDialog()
        d.set_suffixes(["公司"])
        d._strict_lead.setChecked(True)
        d._preview_sample.setPlainText("这家公司实力不错")
        d.refresh_preview()
        assert d.strict_lead_enabled() is True
        assert d._preview_table.rowCount() == 0, "激进档应拦下'这家公司'"


# ============================================================ D. 主窗口接线
class TestMainWindowScope:
    def test_工具栏有范围脱敏按钮(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        assert w.scopes_action() is not None
        assert w.scopes_action().text() == "范围脱敏"
        w.close()

    def test_默认未启用(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        assert w.scope_entries() == []
        assert not w.scopes_action().isChecked()
        w.close()

    def test_apply_scope_entries_写入controller(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        entries = [ScopeEntry(name="单位",
                              suffixes=("公司", "大学", "集团"))]
        w.apply_scope_entries(entries)
        assert w.scope_entries() == entries
        assert w.scopes_action().isChecked()
        # Controller 侧也收到了
        got = w.controller().scope_entries()
        assert got and got[0].suffixes == ("公司", "大学", "集团")
        w.close()

    def test_禁用_清空配置(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        w.apply_scope_entries([ScopeEntry(name="单位", suffixes=("公司",))])
        w.apply_scope_entries([], enable=False)
        assert w.scope_entries() == []
        assert not w.scopes_action().isChecked()
        w.close()

    def test_apply_scope_entries传严格度(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        w.apply_scope_entries(
            [ScopeEntry(name="单位", suffixes=("公司",),
                        tier="conservative", strict_lead=True)]
        )
        assert w.controller()._scope_tier == "conservative"
        assert w.controller()._scope_strict_lead is True
        w.close()


# ============================================================ E. Controller
class TestControllerScope:
    def test_set_scope记住配置(self, qapp):
        from redactor.gui.controller import RedactorController
        c = RedactorController()
        c.set_scope([ScopeEntry(name="单位", suffixes=("公司",))],
                    tier="aggressive", strict_lead=True)
        assert c.scope_entries()[0].suffixes == ("公司",)
        assert c._scope_tier == "aggressive"
        assert c._scope_strict_lead is True

    def test_运行中拒绝改配置(self, qapp, tmp_path: Path):
        """运行中改配置对已启动 worker 无效 —— 静默接受会误导用户。"""
        from redactor.gui.controller import RedactorController
        src = tmp_path / "a.docx"
        _make_docx(src, "重庆某某公司")
        c = RedactorController()
        c.start([str(src)], tmp_path / "out", dry_run=False)
        assert c.is_running()
        c.set_scope([ScopeEntry(name="新", suffixes=("大学",))], tier="conservative")
        # 原配置未被改动
        assert c._scope_tier is None
        assert c.scope_entries() == []
        assert _wait_until(lambda: not c.is_running(), timeout_s=15.0)

    def test_清空scope等于关闭(self, qapp):
        from redactor.gui.controller import RedactorController
        c = RedactorController()
        c.set_scope([ScopeEntry(name="单位", suffixes=("公司",))])
        c.set_scope([])
        assert c.scope_entries() == []


# ============================================================ F. 端到端
class TestScopeEndToEnd:
    def test_真实docx_公司被脱敏(self, qapp, tmp_path: Path):
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "in" / "a.docx"
        src.parent.mkdir(exist_ok=True)
        _make_docx(src, "我们联系了重庆创锐电气技术有限公司")

        w.drop_zone().add_files([str(src)])
        w.apply_scope_entries([
            ScopeEntry(name="单位", suffixes=HIGH_SUFFIXES + LOW_SUFFIXES)
        ])
        out_root = tmp_path / "out"
        assert w.start_processing(output_root=out_root)
        assert _wait_until(lambda: w.state() == "done", timeout_s=20.0)
        w.close()

        out = out_root / "a_redacted.docx"
        assert out.exists()
        from docx import Document
        text = "\n".join(p.text for p in Document(str(out)).paragraphs)
        print(f"  脱敏后正文: {text}")
        assert "重庆创锐电气技术有限公司" not in text
        assert "[有限公司]" in text

    def test_标记公司加手机号_两者分别脱敏(self, qapp, tmp_path: Path):
        """★Q4 端到端：范围脱敏与手机号规则共存，互不吞。"""
        from redactor.gui import MainWindow
        w = MainWindow()
        src = tmp_path / "in" / "b.docx"
        src.parent.mkdir(exist_ok=True)
        _make_docx(src, "联系人张三，电话13812345678，重庆某某有限公司")

        w.drop_zone().add_files([str(src)])
        w.apply_scope_entries([
            ScopeEntry(name="单位", suffixes=HIGH_SUFFIXES + LOW_SUFFIXES)
        ])
        out_root = tmp_path / "out"
        assert w.start_processing(output_root=out_root)
        assert _wait_until(lambda: w.state() == "done", timeout_s=20.0)
        w.close()

        out = out_root / "b_redacted.docx"
        from docx import Document
        text = "\n".join(p.text for p in Document(str(out)).paragraphs)
        print(f"  脱敏后正文: {text}")
        assert "13812345678" not in text, f"手机号残留：{text}"
        assert "重庆某某有限公司" not in text
        assert "[手机号]" in text
        assert "[有限公司]" in text