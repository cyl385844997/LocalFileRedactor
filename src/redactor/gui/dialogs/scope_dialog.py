"""v0.7.0 P7 范围脱敏 —— 独立设置对话框。

裁定3：范围脱敏**独立对话框**，不与词库混在一起。
        词库是"精确列表"（用户把"重庆某某公司"整条写进去），
        范围脱敏是"泛化规则"（只写"公司"，带该后缀的实体全脱敏），
        语义不同，混在一起会让用户困惑"我的公司名到底要不要写进词库"。

裁定5：**强制预览** —— 即使误伤只有 2/22（实测基线），
        也要把命中/不命中清单摆给用户看，把"不可见的风险"变成"可见的清单"。

★预览必须复用同一个 :class:`ScopeRecognizer`
   （不另写一套正则/扫描），否则"预览没问题、跑完一片狼藉"。
   这是本对话框最重要的设计约束。
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtWidgets

from redactor.core.scopes import (
    DEFAULT_TIER,
    HIGH_SUFFIXES,
    LOW_SUFFIXES,
    TIER_LABELS,
    TIERS,
    ScopeEntry,
)
from redactor.recognizers.scope_recognizer import ScopeRecognizer, build_scope_recognizer


#: 预览样例文本（覆盖正例 / 负例 / Q4 三类，让用户一眼看出边界）
PREVIEW_SAMPLE = (
    "我们联系了重庆创锐电气技术有限公司\n"
    "联系人张三，电话13812345678\n"
    "他今天去公司了\n"
    "重庆大学、西南政法大学\n"
)


class ScopeDialog(QtWidgets.QDialog):
    """范围脱敏设置对话框（3 档严格度 + 自定义后缀 + 强制预览）。"""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("范围脱敏设置")
        self.setMinimumSize(560, 520)

        self._entries: List[ScopeEntry] = []
        self._build_ui()

    # ---------------------------------------------------------- UI
    def _build_ui(self) -> None:
        layout = QtWidgets.QBoxLayout(
            QtWidgets.QBoxLayout.Direction.TopToBottom, self
        )

        # ---- 严格度单选 ----
        tier_box = QtWidgets.QGroupBox("严格度", self)
        tier_layout = QtWidgets.QVBoxLayout(tier_box)
        self._tier_buttons = []
        for tier in TIERS:
            btn = QtWidgets.QRadioButton(TIER_LABELS[tier], tier_box)
            btn.setProperty("tier", tier)
            if tier == DEFAULT_TIER:
                btn.setChecked(True)
            btn.toggled.connect(self._refresh_preview)
            self._tier_buttons.append(btn)
            tier_layout.addWidget(btn)
        # 说明当前档位启用了哪些后缀
        self._tier_hint = QtWidgets.QLabel("", tier_box)
        self._tier_hint.setWordWrap(True)
        self._tier_hint.setStyleSheet("color: #666;")
        tier_layout.addWidget(self._tier_hint)
        layout.addWidget(tier_box)

        # ---- 自定义后缀 ----
        suf_box = QtWidgets.QGroupBox("后缀（每行一个）", self)
        suf_layout = QtWidgets.QVBoxLayout(suf_box)
        self._suffix_edit = QtWidgets.QPlainTextEdit(suf_box)
        self._suffix_edit.setPlaceholderText("公司\n大学\n项目")
        self._suffix_edit.setMaximumHeight(90)
        self._suffix_edit.textChanged.connect(self._refresh_preview)
        suf_layout.addWidget(self._suffix_edit)

        self._strict_lead = QtWidgets.QCheckBox(
            "启用首字黑名单（这/那/每/某/该/此/各/本 作首字时判为误伤）", suf_box
        )
        self._strict_lead.toggled.connect(self._refresh_preview)
        suf_layout.addWidget(self._strict_lead)
        layout.addWidget(suf_box)

        # ---- 预览（★强制，不可关闭）----
        prev_box = QtWidgets.QGroupBox(
            "预览（★强制：确认命中范围后再启用）", self
        )
        prev_layout = QtWidgets.QVBoxLayout(prev_box)

        self._preview_sample = QtWidgets.QPlainTextEdit(prev_box)
        self._preview_sample.setPlainText(PREVIEW_SAMPLE)
        self._preview_sample.setMaximumHeight(90)
        self._preview_sample.setReadOnly(False)
        prev_layout.addWidget(self._preview_sample)

        self._preview_table = QtWidgets.QTableWidget(0, 3, prev_box)
        self._preview_table.setHorizontalHeaderLabels(["命中原文", "占位符", "后缀"])
        self._preview_table.horizontalHeader().setStretchLastSection(True)
        prev_layout.addWidget(self._preview_table)

        self._preview_summary = QtWidgets.QLabel("", prev_box)
        self._preview_summary.setWordWrap(True)
        prev_layout.addWidget(self._preview_summary)
        layout.addWidget(prev_box)

        # ---- 按钮 ----
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._refresh_preview()

    # ---------------------------------------------------------- 状态
    def current_tier(self) -> str:
        """当前选中的严格度档位。"""
        for btn in self._tier_buttons:
            if btn.isChecked():
                return str(btn.property("tier"))
        return DEFAULT_TIER

    def custom_suffixes(self) -> List[str]:
        """用户输入的后缀（去空行、去首尾空白、去重、保序）。"""
        out: List[str] = []
        for raw in self._suffix_edit.toPlainText().splitlines():
            s = raw.strip()
            if s and s not in out:
                out.append(s)
        return out

    def strict_lead_enabled(self) -> bool:
        return self._strict_lead.isChecked()

    def entries(self) -> List[ScopeEntry]:
        """由当前 UI 状态构造范围条目（供应用到引擎）。"""
        return [
            ScopeEntry(
                name="单位",
                suffixes=tuple(self.custom_suffixes()),
                tier=self.current_tier(),
                strict_lead=self.strict_lead_enabled(),
            )
        ]

    def set_suffixes(self, suffixes: Sequence[str]) -> None:
        """回填后缀到编辑框（测试 / 打开对话框时用）。"""
        self._suffix_edit.setPlainText("\n".join(suffixes))

    def load_defaults(self) -> None:
        """恢复默认后缀（HIGH + LOW 全部）。"""
        self.set_suffixes(HIGH_SUFFIXES + LOW_SUFFIXES)

    # ---------------------------------------------------------- 预览
    def build_recognizer(self) -> ScopeRecognizer:
        """按当前 UI 状态造识别器 —— **预览与实际处理共用这一条路径**。"""
        return build_scope_recognizer(
            self.entries(),
            tier=self.current_tier(),
            strict_lead=self.strict_lead_enabled(),
        )

    def refresh_preview(self) -> None:
        """重算预览（公开，供测试调用）。"""
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        # 档位说明
        tier = self.current_tier()
        if tier == "conservative":
            hint = f"当前启用：{len(HIGH_SUFFIXES)} 个法律/机构后缀（误伤率最低）"
        elif tier == "balanced":
            hint = f"当前启用：{len(HIGH_SUFFIXES) + len(LOW_SUFFIXES)} 个后缀（含日常词，误伤率略高）"
        else:
            hint = (f"当前启用：{len(HIGH_SUFFIXES) + len(LOW_SUFFIXES)} 个后缀"
                    f" + 首字黑名单（误伤最少，可能漏）")
        self._tier_hint.setText(hint)

        # 造识别器 + 扫样例
        rec = self.build_recognizer()
        text = self._preview_sample.toPlainText()
        matches = rec.find(text) if not rec.is_empty() else []

        self._preview_table.setRowCount(len(matches))
        for r, m in enumerate(matches):
            for c, val in enumerate((m.raw, m.placeholder, m.rule_name)):
                item = QtWidgets.QTableWidgetItem(val)
                # 悬停显示行号，便于回原文定位
                if c == 0:
                    item.setToolTip(f"第 {r + 1} 处命中")
                self._preview_table.setItem(r, c, item)

        # 汇总 + 未命中提示（那才是"没脱敏"的风险）
        if rec.is_empty():
            self._preview_summary.setText("⚠ 未启用任何后缀 —— 不会脱敏任何实体")
            self._preview_summary.setStyleSheet("color: #8a5a00;")
            return

        missed = _uncovered_sample_lines(text, matches)
        parts = [f"样例文本命中 {len(matches)} 处"]
        if missed:
            parts.append(
                f"以下 {len(missed)} 行**未命中**（若是漏掉的实体请在后缀里补）：\n"
                + "\n".join(f"    · {ln}" for ln in missed)
            )
        self._preview_summary.setText("\n".join(parts))
        self._preview_summary.setStyleSheet("color: #444;")


def _uncovered_sample_lines(
    text: str, matches: Sequence,
    max_show: int = 8,
) -> List[str]:
    """列出样例中"完全没被任何命中覆盖"的非空行。

    这些行是**漏脱敏风险**的直接线索 —— 用户能在跑批之前就看到
    "我以为会脱敏的行，其实没脱到"。
    """
    covered: set = set()
    for m in matches:
        covered.update(range(m.start, m.end))

    out: List[str] = []
    pos = 0
    for line in text.splitlines():
        seg = set(range(pos, pos + len(line)))
        pos += len(line) + 1  # +1 是换行符
        if line.strip() and not (seg & covered):
            out.append(line.strip())
        if len(out) >= max_show:
            break
    return out


__all__ = ["PREVIEW_SAMPLE", "ScopeDialog"]