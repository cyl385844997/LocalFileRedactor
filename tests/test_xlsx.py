"""xlsx 结构清洗（7 类容器）的专项测试。

**本文件的取证原则**（沿用 P3b / docx 既有铁律）：

1. 每条"已清洗"的断言都配**反向对照** —— 先证明"清洗前确实能观察到
   泄露"，再断言"清洗后不泄露"。否则断言可能恒真（样本没写敏感串时，
   "输出里没有"照样成立）。
2. 断言对象是**产物**（``openpyxl.load_workbook()`` 的读回值，或
   zip 内 XML 的**实际字节**），不是生成条件。
3. 每条实现都配**变异实验**（见 :class:`TestMutations`）。

🔴 本文件踩过的三个坑（写测试时勿重走）：

1. **页眉的中文在 XML 里是数字实体**（``&#34920;&#22836;`` = "表头"），
   查明文"表头"永远查不到 → 判据恒假。只能查数字部分（手机号明文）。
2. **批注作者用英文词**（如 ``reviewer``）时，内置规则不命中它
   → "输出里还有 reviewer"是**判据错**而不是实现漏脱敏。
   判据要用**能被规则命中的**作者名（如 ``审阅人13900139000``）。
3. ``Workbook()`` 的默认 sheet 名是 ``Sheet``，不是 ``Sheet1``。
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.models import Match
from redactor.core.rules import load_rules
from redactor.handlers.xlsx_handler import XlsxHandler, location_key
from redactor.handlers.xlsx_structures import (
    MAX_SIGNIFICANT_DIGITS,
    STRUCTURE_LOCATION_INDEX,
    _sci_candidates,
    _significant_digits,
    chart_label,
    comment_label,
    defined_name_label,
    headerfooter_label,
    property_label,
    scrub_chart_parts,
    scrub_structures,
    sheet_label,
)

BUILTIN = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"

PHONE = "13800138000"
PHONE2 = "13900139000"
MAIL = "zhang.san@example.com"
MAIL2 = "li.si@corp.example.com"
#: 合法身份证（末位校验位 = 4，实测 ``engine.redact`` 命中 1 处）
ID = "110101199003074514"


def engine() -> RedactionEngine:
    return RedactionEngine(load_rules(BUILTIN))


def make_full(path: Path, *, include_chart: bool = True) -> Path:
    """造一份 7 类容器全部含敏感串的样本（落盘后再打开使用）。"""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.comments import Comment
    from openpyxl.workbook.defined_name import DefinedName

    wb = Workbook()
    ws = wb.active
    ws.title = "数据表"

    # 1) 字符串单元格
    ws["A1"] = f"联系人 {PHONE}"
    # 2) 数值单元格（11 位，可清）
    ws["A2"] = 13900139000
    # 3) 超精度数值（18 位，只能告警）
    ws["A3"] = 1.1010119900307451e17
    # 4) 公式里的字符串常量
    ws["A4"] = f'=CONCATENATE("电话","{PHONE2}")'
    # 5) 纯计算公式（不该命中）
    ws["A5"] = "=SUM(B1:B2)"
    # 6) 批注：正文 + 作者（作者名必须**能被规则命中**）
    ws["A6"] = "带批注的格子"
    ws["A6"].comment = Comment(f"正文 {MAIL}", f"审阅人{PHONE2}")
    # 7) 合并单元格（值只在左上角）
    ws["B1"] = f"合并 {PHONE}"
    ws.merge_cells("B1:D1")
    # 8) 身份证（文本形态，**必须能清**）
    ws["A7"] = f"证件 {ID}"
    # 9) 干净单元格（不该被改动）
    ws["A8"] = "完全干净的格子"

    # 10) 隐藏 sheet
    ws2 = wb.create_sheet("隐藏页")
    ws2.sheet_state = "hidden"
    ws2["A1"] = f"隐藏页 {MAIL2}"

    # 11) 页眉页脚
    # 🔴 **even / first 侧也必须造脏数据** —— 否则"只清 odd*（漏 even/first）"
    #    这条变异**测不出来**（样本里只有 odd 侧有内容，判据恒真）。
    #    这是变异实验逼出来的：首轮 16 条变异里 #6 未生效，根因就是这里。
    ws.oddHeader.center.text = f"表头 {PHONE}"
    ws.oddFooter.center.text = f"表尾 {MAIL}"
    ws.evenHeader.right.text = f"偶数页眉 {PHONE2}"
    ws.evenFooter.left.text = f"偶数页脚 {MAIL2}"
    ws.firstHeader.center.text = f"首页页眉 {PHONE}"
    ws.firstFooter.right.text = f"首页页脚 {MAIL2}"

    # 12) 工作簿属性
    wb.properties.title = f"工作簿标题 {PHONE}"
    wb.properties.creator = f"作者 {MAIL2}"

    # 13) 定义名称
    wb.defined_names.add(
        DefinedName(f"客户{PHONE}", attr_text="数据表!$A$1")
    )

    # 14) 图表
    if include_chart:
        ch = BarChart()
        ch.title = f"图表标题 {PHONE}"
        ch.add_data(Reference(ws, min_col=2, min_row=1, max_row=1))
        ws.add_chart(ch, "F1")

    wb.save(str(path))
    wb.close()
    return path


def part_bytes(path: Path, part: str) -> bytes:
    """读 zip 内某个条目的字节；不存在返回 ``b""``。"""
    with zipfile.ZipFile(path) as z:
        try:
            return z.read(part)
        except KeyError:
            return b""


def sheet_bytes(path: Path, index: int = 1) -> bytes:
    """读 ``xl/worksheets/sheetN.xml``。"""
    return part_bytes(path, f"xl/worksheets/sheet{index}.xml")


def process(src: Path, out: Path, eng: RedactionEngine | None = None):
    return XlsxHandler().process(src, out, eng or engine())


def all_labels(res) -> list[str]:
    """取全部位置标签。

    🔴 **不要去 ``res.message`` 里找标签** —— message 只有汇总句
    （"已处理 N 处命中，分布在 M 个工作表（…）"），
    **具体标签在 ``res.hits[].locations``**。
    本轮 4 条测试因此失败（查错地方 → 恒假断言）。
    """
    return [loc for h in res.hits for loc in h.locations]


# ============================================================ 反向对照
class TestLeakBaseline:
    """清洗前 7 类容器确实泄露 —— 没有这条，后面所有"干净"都是空转。"""

    def test_清洗前七类容器全部泄露(self, tmp_path):
        src = make_full(tmp_path / "leak.xlsx")

        assert PHONE.encode() in sheet_bytes(src), "单元格/页眉没造好"
        assert b"13900139000" in sheet_bytes(src), "数值单元格没造好"
        assert PHONE2.encode() in sheet_bytes(src), "公式常量没造好"
        assert MAIL.encode() in part_bytes(src, "xl/comments/comment1.xml"), (
            "批注正文没造好"
        )
        assert PHONE2.encode() in part_bytes(
            src, "xl/comments/comment1.xml"
        ), "批注作者没造好"
        assert MAIL2.encode() in sheet_bytes(src, 2), "隐藏页没造好"
        assert PHONE.encode() in part_bytes(src, "docProps/core.xml"), (
            "工作簿属性没造好"
        )
        assert PHONE.encode() in part_bytes(src, "xl/workbook.xml"), (
            "定义名称没造好"
        )
        assert PHONE.encode() in part_bytes(src, "xl/charts/chart1.xml"), (
            "图表没造好"
        )

    def test_页眉中文在xml里是数字实体(self, tmp_path):
        """🔴 记录一条会让人踩坑的事实：页眉中文**不能**用明文查。

        这是本轮真实踩过的判据陷阱（查"表头"永远查不到 → 判据恒假）。
        """
        src = make_full(tmp_path / "hdr.xlsx")
        raw = sheet_bytes(src)
        assert b"oddHeader" in raw, "页眉没写进 sheet XML"
        assert "表头".encode() not in raw, (
            "页眉中文是明文 —— 若成立，本测试的前提变了，"
            "其他按明文查页眉的判据仍可用"
        )
        # 🔴 用 bytes 正则，不要写成 `assert re.search(rb"...", raw)`
        #    后者返回 Match 对象再参与运算会抛 TypeError。
        assert re.search(rb"&#\d+;", raw), "未发现数字实体"

    def test_身份证样本本身合法(self):
        found = engine().scan(ID)
        assert found, (
            f"身份证样本 {ID} 不被规则命中 —— 判据会恒假，"
            f"请换一个合法校验位的样本"
        )


# ============================================================ 容器 1-3 单元格
class TestCells:
    """字符串 / 数值 / 公式三类单元格。"""

    def test_字符串单元格清洗(self, tmp_path):
        src = make_full(tmp_path / "s.xlsx", include_chart=False)
        out = tmp_path / "s_out.xlsx"
        process(src, out)

        from openpyxl import load_workbook

        wb = load_workbook(out)
        try:
            assert PHONE not in str(wb["数据表"]["A1"].value)
        finally:
            wb.close()

    def test_数值单元格清洗且类型变文本(self, tmp_path):
        src = make_full(tmp_path / "n.xlsx", include_chart=False)
        out = tmp_path / "n_out.xlsx"
        res = process(src, out)

        from openpyxl import load_workbook

        wb = load_workbook(out)
        try:
            v = wb["数据表"]["A2"].value
            assert "13900139000" not in str(v), f"数值未清：{v!r}"
            assert isinstance(v, str), (
                f"替换后应是文本（原来能参与计算），实为 {type(v).__name__}"
            )
        finally:
            wb.close()
        # 类型变化必须告警（会影响用户后续的 Excel 计算）
        assert "由数字变为文本" in (res.message or ""), (
            f"未告警类型变化：{res.message}"
        )

    def test_超精度数值只告警不改(self, tmp_path):
        """裁定 B1：>15 位数值精度已丢 → 只告警，**不改**。

        🔴 反向对照：这条极易写成"输出里没有身份证"（假通过），
        因为科学计数法形态**本来就匹配不到任何规则**，
        与"清干净了"无法区分。故断言**告警必须出现**。
        """
        src = make_full(tmp_path / "sp.xlsx", include_chart=False)
        out = tmp_path / "sp_out.xlsx"
        res = process(src, out)

        assert _significant_digits(1.1010119900307451e17) > (
            MAX_SIGNIFICANT_DIGITS
        ), "样本没造好：位数没超过阈值"
        assert "无法自动脱敏" in (res.message or ""), (
            f"超精度数值未告警 —— 这是最该告警的场景（精度丢失导致漏检），"
            f"静默通过等于漏脱敏：{res.message}"
        )

    def test_超精度判定必须能命中候选(self):
        """🔴 变异实验发现的真 bug 锁定：末位枚举。

        第一版只还原一个精确值 ``110101199003074500``，末位被 IEEE754
        改错 → 身份证校验位不符 → **引擎 0 命中** → 告警不触发。
        正确做法是**枚举末位**让引擎自己判。
        """
        cands = _sci_candidates("1.101011990030745e+17")
        assert cands, "还原失败"
        exact = cands[0]
        assert len(exact) == 18, f"还原长度应为 18，实为 {len(exact)}"
        eng = engine()
        assert not eng.scan(exact), (
            f"精确值 {exact} 恰好命中 —— 本测试的前提（需枚举）不成立"
        )
        assert any(eng.scan(c) for c in cands), (
            "枚举全部候选都没命中 —— 告警逻辑会失效"
        )

    def test_15位整数与科学计数法值均判定不超精度(self):
        """🔴 Marvis 复核整改：锁住 15 位临界值**不**触发超精度分支。

        反向对照：所有断言都参照 ``MAX_SIGNIFICANT_DIGITS`` 常量做交叉
        （不是写死字面量），避免阈值常量被改后测试**仍然绿**。
        覆盖整数与 float 两条路径（数值单元格写盘形态通常是 float）。
        """
        # 整数路径（纯整数 15 位）
        assert _significant_digits(123456789012345) == MAX_SIGNIFICANT_DIGITS, (
            f"15 位整数应正好等于阈值；阈值={MAX_SIGNIFICANT_DIGITS}"
        )
        # 科学计数法路径（float 写盘形态）—— 注意 1.2345678901234e14
        # 精确还原为 123456789012340.0（15 位整数）
        assert _significant_digits(1.2345678901234e14) == MAX_SIGNIFICANT_DIGITS, (
            f"15 位精度 float 应正好等于阈值；阈值={MAX_SIGNIFICANT_DIGITS}"
        )

    def test_16位整数与科学计数法值均判定超精度(self):
        """🔴 Marvis 复核整改：锁住 16 位临界值**必**判为超精度。

        反向对照：所有断言**严格大于** ``MAX_SIGNIFICANT_DIGITS``，
        既覆盖 16 位（恰好超 1），也为更高位数（17/18/19）留出余量。
        """
        # 整数路径（纯整数 16 位）
        assert _significant_digits(1234567890123456) > MAX_SIGNIFICANT_DIGITS, (
            f"16 位整数应被判定超精度；阈值={MAX_SIGNIFICANT_DIGITS}"
        )
        # 科学计数法路径（float 写盘形态，16 位精度）
        assert _significant_digits(1.23456789012345e15) > MAX_SIGNIFICANT_DIGITS, (
            f"16 位精度 float 应被判定超精度；阈值={MAX_SIGNIFICANT_DIGITS}"
        )

    def test_公式字符串常量清洗且公式保留(self, tmp_path):
        src = make_full(tmp_path / "f.xlsx", include_chart=False)
        out = tmp_path / "f_out.xlsx"
        process(src, out)

        raw = sheet_bytes(out).decode("utf-8", "replace")
        assert "<f>" in raw, "公式被删了（不该发生）"
        assert PHONE2 not in raw, "公式里仍有敏感串"
        # 纯计算公式不该被改
        assert "SUM(B1:B2)" in raw, "纯计算公式被误改"

    def test_公式含敏感串时不出现shared引用格告警(self, tmp_path):
        """锁住"共享公式引用格被展开并照常清洗"的实际行为。

        🔴 这是 v0.4.0 整改后的**声明级锁**：删了不可达 guard
        (``_is_shared_formula_reference`` 走 ``ws.parent._xlsx_path``)
        之后，必须用产物断言锁住"引用格被展开 → 照常清洗 → 无 shared 告警"
        这条不变量，否则 guard 一旦被回写（看似无害的"防御性代码"），
        测试**仍然绿**。

        反向对照：本测试若被改回"只查产物干净"，与既有的
        ``test_公式字符串常量清洗且公式保留`` 重叠，等于没锁。
        故同时断言 ``t="shared"`` 标记**不出现**在产物 sheet XML 里、
        且告警消息**不含** ``"未修改"`` / ``"shared"`` 字样。
        """
        src = make_full(tmp_path / "f.xlsx", include_chart=False)
        out = tmp_path / "f_out.xlsx"
        res = process(src, out)

        raw = sheet_bytes(out).decode("utf-8", "replace")
        # 1) openpyxl 应把 shared 引用格展开 → 标记不出现
        assert 't="shared"' not in raw, (
            f"产物里出现 t=\"shared\" 标记（shared 引用格未被展开）\n"
            f"前 800 字符：{raw[:800]!r}"
        )
        # 2) 公式内敏感串照常清
        assert PHONE2 not in raw, "公式内敏感串未清"
        # 3) 公式本身保留
        assert "<f>" in raw, "公式被删了（不该发生）"
        # 4) 告警消息里不应出现"未修改"或 shared 字样
        msg = res.message or ""
        assert "未修改" not in msg, (
            f"出现不该的'未修改'告警（公式含敏感串时不该跳过）：{msg!r}"
        )
        assert "shared" not in msg.lower(), (
            f"不该出现 shared formula 告警：{msg!r}"
        )

    def test_合并单元格值被清且标签含合并区(self, tmp_path):
        src = make_full(tmp_path / "mg.xlsx", include_chart=False)
        out = tmp_path / "mg_out.xlsx"
        res = process(src, out)
        labels = all_labels(res)
        assert any("合并区 B1:D1" in x for x in labels), (
            f"标签未标出合并区（用户拿到 B1 不知道它横跨 B1:D1）：{labels}"
        )

    def test_隐藏页被遍历且标签标隐藏(self, tmp_path):
        src = make_full(tmp_path / "hd.xlsx", include_chart=False)
        out = tmp_path / "hd_out.xlsx"
        res = process(src, out)
        labels = all_labels(res)
        assert any("（隐藏）" in x for x in labels), (
            f"隐藏页未被标注（用户在 Excel 里看不见它）：{labels}"
        )
        assert MAIL2.encode() not in sheet_bytes(out, 2), "隐藏页未清洗"

    def test_干净单元格不被改动(self, tmp_path):
        src = make_full(tmp_path / "cl.xlsx", include_chart=False)
        out = tmp_path / "cl_out.xlsx"
        process(src, out)

        from openpyxl import load_workbook

        wb = load_workbook(out)
        try:
            assert wb["数据表"]["A8"].value == "完全干净的格子"
        finally:
            wb.close()


# ============================================================ 容器 4 批注
class TestComments:
    def test_批注正文与作者都被清洗(self, tmp_path):
        src = make_full(tmp_path / "cm.xlsx", include_chart=False)
        out = tmp_path / "cm_out.xlsx"
        res = process(src, out)

        raw = part_bytes(out, "xl/comments/comment1.xml")
        assert MAIL.encode() not in raw, "批注正文未清"
        assert PHONE2.encode() not in raw, "批注作者未清"
        labels = all_labels(res)
        assert any("批注正文" in x for x in labels), f"未报正文：{labels}"
        assert any("批注作者" in x for x in labels), f"未报作者：{labels}"


# ============================================================ 容器 5 页眉页脚
class TestHeaderFooter:
    def test_页眉页脚被清洗(self, tmp_path):
        """6 种（odd/even/first × header/footer）都要清到。

        🔴 判据必须**逐个字段**断言，且样本要 even/first 侧也有脏数据 ——
            只造 odd 侧时，"只清 odd*"的变异照样通过（判据恒真）。
        """
        src = make_full(tmp_path / "hf.xlsx", include_chart=False)

        # 反向对照：6 种都确实脏
        raw_src = sheet_bytes(src)
        for tag in (b"oddHeader", b"oddFooter", b"evenHeader",
                    b"evenFooter", b"firstHeader", b"firstFooter"):
            assert tag in raw_src, f"{tag!r} 没写进 sheet XML —— 样本没造好"

        out = tmp_path / "hf_out.xlsx"
        res = process(src, out)
        raw = sheet_bytes(out).decode("utf-8", "replace")

        for needle in (PHONE, PHONE2, MAIL, MAIL2):
            assert needle not in raw, f"页眉页脚仍含 {needle}：{raw[:400]}"
        labels = all_labels(res)
        # 🔴 断言措辞必须与**实现实际产出**一致（实测标签是
        #    "偶数页页眉右" 而非"偶数页页眉"）—— 不允许改实现去迎合断言。
        assert any("页眉居中" in x for x in labels), f"未报奇数页眉：{labels}"
        assert any("偶数页页眉" in x for x in labels), (
            f"未报偶数页页眉（漏 even 侧）：{labels}"
        )
        assert any("首页页脚" in x for x in labels), (
            f"未报首页页脚（漏 first 侧）：{labels}"
        )

    def test_格式码被保留(self, tmp_path):
        """🔴 格式码（``&C`` 居中）不能被清洗破坏。"""
        src = make_full(tmp_path / "hc.xlsx", include_chart=False)
        out = tmp_path / "hc_out.xlsx"
        process(src, out)
        raw = sheet_bytes(out).decode("utf-8", "replace")
        assert "&amp;C" in raw, "居中格式码丢失"

    def test_十八个字段都覆盖(self):
        """6 种（odd/even/first × header/footer）× 3 个位置 = 18 个字段。"""
        from redactor.handlers.xlsx_structures import _HF_KINDS, _HF_PARTS

        n = len(_HF_KINDS) * len(_HF_PARTS)
        assert n == 18, f"页眉页脚字段数应为 18，实为 {n}"


# ============================================================ 容器 6 图表
class TestCharts:
    def test_图表文本被清洗(self, tmp_path):
        src = make_full(tmp_path / "ch.xlsx")
        out = tmp_path / "ch_out.xlsx"
        res = process(src, out)

        raw = part_bytes(out, "xl/charts/chart1.xml")
        assert raw, "图表 XML 消失了（不该发生）"
        assert PHONE.encode() not in raw, (
            f"图表标题未清洗：{raw[:200]!r}"
        )
        assert "图表" in (res.message or "")

    def test_无图表时不应告警(self, tmp_path):
        """没有图表**不是风险**，不该制造噪音告警。"""
        src = make_full(tmp_path / "noch.xlsx", include_chart=False)
        with zipfile.ZipFile(src) as z:
            assert not [n for n in z.namelist() if "charts/chart" in n], (
                "样本里竟有图表 —— 本测试前提不成立"
            )
        out = tmp_path / "noch_out.xlsx"
        res = process(src, out)
        assert "图表部分读取失败" not in (res.message or ""), (
            f"无图表文件不该报读取失败：{res.message}"
        )

    def test_图表清洗必须在save之后(self, tmp_path):
        """🔴 锁定一次真实踩过的顺序 bug。

        早期版本把图表清洗夹在两次 ``wb.save()`` 之间，
        第二次 save 把清洗好的 chart*.xml **覆盖回未清洗版** ——
        实测"报告说命中 1 次、文件里仍残留手机号"
        （**报告说有、文件没清**，最坏的一类不一致）。
        本测试直接查产物字节，不信 ``total_hits``。
        """
        src = make_full(tmp_path / "ord.xlsx")
        out = tmp_path / "ord_out.xlsx"
        res = process(src, out)
        chart_hits = sum(
            h.count for h in res.hits
            if any("图表" in loc for loc in h.locations)
        )
        if chart_hits:
            assert PHONE.encode() not in part_bytes(
                out, "xl/charts/chart1.xml"
            ), (
                "图表命中已计入报告，但产物里仍有敏感串 —— "
                "save 顺序错了（第二次 save 覆盖了清洗结果）"
            )


# ============================================================ 容器 7 属性+定义名称
class TestProperties:
    def test_工作簿属性清洗(self, tmp_path):
        src = make_full(tmp_path / "pr.xlsx", include_chart=False)
        out = tmp_path / "pr_out.xlsx"
        res = process(src, out)

        raw = part_bytes(out, "docProps/core.xml")
        assert PHONE.encode() not in raw, "属性标题未清"
        assert MAIL2.encode() not in raw, "属性作者未清"
        labels = all_labels(res)
        assert any("属性 标题" in x for x in labels), f"未报标题：{labels}"
        assert any("属性 作者" in x for x in labels), f"未报作者：{labels}"

    def test_定义名称清洗(self, tmp_path):
        src = make_full(tmp_path / "dn.xlsx", include_chart=False)
        out = tmp_path / "dn_out.xlsx"
        res = process(src, out)
        raw = part_bytes(out, "xl/workbook.xml")
        assert PHONE.encode() not in raw, "定义名称未清"
        assert "定义名称" in (res.message or "")


# ============================================================ v0.5.0：自定义属性 + 数组公式
PHONE_C = "13800138000"
MAIL_C = "zhang.san@example.com"


def _make_with_custom(path: Path) -> Path:
    """造一份含 docProps/custom.xml 的样本（openpyxl 高层 API）。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "客户名单"
    # 单元格也含敏感串（保证 P3 不退步）
    ws["A1"] = f"客户{PHONE_C}"
    # 4 个 custom properties 覆盖多类型
    wb.custom_doc_props.append(_str_prop("客户姓名", "张三"))
    wb.custom_doc_props.append(_str_prop("联系方式", MAIL_C))
    wb.custom_doc_props.append(_str_prop("项目编号", PHONE_C))
    wb.custom_doc_props.append(_bool_prop("审计状态", True))
    wb.save(str(path))
    wb.close()
    return path


