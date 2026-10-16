"""PDF 处理器 —— P1 第一阶段：只处理**有文本层**的页面。

设计约束（均为本仓库内**可复现**的合成样本所验证，勿凭直觉改）：

1. **无文本层 = 扫描件 = 必须报错**
   这类页面取不出任何字符，静默跳过等于让用户以为已脱敏、实际原文还在
   图像里。故抛 :class:`HandlerError` 并给出可执行提示。

2. **一个 span 可能只有 1 个字**
   实测 ``insert_text`` 逐字写入时，相邻同字体字符会被 PyMuPDF 合并回
   单个 span；但真实 PDF（尤其来自排版软件）常见单字独立成 span。
   因此不能"整页拼成大字符串再find()"——那样算出的偏移映射不回矩形。
   必须逐 span 用 :meth:`Recognizer.find_in_span` 匹配。

3. **redaction 必须遍历全部 rect**
   合成样本 ``search_for('陈彦霖')``（三次出现）返回 3 个 rect，只处理
   1 个时 ``get_text()`` 仍能读到该词 —— 曾据此误判"redaction 失效"。
   凡按命中位置涂改，都必须遍历全部命中。

4. **占位符写入必须检查 ``insert_textbox`` 返回值**
   合成样本：11pt 宽的命中框装不下 ``[甲方单位名称]``，``insert_textbox``
   返回 **-83.41**（负数）却**不抛异常**、静默不写。必须检查返回值并
   自适应扩框。

本阶段范围：
    P1.1 骨架、is_pdf 检测、无文本层报错
    P1.2 find_in_span 接口
    P1.3 redaction + 占位符写入
    P1.4 表格定位
    P1.5 报告集成
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import fitz  # PyMuPDF

from redactor.core.engine import RedactionEngine
from redactor.core.models import FileResult, HitStat, Match
from redactor.handlers.base import BaseHandler, HandlerError, prepare_output
from redactor.handlers.pdf_common import (
    Rect,
    SpanInfo,
    TextBoxHit,
    cell_for_rect,
    extract_spans,
    locate_cells,
    merge_overlapping_rects,
    page_has_text_layer,
    placeholder_anchor,
    rects_for_hits,
    write_placeholder,
)

#: 无文本层时给用户的原因码（复用基类的 CORRUPTED 不合适，单独一个更明确）
NO_TEXT_LAYER = "pdf_no_text_layer"


def location_key(match: Match) -> Tuple[str, int, int]:
    """生成 :meth:`RedactionEngine.aggregate` 用的位置键。

    **必须是三元组**：``match.start`` 是**页内**偏移，两页同布局会得到
    相同的 ``start``；若键只用 ``(rule_name, start)``，后写的页会覆盖
    前一页的标签，报告因此少报（合成 3 页同布局样本复现：只报 1 处）。

    不用 ``page_index * 10**7 + start`` 这类打包整数：那依赖
    "单页文本不超过 10^7 字符" 的隐含假设，异常大页面会静默撞键。
    tuple 键无此假设。

    **本函数必须与 handler 传入 ``locations`` 时用的是同一个**：
    ``aggregate`` 的 ``key_of`` 与 ``locations`` 的键若不一致，
    命中会取不到标签、被静默丢弃（``aggregate`` 的 ``strict`` 守卫
    会抛错拦截这种情况）。
    """
    return (match.rule_name, match.page_index, match.start)


class PdfHandler(BaseHandler):
    """PDF 文档处理器（P1 第一阶段：仅文本层页面）。"""

    extensions = (".pdf",)
    display_name = "PDF 文档"

    def process(
        self,
        source: Path,
        output: Path,
        engine: RedactionEngine,
        *,
        dry_run: bool = False,
        overwrite_output: bool = False,
    ) -> FileResult:
        """处理单个 PDF。

        Args:
            source: 源文件路径（只读打开，绝不覆盖）。
            output: 输出路径。
            engine: 脱敏引擎。
            dry_run: True 时只统计不写文件。
            overwrite_output: 是否允许覆盖已有输出。

        Returns:
            处理结果；命中统计**不含原文**。

        Raises:
            HandlerError: 加密、损坏、无文本层、输出冲突等。
        """
        try:
            doc = fitz.open(str(source))
        except Exception as exc:
            raise HandlerError(
                HandlerError.CORRUPTED,
                f"无法打开PDF（可能已损坏或格式非法）：{exc}",
            ) from exc

        try:
            # --- 加密检查（实测：空密码加密 needs_pass=0，authenticate("") 返回非 0 ---
            #     即无需密码即可读；需要密码时 authenticate("") 返回 0）---
            if doc.needs_pass and doc.authenticate("") == 0:
                raise HandlerError(
                    HandlerError.ENCRYPTED,
                    f"PDF 已加密且需要密码，本工具不做密码交互。"
                    f"请提供已解密的文件：{source}",
                )

            all_hits, stats, locations = self._scan(doc, engine, source)

            if dry_run:
                return FileResult(
                    source=str(source),
                    output=None,
                    status="ok",
                    hits=stats,
                    message=f"发现 {len(all_hits)} 处命中（dry-run，未写文件）",
                )

            # --- P1.3：真正落盘 ---
            prepare_output(
                source, output, overwrite_output=overwrite_output
            )
            failed = self._redact(doc, all_hits, locations)

            # garbage=4 会清理未引用的对象；deflate 压缩。
            # 二者共同保证被涂掉的文字不残留在内容流里
            # （由 pdf_verify.verify_residue 做源/输出差分验证）。
            doc.save(str(output), garbage=4, deflate=True)

            note = ""
            if failed:
                note = f"；{len(failed)} 处占位符写入失败（区域过窄），已删除但未写回占位符"
            return FileResult(
                source=str(source),
                output=str(output),
                status="ok",
                hits=stats,
                message=f"已处理 {len(all_hits)} 处命中{note}",
            )
        finally:
            doc.close()

    def _redact(
        self,
        doc: "fitz.Document",
        all_hits: Sequence[TextBoxHit],
        locations: Dict[Tuple[str, int, int], str],
    ) -> List[Tuple[str, int]]:
        """对全部命中执行 redaction 并写回占位符。

        两条硬要求（均为踩坑后加入）：

        1. **必须遍历全部命中**：合成样本 ``search_for('陈彦霖')`` 返回
           多个 rect 时，只处理第一个，``get_text()`` 仍能读到其余的——
           曾据此误判"redaction 失效"。故按页分组、逐个 add_redact_annot。

        2. **跨 span 命中必须合并矩形**：见
           :func:`~redactor.handlers.pdf_common.rects_for_hits`。

        Args:
            doc: 已打开的文档（会被就地修改）。
            all_hits: 全部命中。
            locations: ``(规则名, 页码, 页内偏移) -> 位置`` 映射。
                键必须由 :func:`location_key` 生成，与 ``aggregate`` 的
                ``key_of`` 保持同一个键函数。

        Returns:
            占位符写入失败的命中列表（已删除但没写回占位符）。
        """
        if not all_hits:
            return []

        # 按页分组：**用命中自带的 page_index**，不从位置标签字符串反解。
        # 早期实现用 ``label[label.index("第")+1]`` 之类截取页码，
        # 在第 10 页时被误判成第 1 页，导致多页文档只处理了第 1 页。
        by_page: Dict[int, List[TextBoxHit]] = {}
        for hit in all_hits:
            by_page.setdefault(hit.page_index, []).append(hit)

        failed: List[Tuple[str, int]] = []
        for page_index, hits in by_page.items():
            page = doc[page_index]
            for hit in hits:
                rects = rects_for_hits([hit])
                for rect in rects:
                    if rect.is_empty:
                        continue
                    page.add_redact_annot(rect)
            # 一次性应用本页全部标注（比逐个apply 更快也更稳）
            page.apply_redactions()

            # 占位符必须在 apply 之后写：apply 才会真正移除原文字
            for hit in hits:
                # 占位符写在【命中子串的实际位置】而非整行矩形上，
                # 否则同一 span 内的多个命中会写到同一处、视觉重叠。
                anchor = placeholder_anchor(hit)
                if anchor.is_empty:
                    continue
                ok = write_placeholder(
                    page, anchor, hit.placeholder, page_rect=page.rect
                )
                if not ok:
                    # 写入失败：文字已删除但没有占位符。
                    # 必须在报告里体现，不能默默吞掉。
                    failed.append((hit.rule_name, hit.match.start))
        return failed

    def _scan(
        self,
        doc: "fitz.Document",
        engine: RedactionEngine,
        source: Path,
        *,
        table_mode: bool = True,
    ) -> Tuple[List[TextBoxHit], List[HitStat], Dict[Tuple[str, int, int], str]]:
        """逐页扫描，收集全部命中。

        Args:
            doc: 已打开的 PDF 文档。
            engine: 脱敏引擎。
            source: 源文件路径（用于错误信息）。
            table_mode: 是否尝试识别表格并在位置标签里给出行列号
                （P1.4）。识别失败会**静默降级**为"只报页码"——
                表格行列号是附加信息，不能因此影响脱敏本身。

        Returns:
            ``(全部命中, 统计, 位置映射)``。

        Raises:
            HandlerError: 存在无文本层页面。
        """
        all_hits: List[TextBoxHit] = []
        locations: Dict[Tuple[str, int, int], str] = {}
        flat_matches: List[Match] = []
        blank_pages: List[int] = []
        hit_seq = 1    # 全文档命中序号，保证位置标签每处唯一

        for page_index in range(doc.page_count):
            page = doc[page_index]

            if not page_has_text_layer(page):
                blank_pages.append(page_index + 1)
                continue

            spans, _ = extract_spans(page)
            # P1.4：识别表格单元格，失败时降级为"只报页码"，不影响脱敏
            cells = locate_cells(page) if table_mode else []
            for span in spans:
                for recognizer in engine.recognizers:
                    for match in recognizer.find_in_span(span.text, span.offset):
                        if match.raw not in span.text:
                            # 防御：find_in_span 换算后仍应能在 span 内找到 raw。
                            # 若对不上说明 offset 传错，宁可跳过也不能错位涂改。
                            continue
                        # Match 必须带上页码：start 是**页内**偏移，
                        # 跨页同布局会撞键（见 aggregate 的 key_of 说明）。
                        match = Match(
                            rule_name=match.rule_name,
                            placeholder=match.placeholder,
                            start=match.start,
                            end=match.end,
                            raw=match.raw,
                            page_index=page_index,
                        )
                        start_in_span = match.start - span.offset
                        # 收集覆盖该命中的**全部** span——PDF 里一个 span
                        # 可能只有 1 个字，手机号会横跨多个 span，
                        # 只涂起始 span 就成了漏脱敏。
                        covering = tuple(
                            s for s in spans
                            if s.offset < match.end and match.start < s.end
                        )
                        hit = TextBoxHit(
                            rule_name=match.rule_name,
                            placeholder=match.placeholder,
                            span=span,
                            start_in_span=start_in_span,
                            end_in_span=match.end - span.offset,
                            match=match,
                            covering_spans=covering,
                            page_index=page_index,
                        )
                        all_hits.append(hit)
                        flat_matches.append(match)

                        # 位置标签：页码 + 表格行列号（若能定位到单元格）
                        label = f"第 {page_index + 1} 页"
                        if cells:
                            cell = cell_for_rect(cells, rects_for_hits([hit])[0])
                            if cell is not None:
                                label += f" {cell.label()}"
                        label += f" 第 {hit_seq} 处"
                        # 键必须是三元组 (规则名, 页码, 页内偏移)：
                        # 二元组在跨页同布局时会撞键覆盖，报告少报。
                        locations[location_key(match)] = label
                        hit_seq += 1

        if blank_pages:
            page_list = "、".join(str(n) for n in blank_pages[:10])
            more = f" 等 {len(blank_pages)} 页" if len(blank_pages) > 10 else ""
            raise HandlerError(
                NO_TEXT_LAYER,
                f"该 PDF 有 {len(blank_pages)} 页没有文本层"
                f"（第 {page_list}{more} 页）——这是扫描件，"
                f"图像里就是原文，本工具不做 OCR，无法保证脱敏。"
                f"请先OCR 成可搜索PDF，或人工处理这些页。",
            )

        stats = engine.aggregate(flat_matches, locations, key_of=location_key)
        return all_hits, stats, locations


__all__ = ["PdfHandler", "NO_TEXT_LAYER"]
