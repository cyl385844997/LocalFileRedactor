"""处理报告：**零敏感内容**。

报告只允许出现：文件路径、状态、规则名、命中次数、位置（页/段/行）、失败原因码。
严禁出现任何被脱敏的原文——这是本项目的核心价值承诺，写代码时必须守住。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from redactor.core.models import FileResult

# 失败原因码 -> 中文说明。报告里展示中文，便于人工兜底判断。
REASON_LABELS: Dict[str, str] = {
    "unsupported_format": "格式不支持",
    "encrypted_file": "文件已加密（PDF 密码 / Office 密码保护）",
    "corrupted_file": "文件损坏或结构异常",
    "permission_denied": "权限不足（文件被占用或无读取权限）",
    "io_error": "读写失败",
    "output_exists": "输出文件已存在，为防覆盖已跳过",
    "output_same_as_source": "输出路径与源文件相同（安全拦截）",
    "empty_file": "文件为空",
    "unknown_error": "未知错误",
    "rule_error": "规则文件加载失败",
}


@dataclass
class Report:
    """一次批量脱敏任务的完整报告。"""

    dry_run: bool = False
    results: List[FileResult] = field(default_factory=list)

    def add(self, result: FileResult) -> None:
        """加入一个文件的结果。"""
        self.results.append(result)

    # ---- 统计 ----
    @property
    def succeeded(self) -> List[FileResult]:
        """成功的文件列表。"""
        return [r for r in self.results if r.status == "ok"]

    @property
    def failed(self) -> List[FileResult]:
        """失败的文件列表。"""
        return [r for r in self.results if r.status == "failed"]

    @property
    def skipped(self) -> List[FileResult]:
        """跳过的文件列表（如 dry-run 无输出）。"""
        return [r for r in self.results if r.status == "skipped"]

    @property
    def total_hits(self) -> int:
        """总命中数。"""
        return sum(r.total_hits for r in self.results)

    @property
    def hit_by_rule(self) -> Dict[str, int]:
        """按规则名汇总的命中次数。"""
        counter: Dict[str, int] = {}
        for result in self.results:
            for stat in result.hits:
                counter[stat.rule_name] = counter.get(stat.rule_name, 0) + stat.count
        return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))

    # ---- 输出 ----
    def to_dict(self) -> dict:
        """转为可 JSON 序列化的字典（同样零敏感内容）。"""
        return {
            "dry_run": self.dry_run,
            "totals": {
                "files": len(self.results),
                "succeeded": len(self.succeeded),
                "failed": len(self.failed),
                "hits": self.total_hits,
                "by_rule": self.hit_by_rule,
            },
            "files": [
                {
                    "source": r.source,
                    "output": r.output,
                    "status": r.status,
                    "reason": r.reason,
                    "reason_label": REASON_LABELS.get(r.reason or "", r.reason),
                    "message": r.message,
                    "elapsed_ms": r.elapsed_ms,
                    "hits": [
                        {
                            "rule": s.rule_name,
                            "placeholder": s.placeholder,
                            "count": s.count,
                            "locations": list(s.locations),
                        }
                        for s in r.hits
                    ],
                }
                for r in self.results
            ],
        }

    def to_json(self, *, indent: int = 2) -> str:
        """输出 JSON 字符串。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_text(self) -> str:
        """输出人类可读文本。"""
        mode = "试运行（未写入任何文件）" if self.dry_run else "正式处理"
        lines: List[str] = [
            "=" * 60,
            f"本地文件脱敏报告 — {mode}",
            "=" * 60,
            f"文件总数：{len(self.results)}    成功：{len(self.succeeded)}"
            f"    失败：{len(self.failed)}    命中总数：{self.total_hits}",
        ]

        by_rule = self.hit_by_rule
        if by_rule:
            lines.append("")
            lines.append("按规则统计：")
            for rule_name, count in by_rule.items():
                lines.append(f"  - {rule_name}：{count} 处 -> [{rule_name}]")

        lines.append("")
        lines.append("逐文件明细：")
        for result in self.results:
            mark = {"ok": "成功", "failed": "失败", "skipped": "跳过"}.get(result.status, result.status)
            lines.append(f"  [{mark}] {result.source}")
            if result.output:
                lines.append(f"         输出 -> {result.output}")
            if result.status == "failed":
                label = REASON_LABELS.get(result.reason or "", result.reason or "未知")
                lines.append(f"         原因：{label}")
                if result.message:
                    lines.append(f"         说明：{result.message}")
            for stat in result.hits:
                loc = f"，位置：{'、'.join(stat.locations)}" if stat.locations else ""
                lines.append(f"         {stat.placeholder} x {stat.count}{loc}")
            # 成功但有提示（如 docx 跳过了含复杂结构的段落）同样要展示：
            # 这类文件状态是"成功"，但存在漏脱敏风险，绝不能因为不是 failed 就被略过
            if result.status != "failed" and result.message:
                lines.append(f"         注意：{result.message}")

        # 失败与跳过文件单独汇总，方便人工兜底（需求补充约束 A）
        needs_attention = [r for r in self.results if r.status in ("failed", "skipped")]
        if needs_attention:
            lines.append("")
            lines.append("需要人工处理的文件：")
            for result in needs_attention:
                label = REASON_LABELS.get(result.reason or "", result.reason or "未知")
                lines.append(f"  - {result.source}（{label}）")

        # 处理成功但存在"部分未处理"的文件同样需要人工复核：
        # 状态是成功、内容也有脱敏，但仍有段落因结构复杂被跳过。
        partial = [r for r in self.results if r.status == "ok" and r.message]
        if partial:
            lines.append("")
            lines.append("处理成功但有未处理内容的文件（需人工复核）：")
            for result in partial:
                lines.append(f"  - {result.source}：{result.message}")

        lines.append("")
        lines.append("说明：本报告仅记录规则名、命中次数与位置，不含任何被脱敏的原文。")
        lines.append("本工具不联网、不上传，所有处理均在本机完成，原文件未被修改。")
        return "\n".join(lines)

    def save(self, path: str, *, as_json: bool = False) -> None:
        """把报告写入文件。

        Args:
            path: 报告输出路径。
            as_json: True 写 JSON，False 写文本。
        """
        content = self.to_json() if as_json else self.to_text()
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)