"""Word（.docx）处理器 —— P1 第一版：只处理正文段落 + 跨 run 匹配。

为什么需要"跨 run"处理
----------------------
Word 并不保证"一个语义单元 = 一个 run"。以下情况都会把一段连续文字拆成多个 run：

* 局部格式变化（部分加粗、变色）
* 拼写校对标记（``<w:proofErr>``）
* 修订残留（rsid 不同）
* 中英文混排、域代码分割

例如下面这句"手机：13800138000"在 XML 里可能是 3 个 run::

    <w:r><w:rPr><w:b/></w:rPr><w:t>手机：13800</w:t></w:r>
    <w:r><w:rPr><w:i/></w:rPr><w:t>1380</w:t></w:r>
    <w:r><w:t>00</w:t></w:r>

如果对每个 run 单独跑正则，第一个 run 里的"13800"匹配不上 11 位手机号，
**敏感信息就会完整地留在输出文件里**——这是最危险的一种漏脱敏。
因此必须先把段落内所有 run 的文本拼成一个字符串做匹配，再把结果映射回各 run。

偏移映射与受控拆分
------------------
1. **拼接**：``[run1_text, run2_text, ...]`` -> ``full_text``，同时记录每个 run 的 ``[start, end)``
2. **匹配**：对 ``full_text`` 跑正则，得到若干 ``(start, end, placeholder)``
3. **映射**：每个命中区间落在哪些 run 上（可能跨多个）
4. **替换**：只重写**被命中区间涉及到的** run；未涉及的 run 一个字节都不动，
   因此它们的格式（加粗/斜体/颜色）天然保留

替换时**不物理合并 run**，而是按需在 run 内部做 XML 级拆分：
若一个run 同时含"命中前文本"和"命中后文本"，就把它的 ``<w:t>`` 拆成多个 run，
新 run 通过深拷贝继承原 ``<w:rPr>``，格式不丢。

非 run 内容的处理策略（第一版）
------------------------------
``paragraph.runs`` 只暴露 ``<w:r>``，而段落里还可能有 ``<w:hyperlink>``、
``<w:ins>``、域代码等把 run 包起来的元素。另外 ``run.text`` 的 getter 会把
``<w:tab/>`` / ``<w:br/>`` 转成 ``\\t`` / ``\\n`` 计入文本——若直接用它拼接，
这些占位字符会让偏移量与真实字符错位。

因此第一版采取**保守策略**：

* 逐个检查 run 的子元素，只接受 ``<w:rPr>`` 与 ``<w:t>``；
* 段落级只接受 ``<w:r>`` 与 ``<w:pPr>``；
* 任何不符合的段落（br/tab/域/超链接/修订/书签等）**整段跳过**，
  并计入报告的"未处理"，绝不猜测性地替换。

宁可漏一段（报告会明确告知），也不能错位替换——错位会破坏排版，
更糟的是可能把不该脱敏的内容留下。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from redactor.core.engine import RedactionEngine
from redactor.core.models import FileResult, HitStat, Match
from redactor.handlers.base import BaseHandler, HandlerError, prepare_output
from redactor.handlers.docx_headerfooter import (
    collect_header_footer_items,
    iter_header_footer_paragraphs,
)
from redactor.handlers.docx_table import iter_all_table_paragraphs
from redactor.handlers.docx_url import redact_hyperlink_targets

# ---------------------------------------------------------------------------
# br/tab 支持（优先级 1）
#
# 关键设计：**按元素而非按字符计偏移**。
# 每个 ``<w:t>`` 贡献它的字符，每个 ``<w:br/>`` 贡献 1 个占位字符（``\n``），
# 每个 ``<w:tab/>`` 贡献 1 个占位字符（``\t``）——与 python-docx 的 ``run.text``
# 语义一致，因此"文本流的偏移"和"XML 里的位置"能一一对应。
#
# 占位字符本身**不会被敏感信息规则命中**（手机号/身份证等不可能含换行或制表符），
# 它们只是占位用于对齐偏移，替换时对应的 ``<w:br/>`` / ``<w:tab/>`` 元素原样保留。
# ---------------------------------------------------------------------------

#: run 内允许存在的子元素：格式、纯文本、换行符、制表符
_ALLOWED_RUN_CHILDREN = {qn("w:rPr"), qn("w:t"), qn("w:br"), qn("w:tab"), qn("w:cr")}

#: 段落内允许存在的子元素：段落属性、run、超链接
_ALLOWED_PARAGRAPH_CHILDREN = {qn("w:pPr"), qn("w:r"), qn("w:hyperlink")}

#: 视为"占一个字符"的元素 -> 该占位字符
_SPECIAL_CHARS = {qn("w:br"): "\n", qn("w:cr"): "\n", qn("w:tab"): "\t"}

#: mailto: 链接的协议前缀（大小写不敏感）
_MAILTO_PREFIX = "mailto:"


def _run_plain_text(run_element) -> str:
    """取 run 的文本流：``<w:t>`` 取字符，``<w:br>``/``<w:tab>`` 取占位字符。

    占位字符保证偏移对齐，**不可被匹配**，替换时会原样保留元素。

    刻意不使用 ``run.text``：它把 ``<w:tab/>``、``<w:br/>`` 转成 ``\\t``/``\\n``
    混入文本，本函数用同样语义但**按元素定位**，便于替换时精确回写。

    Args:
        run_element: ``<w:r>`` 元素（lxml 节点）。

    Returns:
        文本流（含 ``\\n`` / ``\\t`` 占位）。
    """
    parts: List[str] = []
    for child in run_element:
        if child.tag == qn("w:t"):
            parts.append(child.text or "")
        elif child.tag in _SPECIAL_CHARS:
            parts.append(_SPECIAL_CHARS[child.tag])
    return "".join(parts)


def _is_simple_run(run_element) -> bool:
    """判断 run 是否只含 ``<w:rPr>``、``<w:t>``、``<w:br>``、``<w:tab>``。

    图形、域代码、修订标记等仍视为复杂结构（跳过）。
    """
    for child in run_element:
        if child.tag not in _ALLOWED_RUN_CHILDREN:
            return False
    return True


def _is_simple_paragraph(paragraph_element) -> bool:
    """判断段落是否只含 ``<w:pPr>`` 与简单 run。"""
    for child in paragraph_element:
        if child.tag not in _ALLOWED_PARAGRAPH_CHILDREN:
            return False
    return True


class DocxHandler(BaseHandler):
    """Word 文档处理器（P1 第一版：仅正文段落）。"""

    extensions = (".docx",)
    display_name = "Word 文档（正文段落）"

    #: 段落处理结果的三种"无需返回命中"的情形（见 _process_paragraph 的说明）
    _SKIP_COMPLEX = "complex"
    _SKIP_NO_MATCH = "no_match"
    _SKIP_EMPTY = "empty"

    #: 只有 _SKIP_COMPLEX 代表"漏脱敏风险"，需要写进报告提示人工核查
    _UNSAFE_SKIP = frozenset({_SKIP_COMPLEX})

    def process(
        self,
        source: Path,
        output: Path,
        engine: RedactionEngine,
        *,
        dry_run: bool = False,
        overwrite_output: bool = False,
    ) -> FileResult:
        """处理 .docx 正文段落中的敏感信息。

        Args:
            source: 源文件路径（只读打开，绝不回写）。
            output: 输出文件路径。
            engine: 脱敏引擎。
            dry_run: True 时只统计，不写文件。
            overwrite_output: 是否允许覆盖已存在的输出文件。

        Returns:
            处理结果。``message`` 字段会说明跳过了多少个复杂段落。

        Raises:
            HandlerError: 文件加密、损坏、权限不足、输出冲突等。
        """
        # 先做写前检查（dry_run 同样检查，保证安全评估可信）
        prepare_output(source, output, overwrite_output=overwrite_output)

        document = self._open(source)

        all_hits: List[HitStat] = []
        skipped_paragraphs = 0
        total_paragraphs = 0

        for index, paragraph in enumerate(document.paragraphs, start=1):
            total_paragraphs += 1
            result = self._process_paragraph(paragraph, f"第 {index} 段", engine, dry_run=dry_run)

            # 只把"因结构复杂而无法可靠处理"的段落计入未处理。
            # "无命中""空段落"是正常情况，若也算进去会让报告谎报漏脱敏风险。
            if isinstance(result, str):
                if result in self._UNSAFE_SKIP:
                    skipped_paragraphs += 1
                continue

            hits, locations = result
            if hits:
                all_hits.extend(hits)

        # ---- 表格内段落（优先级 3）----
        # document.paragraphs 不含表格内段落，必须单独遍历；
        # 嵌套表格递归处理、合并单元格按 XML 元素去重（见 docx_table 模块）。
        table_skipped_cells = 0
        table_cells = 0
        try:
            for paragraph, location in iter_all_table_paragraphs(document):
                table_cells += 1
                result = self._process_paragraph(
                    paragraph, location, engine, dry_run=dry_run
                )
                if isinstance(result, str):
                    if result in self._UNSAFE_SKIP:
                        table_skipped_cells += 1
                    continue
                cell_hits, _ = result
                if cell_hits:
                    all_hits.extend(cell_hits)
        except RecursionError as exc:
            # 嵌套过深：跳过剩余表格但明确告知，不静默
            raise HandlerError(
                HandlerError.CORRUPTED,
                f"表格处理中止：{exc}",
            ) from exc

        # ---- 页眉页脚（优先级 4）----
        # page header/footer 存在独立 part 里，document.paragraphs 不含它们。
        # "链接到前一节"会让多个节指向同一个 part，因此按 part 去重。
        hf_skipped = 0
        hf_paragraphs = 0
        special_hf_count = 0
        try:
            hf_items, _duplicates = collect_header_footer_items(document)
            special_hf_count = sum(1 for item in hf_items if item.is_special)
            for paragraph, location in iter_header_footer_paragraphs(document):
                hf_paragraphs += 1
                result = self._process_paragraph(
                    paragraph, location, engine, dry_run=dry_run
                )
                if isinstance(result, str):
                    if result in self._UNSAFE_SKIP:
                        hf_skipped += 1
                    continue
                hf_hits, _ = result
                if hf_hits:
                    all_hits.extend(hf_hits)
        except RecursionError as exc:
            raise HandlerError(
                HandlerError.CORRUPTED, f"页眉页脚处理中止：{exc}"
            ) from exc

        # ---- URL 脱敏（rels 文件里的 mailto 链接）----
        # 必须在 document.save() **之前**做：rels 改动随 save 一并写入。
        # 页眉页脚有各自的 part 与 rels，也要一并处理（实测页眉里确实可能有链接）。
        url_hit_count = 0
        try:
            url_parts = [document.part]
            hf_items_for_url, _ = collect_header_footer_items(document)
            url_parts.extend(item.part for item in hf_items_for_url)

            for part in url_parts:
                url_hits = redact_hyperlink_targets(part, engine, dry_run=dry_run)
                url_hit_count += len(url_hits)
        except HandlerError:
            raise
        except Exception as exc:  # noqa: BLE001
            # rels 改写失败不能影响正文脱敏成果，但必须让用户知道
            raise HandlerError(
                HandlerError.CORRUPTED,
                f"超链接目标处理失败：{type(exc).__name__}: {exc}",
            ) from exc

        if not dry_run:
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                document.save(str(output))
            except PermissionError as exc:
                raise HandlerError(HandlerError.PERMISSION, f"无权限写入：{output}") from exc
            except OSError as exc:
                raise HandlerError(HandlerError.IO_ERROR, f"写入失败：{output}（{exc}）") from exc

        # URL 命中单独记一条统计（位置标为"超链接目标"，不含原地址）
        if url_hit_count:
            all_hits.append(
                HitStat(
                    rule_name="超链接目标",
                    count=url_hit_count,
                    locations=("超链接目标（rels）",),
                )
            )

        message = None
        # 正文与表格分开计数：位置标签已能区分（第 N 段 / 表N RXCY），
        # 但汇总数字必须分开，否则无法判断漏脱敏集中在哪。
        parts = []
        if total_paragraphs:
            body_text = f"共 {total_paragraphs} 个正文段落"
            if skipped_paragraphs:
                body_text += f"（其中 {skipped_paragraphs} 个含域代码/修订标记等复杂结构，已跳过未脱敏）"
            parts.append(body_text)
        if table_cells:
            table_text = f"{len(document.tables)} 个表格共 {table_cells} 个单元格段落"
            if table_skipped_cells:
                table_text += f"（其中 {table_skipped_cells} 个含复杂结构，已跳过未脱敏）"
            parts.append(table_text)
        if hf_paragraphs:
            hf_text = f"页眉页脚共 {hf_paragraphs} 个段落"
            if hf_skipped:
                hf_text += f"（其中 {hf_skipped} 个含复杂结构，已跳过未脱敏）"
            parts.append(hf_text)

        if skipped_paragraphs or table_skipped_cells or hf_skipped:
            message = "；".join(parts) + "。请人工核查"
            # 首页/偶数页页眉只有在文档开启对应设置时才生效，
            # 但内容仍可能被脱敏，故明确告知以免误解"处理了但看不到变化"
            if special_hf_count:
                message += (
                    f"。另检测到 {special_hf_count} 处首页/偶数页页眉页脚，"
                    f"当前文档可能未开启对应设置，这些内容当前不显示，但已一并脱敏"
                )
        elif special_hf_count:
            message = (
                f"检测到 {special_hf_count} 处首页/偶数页页眉页脚，"
                f"当前文档可能未开启对应设置，这些内容当前不显示，但已一并脱敏"
            )
        if url_hit_count and message:
            message += f"。已改写 {url_hit_count} 个 mailto 超链接目标"
        elif url_hit_count:
            message = (
                f"已改写 {url_hit_count} 个 mailto 超链接的目标地址；"
                f"URL 路径中的其他敏感信息本版不处理"
            )

        return FileResult(
            source=str(source),
            output=None if dry_run else str(output),
            status="ok",
            hits=self._merge_hits(all_hits),
            message=message,
        )

    # ---- 内部方法 ----

    def _open(self, source: Path):
        """打开 docx，把各类底层异常翻译成可分类的 HandlerError。

        Raises:
            HandlerError: 加密、损坏、权限不足。
        """
        try:
            return Document(str(source))
        except PermissionError as exc:
            raise HandlerError(HandlerError.PERMISSION, f"无权限读取：{source}") from exc
        except OSError as exc:
            raise HandlerError(HandlerError.IO_ERROR, f"读取失败：{source}（{exc}）") from exc
        except Exception as exc:  # python-docx 的异常类型随版本变化，统一归类
            text = str(exc).lower()
            if "password" in text or "encrypt" in text:
                raise HandlerError(
                    HandlerError.ENCRYPTED, f"文件已加密，无法处理：{source}"
                ) from exc
            # zipfile.BadZipFile / KeyError / PackageNotFoundError 等都归为损坏
            raise HandlerError(
                HandlerError.CORRUPTED,
                f"文件损坏或不是有效的 .docx：{source}（{type(exc).__name__}: {exc}）",
            ) from exc

    def _process_paragraph(
        self,
        paragraph: Paragraph,
        location: str,
        engine: RedactionEngine,
        *,
        dry_run: bool,
    ) -> object:
        """处理单个段落（正文段落或表格内段落共用）。

        Args:
            paragraph: 段落对象。
            location: 报告用的位置标签。正文传 ``"第 3 段"``，
                表格内传 ``"表1 R2C3"``（见 :mod:`docx_table`）。
            engine: 脱敏引擎。
            dry_run: True 时不修改 XML。

        Returns:
            ``(命中统计, 位置映射)``。

            **返回字符串表示"无需返回命中"**，调用方必须区分，
            否则报告会把"无需处理"谎报成"无法处理"：

            * :attr:`_SKIP_COMPLEX`  段落含复杂结构，**本版无法可靠脱敏**（漏脱敏风险）
            * :attr:`_SKIP_NO_MATCH` 段落是纯 run 结构但没有命中（无需处理，正常）
            * :attr:`_SKIP_EMPTY`    段落无 run 或全为空白（无需处理，正常）

        位置映射形如 ``{("手机号", 起始偏移): "表1 R2C3"}``，
        其中偏移是**段落内偏移**，与 :meth:`RedactionEngine.aggregate` 的约定一致。
        """
        paragraph_element = paragraph._p

        # 段落级检查：只接受 pPr + r + hyperlink
        if not _is_simple_paragraph(paragraph_element):
            return self._SKIP_COMPLEX

        # run 元素来源有两处：段落的直接子级，以及 <w:hyperlink> 内部的 run。
        # **两者都要纳入偏移映射**——否则一个手机号若跨越"普通run / 链接文字"边界，
        # 拼接时就会缺一段，导致漏脱敏。
        # 注意用 iter 而非 findall：要取到嵌套在 hyperlink 里的 run。
        hyperlink_elements = list(paragraph_element.iter(qn("w:hyperlink")))
        for hyperlink in hyperlink_elements:
            if not self._is_simple_hyperlink(hyperlink):
                return self._SKIP_COMPLEX

        run_elements = list(paragraph_element.iter(qn("w:r")))
        if not run_elements:
            return self._SKIP_EMPTY

        # run 级检查：只接受 rPr + t（排除 br/tab/图形/域）
        for run_element in run_elements:
            if not _is_simple_run(run_element):
                return self._SKIP_COMPLEX

        # ---- 1. 拼接 + 记录区间 ----
        # 每段记录 [run元素, start, end, 原始文本]。所有偏移都相对**原始全文**，
        # 这一点是正确性的关键：绝不能边改边算后续命中的位置。
        segments: List[List] = []
        cursor = 0
        for run_element in run_elements:
            text = _run_plain_text(run_element)
            segments.append([run_element, cursor, cursor + len(text), text])
            cursor += len(text)

        full_text = "".join(item[3] for item in segments)
        if not full_text.strip():
            return self._SKIP_EMPTY

        # ---- 2. 匹配 ----
        matches: List[Match] = engine.scan(full_text)
        if not matches:
            return self._SKIP_NO_MATCH

        # ---- 3. 映射 + 统计 ----
        locations: Dict[Tuple[str, int], str] = {}
        hits: List[HitStat] = []
        for match in matches:
            locations[(match.rule_name, match.start)] = location
            hits.append(HitStat(rule_name=match.rule_name, count=1, locations=(location,)))

        # ---- 4. 一次性重写（先算清所有影响，再落盘）----
        if not dry_run:
            self._apply_matches(segments, matches, engine)

        return hits, locations

    @staticmethod
    def _is_simple_hyperlink(hyperlink_element) -> bool:
        """判断 ``<w:hyperlink>`` 内部是否只含简单 run。

        只允许 ``w:r``（run）与 ``w:proofErr``（拼写校对标记）等**无文本**元素。
        域代码、修订标记等仍视为复杂结构。
        """
        allowed = {qn("w:r"), qn("w:proofErr"), qn("w:bookmarkStart"), qn("w:bookmarkEnd")}
        for child in hyperlink_element:
            if child.tag not in allowed:
                return False
        return True

    @staticmethod
    def _merge_hits(hits: List[HitStat]) -> List[HitStat]:
        """把逐段命中合并成按规则聚合的统计。"""
        counter: Dict[str, int] = {}
        locations: Dict[str, List[str]] = {}
        for stat in hits:
            counter[stat.rule_name] = counter.get(stat.rule_name, 0) + stat.count
            bucket = locations.setdefault(stat.rule_name, [])
            for label in stat.locations:
                if label not in bucket:
                    bucket.append(label)
        return [
            HitStat(rule_name=name, count=count, locations=tuple(locations[name]))
            for name, count in sorted(counter.items())
        ]

    @staticmethod
    def _apply_matches(
        segments: List[List],
        matches: List[Match],
        engine: RedactionEngine,
    ) -> None:
        """把所有命中一次性落到各 run 上。

        本handler 最关键的部分。三个要点：

        1. **偏移基准必须固定在原始文本上**。
           早期版本逐个命中处理、每处理一个就改短``segment[3]``，
           导致后续命中的局部下标算在了错误的基准上——
           表现为银行卡号被"从中间切断"，敏感信息残留。属��最危险的失败模式。

        2. **占位符只写入第一个涉及的 run**，其余涉及的 run 只删除自己被覆盖的片段。
           否则 ``[手机号]`` 会被重复写进多个run。

        3. **未被命中的 run 一个字节都不动**，其格式因此天然保留。

        算法：对每个 run，在其**原始文本**上一次性应用所有与它相交的命中，
        按偏移顺序拼接"保留片段 + 占位符（仅首个）+ 保留片段"，最后重写一次。

        Args:
            segments: ``[run元素, start, end, 原始文本]`` 的列表（``start``/``end`` 相对全文）。
            matches: 互不重叠的命中列表（由 ``resolve_overlaps`` 保证）。
            engine: 引擎（用于取占位符文本）。
        """
        if not matches:
            return

        # 先把占位符算好（编号模式下有状态，必须在改文本前统一取，避免编号漂移）
        placeholders = [engine.render_placeholder(match) for match in matches]

        for segment in segments:
            run_element, seg_start, seg_end, original = segment

            # 找出与本 run 相交的命中（按起点排序，便于顺序拼接）
            involved = [
                (match.start, match.end, placeholder)
                for match, placeholder in zip(matches, placeholders)
                if match.start < seg_end and match.end > seg_start
            ]
            if not involved:
                continue  # 未被命中 -> 完全不动

            pieces: List[str] = []
            cursor = 0  # 相对本 run 起点的游标

            for match_start, match_end, placeholder in involved:
                # 本 run 内被覆盖的区间，换算成相对下标
                local_start = max(match_start, seg_start) - seg_start
                local_end = min(match_end, seg_end) - seg_start

                # 命中之前的原文
                if local_start > cursor:
                    pieces.append(original[cursor:local_start])

                # 只有承载命中起点的那个 run 写入占位符
                if seg_start <= match_start < seg_end:
                    pieces.append(placeholder)

                cursor = max(cursor, local_end)

            # 命中之后的原文
            if cursor < len(original):
                pieces.append(original[cursor:])

            DocxHandler._rewrite_run(run_element, "".join(pieces))

    @staticmethod
    def _rewrite_run(run_element, new_text: str) -> None:
        """把一个 run 重写为给定文本，保留 ``<w:br/>`` / ``<w:tab/>`` 等元素。

        该 run 已通过"只含 rPr/t/br/tab"检查，其结构形如::

            <w:r><w:rPr/><w:t>文本</w:t><w:br/><w:t>更多</w:t></w:r>

        因为 ``<w:br/>`` / ``<w:tab/>`` 在文本流里各占 1 个字符（``\\n`` / ``\\t``），
        而它们**永远不会被命中覆盖**，所以做法是：
        先把这些特殊元素"抽出来占位"，把 ``new_text`` 填进文本位置，
        再把特殊元素按原顺序插回。

        **新文本为空时删除整个 run 元素**。
        跨 run 替换会把中间/末尾的 run 删空，若保留一个只含空 ``<w:t>`` 的 run，
        它在 Word 里是**零宽度**的：不渲染、也选不中，用户看到的效果就是
        "这一段的斜体/加粗格式丢了"。删掉空壳后空白自然并入相邻 run 的显示。
        """
        if not new_text:
            parent = run_element.getparent()
            if parent is not None:
                parent.remove(run_element)
            return

        # 抽出特殊元素（br/tab），记录它们的原始顺序
        specials = [child for child in run_element if child.tag in _SPECIAL_CHARS]
        for child in specials:
            run_element.remove(child)

        # 找出所有 <w:t>，把 new_text 填进第一个，其余清空
        text_nodes = run_element.findall(qn("w:t"))
        if not text_nodes:
            run_element.append(_make_text_element(new_text))
        else:
            text_nodes[0].text = new_text
            # 关键：xml:space="preserve"，否则文本首尾空格会被 Word 吃掉
            text_nodes[0].set(qn("xml:space"), "preserve")
            for extra in text_nodes[1:]:
                extra.text = ""

        # 把 br/tab 元素按原顺序追加回去（跟在文本之后）
        # 这样 br/tab 位置在视觉上保持在 run 末尾——对于"文本 + 换行"这种
        # 最常见的结构（换行总是段末），视觉与原文件一致。
        for child in specials:
            run_element.append(child)


def _make_text_element(text: str):
    """构造一个 ``<w:t>`` 元素（带 xml:space="preserve"）。"""
    from docx.oxml import OxmlElement

    element = OxmlElement("w:t")
    element.text = text
    element.set(qn("xml:space"), "preserve")
    return element