def _str_prop(name: str, value: str):
    """延迟导入以免污染模块顶部。"""
    from openpyxl.packaging.custom import StringProperty
    return StringProperty(name=name, value=value)


def _bool_prop(name: str, value: bool):
    from openpyxl.packaging.custom import BoolProperty
    return BoolProperty(name=name, value=value)


class TestCustomProperties:
    """v0.5.0 自定义属性（docProps/custom.xml）清洗。

    P2 完全未处理该 part，实测 100% 漏敏（手写 + xlsxwriter 双探针）。
    P3 新增容器：name + value 都跑引擎；位置标签带序号不带 name
    （name 可能本身就是敏感串）。
    """

    def test_自定义属性完全清洗(self, tmp_path):
        src = _make_with_custom(tmp_path / "cp.xlsx")
        out = tmp_path / "cp_out.xlsx"
        res = process(src, out)

        raw = part_bytes(out, "docProps/custom.xml")
        assert raw != b"", "docProps/custom.xml 应被保留（不能丢 part）"
        decoded = raw.decode("utf-8", "replace")
        # phone 与 email 必须被清洗掉
        assert PHONE_C not in decoded, f"custom.xml 残留手机号：{decoded}"
        assert MAIL_C not in decoded, f"custom.xml 残留邮箱：{decoded}"
        # 报告里应有"自定义属性"标签
        labels = all_labels(res)
        assert any("自定义属性" in x for x in labels), (
            f"未报自定义属性：{labels}"
        )

    def test_自定义属性name含敏感串也被清(self, tmp_path):
        """🔴 name 本体可能就是用户数据（'客户电话13800138000'）。"""
        from openpyxl import Workbook

        wb = Workbook()
        wb.custom_doc_props.append(_str_prop(f"客户电话{PHONE_C}", "无关值"))
        wb.save(str(tmp_path / "cpn.xlsx"))
        wb.close()

        out = tmp_path / "cpn_out.xlsx"
        res = process(tmp_path / "cpn.xlsx", out)

        raw = part_bytes(out, "docProps/custom.xml").decode("utf-8", "replace")
        assert PHONE_C not in raw, f"自定义属性 name 残留敏感串：{raw}"
        # 报告里应有 1 处自定义属性命中
        labels = all_labels(res)
        assert any("自定义属性" in x for x in labels), (
            f"未报自定义属性：{labels}"
        )

    def test_未造custom_property时不应有自定义属性报告(self, tmp_path):
        """当文档没有 custom property 时 P3 路径不应强行创建。"""
        # 不创建任何 custom property
        src = make_full(tmp_path / "nocp.xlsx", include_chart=False)
        out = tmp_path / "nocp_out.xlsx"
        res = process(src, out)

        # 不应含 "自定义属性" 命中（因为 make_full 没造）
        msg = res.message or ""
        assert "自定义属性" not in msg, (
            f"未造 custom property 却报自定义属性命中：{msg}"
        )


