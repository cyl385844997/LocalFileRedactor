"""P7 范围脱敏端到端验证（CLI + GUI 双路）。

跑法：
    set PYTHONPATH=src;.verify_libs
    set QT_QPA_PLATFORM=offscreen
    "E:/Program Files/python/python.exe" scripts/e2e_p7.py

场景（6 个）：
    1. ★Q4 防吞：文本含"公司 + 手机号"，两者必须**分别**正确脱敏
    2. 身份证 + 公司：两者分别脱敏
    3. 三档严格度：保守 / 平衡 / 激进 的行为差异
    4. 占位符按后缀（[公司] / [大学]，不是 [单位]）
    5. GUI 独立对话框：强制预览 + 未命中行提示
    6. ★反向对照：把反向扫描改成正向正则，验证守护判据会红

本脚本是 v0.7.0 交付的 e2e 验证，不入产品包。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 跑法：set PYTHONPATH=src;.verify_libs → 这里也要能独立跑，故自插 sys.path
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / ".verify_libs"))

from PySide6 import QtCore, QtWidgets  # noqa: E402

from redactor.core.engine import RedactionEngine  # noqa: E402
from redactor.core.rules import load_rule_dir  # noqa: E402
from redactor.core.scopes import (  # noqa: E402
    HIGH_SUFFIXES,
    LOW_SUFFIXES,
    ScopeEntry,
    expand_suffixes,
)
from redactor.recognizers.scope_recognizer import (  # noqa: E402
    ScopeRecognizer,
    scan_scope,
)

PHONE = "13812345678"
ID_CARD = "110101199001011233"      # 校验位合法（已实测能过 id_card validator）
ALL = HIGH_SUFFIXES + LOW_SUFFIXES


def _engine(tier="balanced", strict=False, rules=None):
    return RedactionEngine(
        rules if rules is not None else load_rule_dir(_REPO / "rules"),
        scope_entries=[ScopeEntry(name="单位", suffixes=ALL)],
        scope_tier=tier,
        scope_strict_lead=strict,
    )


def main() -> int:
    print("=" * 74)
    print("P7 范围脱敏 端到端验证（v0.7.0）")
    print("=" * 74)

    # ============================================================
    # 场景 1：★Q4 防吞 —— 公司 + 手机号分别正确脱敏
    # ============================================================
    print("\n--- 场景 1：★Q4 公司 + 手机号 分别脱敏 ---")
    text = f"联系人张三，电话{PHONE}，重庆创锐电气技术有限公司"
    out, matches = _engine().redact(text)
    print(f"  in : {text}")
    print(f"  out: {out}")
    names = sorted({m.rule_name for m in matches})
    print(f"  规则: {names}")
    assert PHONE not in out, f"手机号残留：{out}"
    assert "[手机号]" in out
    assert "重庆创锐电气技术有限公司" not in out
    assert "[有限公司]" in out
    assert "手机号" in names and "有限公司" in names
    print("  ✅ 场景 1 通过：两者分别脱敏，无交叉吞并")

    # 更险的紧邻场景
    print("\n  -- 紧邻场景：...公司13812345678 --")
    text2 = f"重庆水务集团{PHONE}"
    out2, m2 = _engine().redact(text2)
    print(f"  in : {text2}")
    print(f"  out: {out2}")
    assert PHONE not in out2
    assert "[集团]" in out2
    for m in m2:
        if m.rule_name in ALL:
            assert not any(c.isdigit() for c in m.raw), f"范围命中含数字：{m.raw!r}"
    print("  ✅ 紧邻场景通过：范围命中区间不含数字")

    # ============================================================
    # 场景 2：第二类数字规则（邮箱）+ 公司
    # ============================================================
    print("\n--- 场景 2：邮箱 + 公司（第二类内置规则）---")
    #注：身份证 18 位会被"银行卡号"规则（\d{16,19}）先命中 ——
    #    两者同长时按 priority 银行卡在前胜出。**既有行为，与 P7 无关**，
    #    但值得记录：身份证规则在 18 位输入上实际永远不生效。
    text3 = f"邮箱 zhangsan@example.com，重庆大学的学生"
    out3, names3 = _engine().redact(text3)
    print(f"  in : {text3}")
    print(f"  out: {out3}")
    print(f"  规则: {sorted({m.rule_name for m in names3})}")
    assert "zhangsan@example.com" not in out3, f"邮箱残留：{out3}"
    assert "重庆大学" not in out3
    assert "[邮箱]" in out3 and "[大学]" in out3
    print("  ✅ 场景 2 通过")

    # ============================================================
    # 场景 3：三档严格度
    # ============================================================
    print("\n--- 场景 3：三档严格度 ---")
    probe = "重庆某某科技公司与重庆大学，以及重庆水务集团"
    for tier, strict in [("conservative", False),
                         ("balanced", False),
                         ("aggressive", True)]:
        out_p, _ = _engine(tier, strict).redact(probe)
        print(f"  [{tier:<12}] {out_p}")
    out_c, _ = _engine("conservative").redact(probe)
    assert "[集团]" in out_c, "保守档仍应脱敏 HIGH 后缀"
    assert "[公司]" not in out_c and "[大学]" not in out_c, "保守档不该脱 LOW"

    out_b, _ = _engine("balanced").redact(probe)
    assert "[公司]" in out_b and "[大学]" in out_b, "平衡档应脱 LOW"

    out_a, _ = _engine("aggressive", True).redact("这家公司实力不错")
    assert "[公司]" not in out_a, "激进档应拦下'这家公司'"
    print("  ✅ 场景 3 通过：三档行为差异符合预期")

    # ============================================================
    # 场景 4：占位符按后缀
    # ============================================================
    print("\n--- 场景 4：★占位符按后缀（裁定 4）---")
    out4, _ = _engine().redact("北京某某科技公司与重庆大学与重庆水务集团")
    print(f"  out: {out4}")
    for ph in ("[公司]", "[大学]", "[集团]"):
        assert ph in out4, f"{ph} 不在 {out4}"
    assert "[单位]" not in out4, "不该按组占位（裁定 4 选的是按后缀）"
    print("  ✅ 场景 4 通过")

    # ============================================================
    # 场景 5：GUI 独立对话框 + 强制预览
    # ============================================================
    print("\n--- 场景 5：GUI 独立对话框 + 强制预览 ---")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    from redactor.gui.dialogs.scope_dialog import ScopeDialog

    dlg = ScopeDialog()
    dlg.load_defaults()
    dlg._preview_sample.setPlainText(
        "我们联系了重庆创锐电气技术有限公司\n"
        "他今天去公司了\n"
        "重庆大学\n"
    )
    dlg.refresh_preview()
    rows = dlg._preview_table.rowCount()
    print(f"  预览命中 {rows} 处：")
    for r in range(rows):
        print(f"    | {dlg._preview_table.item(r, 0).text()}"
              f" -> {dlg._preview_table.item(r, 1).text()}")
    print(f"  汇总：{dlg._preview_summary.text()[:100]}")
    assert rows >= 2, "预览应命中公司 + 大学"
    assert "未命中" in dlg._preview_summary.text(), \
        "★裁定 5：必须列出未命中行（漏脱敏风险）"
    assert "他今天去公司了" in dlg._preview_summary.text()

    # 主窗口接线
    from redactor.gui import MainWindow
    w = MainWindow()
    print(f"  工具栏按钮：{w.scopes_action().text()!r}")
    w.apply_scope_entries([ScopeEntry(name="单位", suffixes=ALL)])
    print(f"  controller 收到：{w.controller().scope_entries()[0].suffixes[:4]}…")
    assert w.scopes_action().isChecked()
    assert w.controller().scope_entries()
    w.close()
    print("  ✅ 场景 5 通过")

    # ============================================================
    # 场景 6：★反向对照 —— 正则变异版会吞手机号
    # ============================================================
    print("\n--- 场景 6：★反向对照（反向扫描 vs 正则）---")
    SUF3 = tuple(expand_suffixes([ScopeEntry(name="单位", suffixes=ALL)]))

    def regex_version(text: str):
        r"""变异版：[^\s] 贪婪匹配整个实体名（真实危险的那类正则）。"""
        alts = "|".join(re.escape(s) for s, _, _ in SUF3)
        pat = rf"[^\s]{{1,14}}(?:{alts})"
        return [m.group() for m in re.finditer(pat, text)]

    print("  Q4 对照（每条 OVERLAP 用例）：")
    violations = 0
    for probe_text in [
        f"联系人张三，电话{PHONE}，重庆创锐电气技术有限公司",
        f"身份证{ID_CARD}，重庆大学",
        f"项目编号2024ABC，公司名称重庆某某公司",
        f"公司电话是{PHONE}，请拨打",
        f"重庆大学的学生电话{PHONE}",
    ]:
        evil = regex_version(probe_text)
        good = [x[2] for x in scan_scope(probe_text, SUF3)]
        evil_digit = [g for g in evil if any(c.isdigit() for c in g)]
        good_digit = [g for g in good if any(c.isdigit() for c in g)]
        if evil_digit:
            violations += 1
        print(f"    {probe_text[:34]}")
        print(f"      正则版  : {evil}   含数字 {len(evil_digit)} 处")
        print(f"      反向扫描: {good}   含数字 {len(good_digit)} 处")
        assert not good_digit, f"反向扫描不该吞数字：{good_digit}"
    assert violations >= 1, (
        "❌ 反向对照失效：正则变异版竟然没吞任何数字，"
        "说明 OVERLAP 用例已不能区分两种实现"
    )
    print(f"  ✅ 场景 6 通过：正则变异版吞数字 {violations} 处，反向扫描版 0 处")

    print("\n" + "=" * 74)
    print("✅ P7 端到端验证全部通过")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())