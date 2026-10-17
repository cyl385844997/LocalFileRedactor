"""xlsx 处理器（第一版：单元格 + 6 类结构载体）。

设计约束（与 PDF P3b 一致）：

* **绝不覆盖原文件**：走 :func:`~redactor.handlers.base.prepare_output`。
* **报告不含任何原文**：位置标签一律用序号，见
  :mod:`redactor.handlers.xlsx_structures` 的"安全红线"。
* **宁可不做，也不做半吊子**：做不了的（超精度数值、共享/数组公式）
  **显式告警**，绝不静默跳过。

三类"结构不可靠即漏脱敏"的载体都走告警通道，与 docx 的
``_UNSAFE_SKIP`` 同一思路。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from redactor.core.engine import RedactionEngine
from redactor.core.models import FileResult, HitStat, Match
from redactor.handlers.base import BaseHandler, HandlerError, prepare_output
from redactor.handlers.xlsx_structures import (
    STRUCTURE_LOCATION_INDEX,
    scrub_chart_parts,
    scrub_structures,
)

#: 位置键：``(规则名, 载体类别, 序号)``
#:
#: 🔴 **必须含载体类别**：单元格、工作簿属性、定义名称三类都用 ``-1``
#: 或从 0 开始的序号，若只用 ``(规则_name, start)`` 会撞键 ——
#: "两个 sheet 的 A1 都有手机号"会共用同一个标签，后写的覆盖先写的、
#: 报告少报（实测同类问题在 PDF P3b 出现过：元数据与结构都用 ``page_index=-1``）。
PositionKey = Tuple[str, int, int]


def location_key(match: Match) -> PositionKey:
    """由 :class:`Match` 生成位置键。

    ``Match.page_index`` 承载"载体类别下标"：
      - ``>= 0`` → 该值是**工作表序号减一**
      - ``-1`` → 工作簿级载体（属性/定义名称）
    ``Match.start`` 是全局递增序号（由 ``_Collector`` 分配），
    保证同一载体类别内不同命中也有不同标签。
    """
    return (match.rule_name, match.page_index, match.start)


class XlsxHandler(BaseHandler):
    """Excel 工作簿处理器。"""

    extensions = (".xlsx",)
    display_name = "Excel 工作簿（单元格 + 结构载体）"

    def process(
        self,
        source: Path,
        output: Path,
        engine: RedactionEngine,
        *,
        dry_run: bool = False,
        overwrite_output: bool = False,
    ) -> FileResult:
        """处理 .xlsx 中的单元格与结构载体。

        Args:
            source: 源文件路径（**只读**，绝不回写）。
            output: 输出文件路径。
            engine: 脱敏引擎。
            dry_run: True 时只统计，不写文件。
            overwrite_output: 是否允许覆盖已存在的输出。

        Returns:
            处理结果。``message`` 会汇总未处理项（超精度数值、
            共享/数组公式等）与各载体命中分布。

        Raises:
            HandlerError: 文件加密、损坏、权限不足、输出冲突等。
        """
        started = time.perf_counter()
        prepare_output(source, output, overwrite_output=overwrite_output)

        wb = self._open(source)

        try:
            all_matches: List[Match] = []
            locations: Dict[PositionKey, str] = {}

            # ---- 扫描（只读语义）----
            # 单元格正文走"扫描"，因为涂改式替换在 xlsx 里不适用
            # （Excel 没有"涂黑"这种格式），统一走值替换。
            scanned, locations = self._scan(wb, engine, locations)

            if dry_run:
                hits = RedactionEngine.aggregate(
                    scanned, locations, key_of=location_key
                )
                return FileResult(
                    source=str(source),
                    output=None,
                    status="ok",
                    hits=hits,
                    message=self._describe(hits, locations, [], dry_run=True),
                    elapsed_ms=self._elapsed(started),
                )

            prepare_output(source, output, overwrite_output=overwrite_output)

            # ---- 结构清洗（6 类内存态容器；图表除外）----
            struct = scrub_structures(
                wb,
                engine,
                start_seq=len(scanned),
                dry_run=False,
            )
            # 结构清洗里单元格也会被处理 → 需与扫描结果**去重**，
            # 否则同一个单元格会算两次命中。
            scanned = self._dedup(scanned, struct.matches)
            for m, label in zip(struct.matches, struct.labels):
                locations[location_key(m)] = label

            # ---- 落盘（内存态容器的改动到此写进文件）----
            self._save(wb, output)

            # ---- 图表：必须在 save **之后**改 zip ----
            # 🔴🔴 **顺序是硬要求，且已踩过一次坑**：
            #    ① 图表必须读磁盘上的 chart*.xml（openpyxl 把图表重建成
            #       对象树，内存里拿不到原始字节）；
            #    ② 图表清洗必须放在**最后一次 save 之后** ——
            #       早期版本把它夹在两次 save 之间，第二次 ``wb.save()``
            #       把清洗好的 chart*.xml 又覆盖回未清洗版，
            #       实测"命中计了 1 次、文件里仍残留手机号"
            #       （**报告说有、文件没清**，最坏的一类不一致）；
            #    ③ 反过来若把内存态容器也放到 save 之后，改动同样不落盘
            #       （实测"除图表外全部未清洗"）。
            #    → 正确顺序：内存态清洗 → save → 图表 zip 改写。
            chart_result = scrub_chart_parts(
                output, engine, start_seq=len(scanned)
            )
            scanned = self._dedup(scanned, chart_result.matches)
            for m, label in zip(chart_result.matches, chart_result.labels):
                locations[location_key(m)] = label
            warnings = list(struct.warnings) + list(chart_result.warnings)
            parts = dict(struct.parts_touched)
            for k, v in chart_result.parts_touched.items():
                parts[k] = parts.get(k, 0) + v

            hits = RedactionEngine.aggregate(
                scanned, locations, key_of=location_key
            )
            return FileResult(
                source=str(source),
                output=str(output),
                status="ok",
                hits=hits,
                message=self._describe(
                    hits, locations, warnings, dry_run=False, parts=parts,
                ),
                elapsed_ms=self._elapsed(started),
            )
        finally:
            try:
                wb.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 内部
    def _open(self, source: Path):
        """只读打开工作簿。

        Raises:
            HandlerError: 加密、损坏、格式非法。
        """
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover
            raise HandlerError(
                HandlerError.UNSUPPORTED,
                "处理 .xlsx 需要 openpyxl，请先安装：pip install openpyxl",
            ) from exc
        try:
            # 🔴 不能用 read_only=True：只读模式下无法写回。
            #   data_only 也**必须留 False** —— 实测 data_only=True 时
            #   公式单元格读到的是缓存值（空），公式本身读不到，
            #   会导致公式里的敏感串漏脱敏。
            return load_workbook(str(source), data_only=False, keep_vba=False)
        except PermissionError as exc:
            raise HandlerError(
                HandlerError.PERMISSION, f"无权限读取：{source}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HandlerError(
                HandlerError.CORRUPTED,
                f"无法打开 xlsx（可能已损坏、加密或格式非法）：{exc}",
            ) from exc

    def _scan(
        self,
        wb,
        engine: RedactionEngine,
        locations: Dict[PositionKey, str],
    ) -> Tuple[List[Match], Dict[PositionKey, str]]:
        """扫描所有工作表单元格，返回命中与位置标签。

        🔴 **必须遍历全部 sheet**：``load_workbook()`` 默认含
        ``hidden`` / ``veryHidden``（实测隐藏页确实有脏数据），
        而用户在 Excel 里默认看不见隐藏页 → 不清即漏脱敏。
        """
        matches: List[Match] = []
        seq = 0
        for sheet_index, ws in enumerate(wb.worksheets):
            for row in ws.iter_rows():
                for cell in row:
                    text = self._cell_text(cell)
                    if not text:
                        continue
                    found = engine.scan(text)
                    if not found:
                        continue
                    sheet_seq = sheet_index + 1
                    hidden = (
                        getattr(ws, "sheet_state", "visible") != "visible"
                    )
                    label = _cell_label(ws, cell, sheet_seq, hidden)
                    for m in found:
                        matches.append(
                            Match(
                                rule_name=m.rule_name,
                                placeholder=engine.render_placeholder(m),
                                start=seq,
                                end=seq + 1,
                                raw=m.raw,
                                page_index=sheet_index,
                            )
                        )
                        locations[(m.rule_name, sheet_index, seq)] = label
                        seq += 1
        return matches, locations

    @staticmethod
    def _cell_text(cell) -> str:
        """取单元格的用户可见文本；取不到返回空串。

        🔴 **数组公式不能 ``str()``**：其 ``value`` 是 ``ArrayFormula``
        对象，转字符串会得到 ``<openpyxl...object at 0x...>`` 这种垃圾，
        反而制造假命中。
        """
        value = cell.value
        if value is None:
            return ""
        if value.__class__.__name__ == "ArrayFormula":
            return ""
        if isinstance(value, bool):
            return ""
        if isinstance(value, (int, float)):
            return str(value)
        if not isinstance(value, str):
            return ""
        return value

    def _save(self, wb, output: Path) -> Path:
        """把工作簿保存到 ``output``，返回该路径。

        Raises:
            HandlerError: 权限不足或写入失败。
        """
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            wb.save(str(output))
        except PermissionError as exc:
            raise HandlerError(
                HandlerError.PERMISSION, f"无权限写入：{output}"
            ) from exc
        except OSError as exc:
            raise HandlerError(
                HandlerError.IO_ERROR, f"写入失败：{output}（{exc}）"
            ) from exc
        return output

    @staticmethod
    def _dedup(
        scanned: List[Match], structural: List[Match]
    ) -> List[Match]:
        """去掉"扫描阶段已计、结构清洗又计一次"的重复命中。

        🔴 **只按"同一容器同一格"去重，绝不能只按 ``(rule_name, raw)``**：
            早期版本用 ``(rule_name, raw, page_index)`` 去重，结果把
            **工作簿属性标题里的手机号**当成"A1 单元格里那个手机号的重复"
            删掉了 → 报告少报（实测 parts 说"工作簿属性 2 处"，
            但明细里只有"属性 作者"，属性标题凭空消失）。
            同一份文件里**同一个敏感串出现在不同载体**是常态，
            每一处都要单独报。

        正确做法：结构清洗里的**单元格**命中与扫描阶段一一对应
        （都是"那个格子的值"），但**非单元格**载体（批注/页眉/属性/
        定义名称/图表）扫描阶段**根本没扫**，不该被去重逻辑波及。

        故这里按 `page_index` 区分：``>= 0`` 是单元格（会被去重），
        ``-1`` 是非单元格载体（保留）。
        """
        if not structural:
            return scanned
        struct_cell_keys = {
            (m.rule_name, m.raw, m.page_index)
            for m in structural
            if m.page_index >= 0
        }
        out: List[Match] = [
            m for m in scanned
            if (m.rule_name, m.raw, m.page_index) not in struct_cell_keys
        ]
        base = len(out)
        fixed = [
            Match(
                rule_name=m.rule_name,
                placeholder=m.placeholder,
                start=base + i,
                end=base + i + 1,
                raw=m.raw,
                page_index=m.page_index,
            )
            for i, m in enumerate(structural)
        ]
        return out + fixed

    @staticmethod
    def _describe(
        hits: List[HitStat],
        locations: Dict[PositionKey, str],
        warnings: Sequence[str],
        *,
        dry_run: bool,
        parts: Optional[Dict[str, int]] = None,
    ) -> str:
        """组装面向用户的一句话说明。"""
        parts_list: List[str] = []
        total = sum(h.count for h in hits)
        prefix = "发现" if dry_run else "已处理"
        if not total:
            note = f"{prefix} 0 处命中"
        else:
            note = f"{prefix} {total} 处命中"
        if locations:
            sheets = {
                key[1] for key in locations if isinstance(key[1], int)
                and key[1] >= 0
            }
            if sheets:
                note += f"，分布在 {len(sheets)} 个工作表"
        if parts:
            shown = "、".join(f"{k} {v} 处" for k, v in parts.items() if v)
            if shown:
                note += f"（{shown}）"
        if warnings:
            note += "；⚠️ " + "；⚠️ ".join(str(w) for w in warnings)
        return note

    @staticmethod
    def _elapsed(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)


def _cell_label(ws, cell, sheet_seq: int, hidden: bool) -> str:
    """单元格标签（薄封装，便于单测替换）。"""
    from redactor.handlers.xlsx_structures import _merge_range_of, sheet_label

    formula = cell.data_type == "f" or (
        isinstance(cell.value, str) and str(cell.value).startswith("=")
    )
    return sheet_label(
        sheet_seq,
        cell.coordinate,
        hidden=hidden,
        merge=_merge_range_of(ws, cell),
        formula=formula,
    )


__all__ = ["XlsxHandler", "STRUCTURE_LOCATION_INDEX", "location_key"]