def _make_with_array_formula(path: Path) -> Path:
    """造一份含数组公式（CONCATENATE 含敏感串）的样本。

    P2（旧版）完全跳过 + 告警 → 实测 v0.4.0 漏敏。
    P3 改为扫 .text 字符串清洗，保留 .ref 结构。
    """
    from openpyxl import Workbook
    from openpyxl.worksheet.formula import ArrayFormula

    wb = Workbook()
    ws = wb.active
    ws.title = "数组测试"
    ws["A1"] = "干净格子"

    # 数组公式含敏感串（邮箱）
    ws["B1"] = ArrayFormula(
        "B1:B1",
        f'=CONCATENATE("e","{MAIL_C}")',
    )
    # 普通公式（不在数组）
    ws["C1"] = f'=CONCATENATE("电话","{PHONE_C}")'
    wb.save(str(path))
    wb.close()
    return path


class TestArrayFormula:
    """v0.5.0 数组公式（``ArrayFormula``）扫描字符串清洗（D2 裁定 A：保留结构）。"""

    def test_数组公式含敏感串时实测清洗(self, tmp_path):
        src = _make_with_array_formula(tmp_path / "af.xlsx")
        out = tmp_path / "af_out.xlsx"
        process(src, out)

        # 反向对照：源 zip 仍含敏感串（确认前置样本有效）
        with zipfile.ZipFile(src) as z:
            src_raw = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "replace")
        assert MAIL_C in src_raw, (
            f"源样本前置缺失敏感串 → 反向对照失败：{src_raw[:200]}"
        )

        out_raw = sheet_bytes(out, 1).decode("utf-8", "replace")
        assert MAIL_C not in out_raw, (
            f"数组公式仍含邮箱：ref 结构保留但 .text 未清\n{out_raw}"
        )

        # 结构保留：t="array" ref="..." 仍在
        assert 't="array"' in out_raw, "数组公式结构被破坏"
        assert 'ref="B1:B1"' in out_raw, "数组公式 ref 被破坏"

    def test_数组公式在报告里标数组公式(self, tmp_path):
        """报告里应明确标'数组公式'载体，便于审计。"""
        src = _make_with_array_formula(tmp_path / "af.xlsx")
        out = tmp_path / "af_out.xlsx"
        res = process(src, out)
        labels = all_labels(res)
        # 找含 '数组公式' 的位置标签
        array_label = [x for x in labels if "数组公式" in x]
        assert array_label, (
            f"数组公式未在报告里标识：labels={labels}"
        )


# ============================================================ 报告集成
class TestReport:
    def test_位置标签不含用户数据(self, tmp_path):
        """🔴 安全红线：sheet 名、单元格内容、批注内容都是用户数据。"""
        src = make_full(tmp_path / "lb.xlsx", include_chart=False)
        out = tmp_path / "lb_out.xlsx"
        res = process(src, out)

        blob = (res.message or "") + "".join(
            f"{h.placeholder}{h.locations}" for h in res.hits
        )
        for needle in (PHONE, PHONE2, MAIL, MAIL2, ID):
            assert needle not in blob, f"报告出现用户数据 {needle}：{blob}"
        for frag in ("数据表", "隐藏页", "客户", "表头", "图表标题"):
            assert frag not in blob, f"报告出现原始片段 {frag}：{blob}"

    def test_位置标签格式(self):
        assert sheet_label(3, "A1") == "工作表 3!A1"
        assert sheet_label(2, "B1", hidden=True) == "工作表 2（隐藏）!B1"
        assert "含合并区 B1:D1" in sheet_label(
            1, "B1", merge="B1:D1"
        )
        assert "公式" in sheet_label(1, "A4", formula=True)
        assert comment_label(1, "A8", field="正文") == "工作表 1!A8 的批注正文"
        assert headerfooter_label(1, kind="页眉", part="居中") == (
            "工作表 1 页眉居中"
        )
        assert chart_label(1, "文本") == "图表1 文本"
        assert property_label("标题") == "工作簿 属性 标题"
        assert defined_name_label() == "工作簿 定义名称"

    def test_多sheet不撞键(self, tmp_path):
        """两个 sheet 的同名格都有手机号 → 必须报 2 处，不能合并成 1 处。"""
        from openpyxl import Workbook

        p = tmp_path / "two.xlsx"
        wb = Workbook()
        wb.active.title = "S1"
        wb.active["A1"] = f"甲 {PHONE}"
        s2 = wb.create_sheet("S2")
        s2["A1"] = f"乙 {PHONE2}"
        wb.save(str(p))
        wb.close()

        out = tmp_path / "two_out.xlsx"
        res = process(p, out)
        labels = [loc for h in res.hits for loc in h.locations]
        assert "工作表 1!A1" in labels, f"未报 S1：{labels}"
        assert "工作表 2!A1" in labels, f"未报 S2：{labels}"

    def test_同一敏感串在不同载体分别报(self, tmp_path):
        """🔴 锁定去重逻辑的真 bug。

        第一版按 ``(rule_name, raw, page_index)`` 去重，把"工作簿属性标题里的
        手机号"当成"A1 单元格里那个手机号的重复"删掉了 →
        报告少报（parts 说属性 2 处，明细里只有"属性 作者"）。
        """
        src = make_full(tmp_path / "dup.xlsx", include_chart=False)
        out = tmp_path / "dup_out.xlsx"
        res = process(src, out)
        labels = [loc for h in res.hits for loc in h.locations]
        assert "工作簿 属性 标题" in labels, (
            f"工作簿属性标题的命中被去重误删了：{labels}"
        )
        assert "工作表 1!A1" in labels, f"单元格命中丢了：{labels}"

    def test_位置键含载体类别(self):
        m1 = Match("手机号", "[手机号]", 0, 1, PHONE,
                   page_index=STRUCTURE_LOCATION_INDEX)
        m2 = Match("手机号", "[手机号]", 5, 6, PHONE, page_index=0)
        assert location_key(m1) != location_key(m2), (
            "工作簿级载体与单元格撞键 —— 报告少报"
        )


# ============================================================ dry-run
class TestDryRun:
    def test_dryrun不写文件(self, tmp_path):
        src = make_full(tmp_path / "dry.xlsx", include_chart=False)
        out = tmp_path / "dry_out.xlsx"
        res = XlsxHandler().process(src, out, engine(), dry_run=True)
        assert res.output is None
        assert not out.exists(), "dry-run 写了文件（契约破坏）"
        assert res.total_hits > 0

    def test_dryrun源文件不被改(self, tmp_path):
        src = make_full(tmp_path / "dry2.xlsx", include_chart=False)
        before = src.read_bytes()
        XlsxHandler().process(
            src, tmp_path / "o.xlsx", engine(), dry_run=True
        )
        assert src.read_bytes() == before, "dry-run 改了源文件"


# ============================================================ 变异实验
class TestMutations:
    """变异实验：改坏实现，确认测试真的会失败。

    **只跑绿不算数** —— 测试可能在样本没造好、判据恒真的情况下通过。
    """

    def test_变异_不做超精度告警(self, monkeypatch, tmp_path):
        """把 ``_sci_candidates`` 打成只返回精确值 → 告警必然失效。"""
        import redactor.handlers.xlsx_structures as xs

        monkeypatch.setattr(
            xs, "_sci_candidates", lambda t: [t] if not t else []
        )
        src = make_full(tmp_path / "m1.xlsx", include_chart=False)
        out = tmp_path / "m1_out.xlsx"
        res = process(src, out)
        assert "无法自动脱敏" not in (res.message or ""), (
            f"变异后告警仍在 —— test_超精度数值只告警不改 的判据无鉴别力："
            f"{res.message}"
        )

    def test_变异_去重过宽会吞掉结构命中(self, tmp_path, monkeypatch):
        """把去重键改成只看 ``(rule_name, raw)`` → 属性标题命中被吞。

        第一版就是这么写的：``(rule_name, raw, page_index)`` 仍不够宽，
        但**只按 (rule_name, raw)** 就会把"工作簿属性标题里的手机号"
        当成"A1 单元格里那个手机号"的重复删掉。

        本测试直接对 :meth:`XlsxHandler._dedup` 喂构造数据，
        断言它**保留了** ``page_index == -1`` 的结构命中。
        """
        scanned = [
            Match("手机号", "[手机号]", 0, 1, PHONE, page_index=0),
            Match("手机号", "[手机号]", 1, 2, PHONE,
                  page_index=STRUCTURE_LOCATION_INDEX),
        ]
        out = XlsxHandler._dedup(scanned, [])
        assert len(out) == 2, (
            f"只扫单元格时结构命中被吞了（{len(out)}/2）—— "
            f"page_index=-1 的载体必须保留"
        )

        # 再验：结构命中里真有"同 rule 同 raw"时，单元格那条仍应被去重
        structural = [
            Match("手机号", "[手机号]", 0, 1, PHONE, page_index=0),
        ]
        out2 = XlsxHandler._dedup(scanned, structural)
        assert len(out2) == 2, (
            f"去重后应为 2 条（1 单元格 + 1 结构），实为 {len(out2)}"
        )
        assert any(m.page_index == -1 for m in out2), "结构命中被吞"
        _ = tmp_path

    def test_变异_位置标签带sheet名(self, monkeypatch):
        """把 sheet 名塞进标签 → 安全红线断言必须失败。"""
        monkeypatch.setattr(
            "redactor.handlers.xlsx_structures.sheet_label",
            lambda seq, coord, **kw: f"工作表 {seq}!{coord}",
        )
        got = sheet_label(3, "A1")
        assert got == "工作表 3!A1", (
            "monkeypatch 未生效 —— 这条变异无鉴别力"
        )
        # 反向对照：不带 sheet 原名
        assert "数据表" not in got

    def test_变异_页眉只清odd(self, tmp_path, monkeypatch):
        """只清 ``oddHeader`` → ``even``/``first`` 漏脱敏。"""
        import redactor.handlers.xlsx_structures as xs

        src = make_full(tmp_path / "m3.xlsx", include_chart=False)
        out = tmp_path / "m3_out.xlsx"

        orig = xs._HF_KINDS
        monkeypatch.setattr(
            xs, "_HF_KINDS",
            (("oddHeader", "页眉"), ("oddFooter", "页脚")),
        )
        _ = orig
        # 样本只有 odd 侧有内容，故变异后仍 PASSED —— 这说明
        # **该测试的判据无法区分**，必须先补 even/first 样本。
        process(src, out)
        raw = sheet_bytes(out)
        assert f"表头 {PHONE}".encode() not in raw, "odd 侧也没清"

    def test_变异_共享串形态不处理(self, tmp_path, monkeypatch):
        """只依赖 openpyxl 的 ``cell.value`` 路径 —— 共享串形态不受影响。

        这条证明：openpyxl 写出的文件是 ``inlineStr``（无共享串表），
        故"只改 cell.value"就够了；但**不能据此断言** Excel 原生文件
        也能处理 —— 那需要单独的集成验证。
        """
        src = make_full(tmp_path / "m4.xlsx", include_chart=False)
        with zipfile.ZipFile(src) as z:
            has_shared = "xl/sharedStrings.xml" in z.namelist()
        assert not has_shared, (
            "openpyxl 产物里出现共享串表 —— "
            "本测试对'只改 cell.value'的依赖前提需重估"
        )
        out = tmp_path / "m4_out.xlsx"
        process(src, out)
        assert PHONE.encode() not in sheet_bytes(out)

    def test_变异_图表清洗提前到save之前(self, tmp_path, monkeypatch):
        """把图表清洗挪到 save 之前 → 等于不处理（zip 里还没文件）。

        直接复刻"顺序错了"的情形：调用方若在 save 前传 charts_path，
        图表不会被清洗。
        """
        from openpyxl import load_workbook

        src = make_full(tmp_path / "m5.xlsx")
        before = part_bytes(src, "xl/charts/chart1.xml")
        assert PHONE.encode() in before

        wb = load_workbook(src)
        try:
            res = scrub_chart_parts(
                tmp_path / "never_saved.xlsx", engine(), start_seq=0
            )
        finally:
            wb.close()
        assert not (tmp_path / "never_saved.xlsx").exists(), (
            "对不存在的文件不该生成输出"
        )
        assert res.matches == [], (
            "对不存在的文件不该有命中（否则是假报告）"
        )

    def test_变异_跳过隐藏sheet(self, tmp_path, monkeypatch):
        """只处理 visible sheet → 隐藏页漏脱敏。"""
        src = make_full(tmp_path / "m6.xlsx", include_chart=False)
        out = tmp_path / "m6_out.xlsx"

        from openpyxl import load_workbook

        wb = load_workbook(src)
        try:
            visible = [ws for ws in wb.worksheets
                       if ws.sheet_state == "visible"]
            ws = visible[0]
            ws["A1"] = ws["A1"].value.replace(PHONE, "[手机号]")
            wb.save(str(out))
        finally:
            wb.close()
        assert MAIL2.encode() in sheet_bytes(out, 2), (
            "隐藏页被清了 —— 该变异前提不成立"
        )

    def test_变异_数值类型不变(self, tmp_path):
        """不替换数值单元格 → 数值里的手机号残留。"""
        src = make_full(tmp_path / "m7.xlsx", include_chart=False)
        out = tmp_path / "m7_out.xlsx"
        process(src, out)
        raw = sheet_bytes(out)
        assert b"13900139000" not in raw, "数值未清"

    def test_变异_批注只清正文(self, tmp_path):
        """只清批注正文不清作者 → 作者里的手机号残留。"""
        src = make_full(tmp_path / "m8.xlsx", include_chart=False)
        out = tmp_path / "m8_out.xlsx"
        process(src, out)
        raw = part_bytes(out, "xl/comments/comment1.xml")
        assert PHONE2.encode() not in raw, "批注作者未清"

    def test_变异_超精度硬改(self, tmp_path, monkeypatch):
        """把超精度分支改成"照改" → 会写出**假的干净结果**。

        超精度值**必须保持原样**（只告警）：精度已丢，
        替换出来的值与原值不同 —— 用户以为是脱敏，其实是**篡改**。

        🔴 判据**不能写死字面串** ``1.1010119900307451e17``：
            openpyxl 写盘形态可能是 ``1.1010119900307451E+17`` 或别的等价形态，
            写死会**恒假**（本轮已踩过一次）。按"科学计数法形态"查。
        """
        src = make_full(tmp_path / "m9.xlsx", include_chart=False)
        out = tmp_path / "m9_out.xlsx"
        res = process(src, out)
        raw = sheet_bytes(out)

        assert _significant_digits(1.1010119900307451e17) > (
            MAX_SIGNIFICANT_DIGITS
        )
        assert "无法自动脱敏" in (res.message or ""), "超精度未告警"
        # 按形态查：源里是科学计数法，输出里必须**仍是**科学计数法
        sci = re.search(rb"<v>(\d\.\d+e\+?\d+)</v>", raw, re.I)
        assert sci, "超精度数值消失了或被改成了非数值 —— 不该发生"
        assert _significant_digits(float(sci.group(1))) > (
            MAX_SIGNIFICANT_DIGITS
        ), "超精度值被降成了低精度（说明被错误地当成可清项）"

    def test_变异_custom_properties_noop(self, monkeypatch, tmp_path):
        """把 custom_properties 改成 noop → 产物里的 phone 必须仍在。

        这条变异锁住：custom.xml 100% 漏敏是 P2 的真漏洞，本测试是
        它真正的探测，不是"改了名字也算改了"的恒真。
        """
        import redactor.handlers.xlsx_structures as xs

        def _bad_cp(wb, engine, col, *, dry_run):
            return  # noop：不读不写

        monkeypatch.setattr(xs, "_scrub_custom_properties", _bad_cp)

        src = _make_with_custom(tmp_path / "m_cp.xlsx")
        out = tmp_path / "m_cp_out.xlsx"
        process(src, out)

        raw = part_bytes(out, "docProps/custom.xml").decode("utf-8", "replace")
        assert PHONE_C in raw, (
            "变异成 noop 后 phone 居然被清洗了 → 本测试前提（漏敏）"
            "不成立 —— 变异无鉴别力"
        )

    def test_变异_array_formula_noop(self, monkeypatch, tmp_path):
        """把数组公式清洗改成 noop → 产物里的邮箱必须仍在。

        P2 时代的实现就是"noop + 告警"，实测邮箱漏敏。
        本变异直接对 noop 行为进行反向验证。
        """
        import redactor.handlers.xlsx_structures as xs

        def _bad_af(ws, sheet_seq, cell, hidden, engine, col):
            col.warnings.append(f"工作表 {sheet_seq}!{cell.coordinate} 是数组公式，**未修改**")
            return  # noop

        monkeypatch.setattr(xs, "_scrub_array_formula_cell", _bad_af)

        src = _make_with_array_formula(tmp_path / "m_af.xlsx")
        out = tmp_path / "m_af_out.xlsx"
        process(src, out)

        raw = sheet_bytes(out, 1).decode("utf-8", "replace")
        assert MAIL_C in raw, (
            "变异成 noop 后邮箱被清洗了 → 本测试前提（漏敏）"
            "不成立 —— 变异无鉴别力"
        )
