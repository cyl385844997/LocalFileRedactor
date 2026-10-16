"""P3 元数据清洗（``/Info`` + XMP）的专项测试。

**本文件的取证原则**（沿用仓库既有铁律）：

1. 每条"已清洗"的断言，都配一条**反向对照** —— 证明"清洗失效时确实
   能观察到残留"。否则断言可能恒真（例如"输出里没有手机号"在
   样本本身就没写手机号时也成立）。
2. 断言对象是**产物**（``fitz.metadata`` / ``get_xml_metadata()`` 的实际
   读回值），不是生成条件。
3. "结构合法"一律用**真解析器**（``ET.fromstring``）判断，不用
   ``len(xml) > 0`` 这类重言式（fitz 不做 XML 解析，那个判据恒真）。

覆盖：
    - ``/Info`` 六字段清洗 + 位置标签格式
    - XMP 明文 / **实体编码**（``&#56;``）清洗
    - 跨节点敏感串 -> 告警（不静默漏掉）
    - 清洗后 XML 非法 -> 回退 + 告警
    - 属性值引号转义
    - ``page_index = -1`` 与正文命中不撞键
    - ``aggregate`` 的 strict 守卫不因元数据键报错
    - dry-run 不改源文件
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import fitz
import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rules, load_rules_from_text
from redactor.handlers.pdf_handler import PdfHandler, location_key
from redactor.handlers.pdf_metadata import (
    INFO_FIELDS,
    METADATA_PAGE_INDEX,
    XMP_LABEL,
    info_label,
    scrub_info_metadata,
    scrub_metadata,
    scrub_xmp_metadata,
)

BUILTIN = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"

PHONE = "13800138000"
#: ``&#56;`` 是 ``8``。用它把手机号切开，字节层面就不连续了 ——
#: 字符串替换类做法会漏（见 pdf_metadata 的模块 docstring 结论 2）。
PHONE_ENTITY = "1380013&#56;000"

_XP_HEAD = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
)
_XP_TAIL = "</rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"


def engine(rules=None) -> RedactionEngine:
    """默认用内置规则；传入规则列表可换成自定义占位符。"""
    return RedactionEngine(rules if rules is not None else load_rules(BUILTIN))


def xmp_with_title(text: str, *, attr: bool = False) -> str:
    """造一段带 ``dc:title`` 的 XMP。

    Args:
        text: 要放进 title 的内容。
        attr: True 时放进**属性值**（``rdf:about``），否则放文本节点。
            属性值这条路径必须单独测 —— 正则与转义规则都不同。
    """
    if attr:
        desc = (
            '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"'
            f' rdf:about="{text}">'
            "<dc:title><rdf:Alt>"
            '<rdf:li xml:lang="x-default">标题</rdf:li>'
            "</rdf:Alt></dc:title></rdf:Description>"
        )
    else:
        desc = (
            '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title><rdf:Alt>"
            f'<rdf:li xml:lang="x-default">{text}</rdf:li>'
            "</rdf:Alt></dc:title></rdf:Description>"
        )
    return _XP_HEAD + desc + _XP_TAIL


def make_pdf(
    path: Path,
    *,
    body: str = "本文件正文不含敏感信息",
    meta: dict | None = None,
    xmp: str | None = None,
) -> Path:
    """造一份 PDF 样本。``body`` 写在正文，``meta`` 写 /Info，``xmp`` 写 XMP。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    if body:
        page.insert_text((60, 130), body, fontsize=12, fontname="helv")
    if meta:
        doc.set_metadata(meta)
    if xmp is not None:
        doc.set_xml_metadata(xmp)
    doc.save(str(path))
    doc.close()
    return path


def xml_parsable(text: str) -> bool:
    """真跑 XML 解析器。**不要**换成 ``len(text) > 0``。"""
    body = text
    if body.startswith("<?xpacket"):
        i = body.find("?>")
        if i >= 0:
            body = body[i + 2:]
    j = body.rfind("<?xpacket")
    if j >= 0:
        body = body[:j]
    try:
        ET.fromstring(body)
        return True
    except ET.ParseError:
        return False


# ============================================================ /Info 六字段
class TestInfoMetadata:
    """``/Info`` 字典的清洗。"""

    def test_六字段全部清洗_且反向对照证明清洗前确实泄露(self, tmp_path):
        """先证明"不清洗就泄露"，再证明"清洗后不泄露"。

        只断言后者是**空转**：样本若根本没写手机号，它照样通过。
        故这里在同一个样本上取两次证：

            清洗前 -> 6 个字段全部含手机号（证明泄露真实存在）
            清洗后 -> 一个都不含
        """
        src = make_pdf(
            tmp_path / "info.pdf",
            meta={py: f"字段 {PHONE}" for py, _ in INFO_FIELDS},
        )

        # --- 对照：清洗之前，泄露必须能被观察到 ---
        before = fitz.open(str(src)).metadata
        leaked_before = [k for k, _ in INFO_FIELDS if PHONE in (before.get(k) or "")]
        assert len(leaked_before) == len(INFO_FIELDS), (
            f"样本没造好：清洗前只有 {leaked_before} 含手机号，"
            f"预期全部 {len(INFO_FIELDS)} 个 —— 反向对照失效，"
            f"后面的'清洗后不含'就没有鉴别力"
        )

        # --- 正向：走一遍处理 ---
        out = tmp_path / "info_out.pdf"
        PdfHandler().process(src, out, engine())

        after = fitz.open(str(out)).metadata
        leaked_after = {
            k: v for k, v in after.items() if v and PHONE in v
        }
        assert not leaked_after, f"清洗后仍有字段泄露：{leaked_after}"

    def test_只改含敏感串的字段_其余原样保留(self, tmp_path):
        """未命中的字段不该被动过。

        为什么值得测：``set_metadata`` 是**合并**语义（实测），
        逐字段调用时若误传空串，会把用户原有的元数据抹掉。
        """
        src = make_pdf(
            tmp_path / "keep.pdf",
            meta={
                "title": f"报告 {PHONE}",
                "author": "张三",
                "subject": "年度总结",
            },
        )
        out = tmp_path / "keep_out.pdf"
        PdfHandler().process(src, out, engine())

        after = fitz.open(str(out)).metadata
        assert PHONE not in (after.get("title") or "")
        assert after.get("author") == "张三", (
            f"未命中字段被改动了：author={after.get('author')!r}"
        )
        assert after.get("subject") == "年度总结", (
            f"未命中字段被改动了：subject={after.get('subject')!r}"
        )

    def test_位置标签格式为_元数据_斜杠_原始键名(self, tmp_path):
        """标签必须是 ``元数据 /Title`` 这种形式。

        用**原始键名**（``/Title``）而不是中文译名：用户在 Acrobat 里看到
        "标题"、在 PDF 源码里看到 ``/Title``，只有原始键名两边都对得上。
        """
        src = make_pdf(
            tmp_path / "label.pdf",
            meta={"title": f"报告 {PHONE}", "author": f"张三 {PHONE}"},
        )
        out = tmp_path / "label_out.pdf"
        res = PdfHandler().process(src, out, engine())

        labels = [loc for h in res.hits for loc in h.locations]
        assert any(l.startswith("元数据 /Title") for l in labels), (
            f"缺少 `元数据 /Title` 标签，实际：{labels}"
        )
        assert any(l.startswith("元数据 /Author") for l in labels), (
            f"缺少 `元数据 /Author` 标签，实际：{labels}"
        )
        # 不能是中文译名或 py 键名
        assert not any("元数据 标题" in l for l in labels)
        assert not any("元数据 title" in l for l in labels)

    def test_info_label_用原始键名而非py键名(self):
        """``info_label`` 的输入契约：收 PDF 原始键名。"""
        assert info_label("/Title") == "元数据 /Title"
        assert info_label("/Producer") == "元数据 /Producer"

    def test_元数据命中的page_index为负一(self, tmp_path):
        """``page_index = -1`` 是"非页面"的约定（用户裁定 C1）。"""
        src = make_pdf(tmp_path / "pi.pdf", meta={"title": f"报告 {PHONE}"})
        doc = fitz.open(str(src))
        try:
            result = scrub_metadata(doc, engine())
        finally:
            doc.close()
        assert result.matches, "样本应至少命中 1 处"
        assert all(
            m.page_index == METADATA_PAGE_INDEX for m in result.matches
        ), [m.page_index for m in result.matches]

    def test_正文无敏感串时元数据命中数即为总命中数(self, tmp_path):
        """正文干净、元数据脏 —— 报告的命中数应全部来自元数据。

        这条同时是"元数据命中确实进了报告"的证据：若漏接，``hits`` 会是空的。
        """
        src = make_pdf(tmp_path / "only_meta.pdf", meta={"title": f"报告 {PHONE}"})
        out = tmp_path / "only_meta_out.pdf"
        res = PdfHandler().process(src, out, engine())

        total = sum(h.count for h in res.hits)
        assert total == 1, f"预期 1 处（全部来自元数据），实际 {total}：{res.hits}"
        assert "1 处" in res.message


# ============================================================ XMP
class TestXmpMetadata:
    """XMP 包的清洗（方案 D：保骨架换文本）。"""

    def test_XMP明文手机号被清洗_且骨架保留(self, tmp_path):
        """清洗后：手机号没了，但命名空间前缀与 xpacket 必须原样。

        为什么强调骨架：方案 B（``ET.tostring`` 重建）会把 ``dc:`` 改写成
        ``ns0:``，阅读器可能认不出。本测试守住这个取舍不被改回去。
        """
        src = make_pdf(tmp_path / "xmp.pdf", xmp=xmp_with_title(f"报告 {PHONE}"))

        # 对照：清洗前必须能读到
        assert PHONE in fitz.open(str(src)).get_xml_metadata()

        out = tmp_path / "xmp_out.pdf"
        PdfHandler().process(src, out, engine())

        after = fitz.open(str(out)).get_xml_metadata()
        assert PHONE not in after, "XMP 里的手机号未被清洗"
        assert "dc:" in after, "命名空间前缀 dc: 丢失（方案 B 的坑）"
        assert "ns0:" not in after, "出现 ns0: 前缀 —— 说明用了 ET 重建"
        assert "<?xpacket" in after, "xpacket 包裹层丢失"
        assert xml_parsable(after), "清洗后 XML 不合法"

    def test_XMP实体编码手机号被清洗(self, tmp_path):
        """``1380013&#56;000`` 也必须清掉 —— 这是"方案 D 优于字符串替换"的核心。

        字符串替换在字节层面看不到 ``&#56;``，会**漏掉**（实测）。
        方案 D 先 ``html.unescape`` 还原成 ``8``，正则才看得见。
        """
        src = make_pdf(
            tmp_path / "xmp_ent.pdf", xmp=xmp_with_title(PHONE_ENTITY)
        )
        # 对照：样本里确实是以实体形态存的，明文手机号不存在
        raw = fitz.open(str(src)).get_xml_metadata()
        assert PHONE_ENTITY in raw, "样本没写成实体编码形态"
        assert PHONE not in raw, "样本里不该有明文手机号"

        out = tmp_path / "xmp_ent_out.pdf"
        res = PdfHandler().process(src, out, engine())

        after = fitz.open(str(out)).get_xml_metadata()
        assert PHONE not in after, "实体编码的手机号未被还原后清洗"
        assert PHONE_ENTITY not in after, "原实体形态仍在"
        assert sum(h.count for h in res.hits) == 1, (
            f"应报 1 处命中，实际 {res.hits}"
        )

    def test_XMP属性值里的手机号也被清洗(self, tmp_path):
        """属性值与文本节点是**两条不同代码路径**，必须分别覆盖。"""
        src = make_pdf(
            tmp_path / "xmp_attr.pdf", xmp=xmp_with_title(PHONE, attr=True)
        )
        assert PHONE in fitz.open(str(src)).get_xml_metadata()

        out = tmp_path / "xmp_attr_out.pdf"
        PdfHandler().process(src, out, engine())

        after = fitz.open(str(out)).get_xml_metadata()
        assert PHONE not in after, "属性值里的手机号未被清洗"
        assert xml_parsable(after), "属性值清洗后 XML 不合法"

    def test_位置标签为_元数据_XMP(self, tmp_path):
        src = make_pdf(tmp_path / "xmp_label.pdf", xmp=xmp_with_title(f"报告 {PHONE}"))
        out = tmp_path / "xmp_label_out.pdf"
        res = PdfHandler().process(src, out, engine())

        labels = [loc for h in res.hits for loc in h.locations]
        assert any(l.startswith(XMP_LABEL) for l in labels), (
            f"缺少 `{XMP_LABEL}` 标签，实际：{labels}"
        )

    def test_无XMP的文档不报错不告警(self, tmp_path):
        """绝大多数 PDF 没有 XMP —— 不能因此报错或产生噪声告警。"""
        src = make_pdf(tmp_path / "noxmp.pdf", meta={"title": f"报告 {PHONE}"})
        out = tmp_path / "noxmp_out.pdf"
        res = PdfHandler().process(src, out, engine())

        assert "⚠️" not in res.message, f"无 XMP 却出现告警：{res.message}"
        assert res.status == "ok"

    def test_清洗后XML非法时必须回退原文并告警(self, tmp_path, monkeypatch):
        """校验兜底：若清洗产出了非法 XML，**宁可不改**也要告警。

        为什么要注入而不是造真实样本：
            正常路径下 ``_escape_xml`` 已经把 ``&``/``<``/引号转义掉了，
            **真实样本无法再触发**这个分支（这正是加固生效的证据，
            见下一条对照用例）。故这里注入一个"不转义"的实现，
            专门把这条兜底路径逼出来。

        注意：本测试只证明"校验失败时会回退"，不证明"一定会失败" ——
        后者由 :meth:`test_不转义时确实会破坏XML_证明兜底有必要` 负责。
        """
        import redactor.handlers.pdf_metadata as mod

        monkeypatch.setattr(mod, "_escape_xml", lambda text, quote="": text)

        rules = load_rules_from_text(
            "version: 1\n"
            "rules:\n"
            "  - name: 手机号\n"
            "    pattern: '(?<!\\d)1[3-9]\\d{9}(?!\\d)'\n"
            "    placeholder: 'A&B'\n",
            path=Path("<t>"),
        )
        src = make_pdf(
            tmp_path / "broken.pdf", xmp=xmp_with_title(f"报告 {PHONE}")
        )
        doc = fitz.open(str(src))
        try:
            labeled, warnings, touched = scrub_xmp_metadata(doc, engine(rules))
            after = doc.get_xml_metadata()
        finally:
            doc.close()

        assert not labeled, "回退时不应报命中（未真正清洗）"
        assert warnings, "XML 非法却无告警 —— 会静默漏掉残留"
        assert "未清洗" in warnings[0], f"告警文案未说明未清洗：{warnings[0]}"
        assert PHONE in after, (
            "回退后原文应保持不动（宁可不清洗，也不产出非法 XMP）"
        )
        assert xml_parsable(after), "回退后的原文本身应是合法 XML"

    def test_不转义时确实会破坏XML_证明兜底有必要(self, tmp_path):
        """**反例强度对照**：证明"不转义会坏"这个风险是真实的。

        若没有这条，上面那条"注入后触发回退"可能只是在测一个
        永远不会发生的分支。这里直接验证：把 ``&`` 原样塞进文本节点，
        真解析器会拒绝。
        """
        broken = xmp_with_title("A&B")
        assert not xml_parsable(broken), (
            "含裸 & 的 XMP 竟能被解析 —— 那 R2 反例的前提就不成立，"
            "兜底逻辑也就没必要存在，请重查"
        )
        # 转义后就合法
        fixed = xmp_with_title("A&amp;B")
        assert xml_parsable(fixed)

    def test_占位符含引号时属性值被正确转义(self, tmp_path):
        """属性值用双引号包裹，替换值里的 ``"`` 必须转义成 ``&quot;``。

        否则会提前闭合属性、产出非法 XML。与 ``&`` 破坏结构同源，
        只是维度不同（R2 反例的引号版）。
        """
        rules = load_rules_from_text(
            "version: 1\n"
            "rules:\n"
            "  - name: 手机号\n"
            "    pattern: '(?<!\\d)1[3-9]\\d{9}(?!\\d)'\n"
            '    placeholder: \'X"Y\'\n',
            path=Path("<t>"),
        )
        src = make_pdf(
            tmp_path / "quote.pdf", xmp=xmp_with_title(PHONE, attr=True)
        )
        out = tmp_path / "quote_out.pdf"
        PdfHandler().process(src, out, engine(rules))

        after = fitz.open(str(out)).get_xml_metadata()
        assert PHONE not in after, "属性值里的手机号未被清洗"
        assert xml_parsable(after), (
            f"占位符含引号导致 XML 非法（引号未转义）：{after!r}"
        )
        assert "&quot;" in after, "替换值里的引号应转义为 &quot;"


# ============================================================ 跨节点
class TestCrossNodeWarning:
    """跨节点敏感串必须**报告**，不能静默漏掉（用户明确要求）。"""

    def test_跨节点手机号触发人工核查告警(self, tmp_path):
        """手机号被 XML 标签拆成 ``前13800`` + ``138000后``。

        实测：任何单节点视角都不构成合法手机号，字符串替换与解析重建
        **都识别不出**。这是已知边界，但**必须让用户知道**。
        """
        cross = (
            '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title><rdf:Alt>"
            '<rdf:li xml:lang="x-default">前13800</rdf:li>'
            '<rdf:li xml:lang="x-default">138000后</rdf:li>'
            "</rdf:Alt></dc:title></rdf:Description>"
        )
        src = make_pdf(tmp_path / "cross.pdf", xmp=_XP_HEAD + cross + _XP_TAIL)
        out = tmp_path / "cross_out.pdf"
        res = PdfHandler().process(src, out, engine())

        assert "⚠️" in res.message, (
            "跨节点敏感串未被告警 —— 这正是『静默漏掉』，用户明确禁止："
            f"{res.message}"
        )
        assert "人工核查" in res.message, (
            f"告警必须给出可执行动作（人工核查）：{res.message}"
        )
        assert "跨节点" in res.message

    def test_告警不影响正常命中统计(self, tmp_path):
        """跨节点告警是附加信息，不该把已清洗的命中数算错。

        样本要同时满足两件事，否则测不到"并存"：
            ① 有一个**明文**手机号（可被清洗 -> 计入 hits）
            ② 另有一个**被标签拆开**的手机号（清洗不掉 -> 触发告警）

        早期版本只放了 ``报告 {PHONE}`` + 孤立的 ``前13800``，
        后者单看不是手机号、和前者拼接也不是 —— 本来就不该告警，
        是**样本错**而非实现错。这里补上 ``138000后``，
        与 ``前13800`` 拼起来才是完整号码。
        """
        body = (
            '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title><rdf:Alt>"
            f'<rdf:li xml:lang="x-default">报告 {PHONE}</rdf:li>'
            '<rdf:li xml:lang="x-default">前13800</rdf:li>'
            '<rdf:li xml:lang="x-default">138000后</rdf:li>'
            "</rdf:Alt></dc:title></rdf:Description>"
        )
        src = make_pdf(tmp_path / "mix.pdf", xmp=_XP_HEAD + body + _XP_TAIL)
        out = tmp_path / "mix_out.pdf"
        res = PdfHandler().process(src, out, engine())

        assert sum(h.count for h in res.hits) == 1, (
            f"应报 1 处（明文的那个），实际 {res.hits}"
        )
        assert "⚠️" in res.message, (
            f"同文件里存在跨节点串，应同时告警：{res.message}"
        )
        after = fitz.open(str(out)).get_xml_metadata()
        assert PHONE not in after, "明文手机号应已被清洗"


# ============================================================ 与正文共存
class TestCoexistWithBody:
    """元数据命中与正文命中并存时，键不能撞。"""

    def test_正文与元数据同规则命中不撞键(self, tmp_path):
        """正文一个手机号 + ``/Title`` 一个手机号 -> 必须报 **2 处**。

        键是 ``(规则名, page_index, start)``。元数据用 ``page_index=-1``，
        正文用 ``0``，理论上不撞。但元数据的 ``start`` 是**全局序号**，
        若实现改动导致两者落在同一个键上，报告就会少报 1 处 ——
        这正是 ``aggregate`` 的 strict 守卫要拦的事。
        """
        src = make_pdf(
            tmp_path / "both.pdf",
            body=f"联系电话 {PHONE}",
            meta={"title": f"报告 {PHONE}"},
        )
        out = tmp_path / "both_out.pdf"
        res = PdfHandler().process(src, out, engine())

        assert sum(h.count for h in res.hits) == 2, (
            f"正文 1 处 + 元数据 1 处 = 应报 2 处，实际 {res.hits}"
        )
        locations = [loc for h in res.hits for loc in h.locations]
        assert len(locations) == 2, (
            f"位置标签应有 2 条（撞键会只剩 1 条）：{locations}"
        )
        assert any("元数据" in l for l in locations)
        assert any("页" in l and "元数据" not in l for l in locations)

    def test_aggregate严格守卫不因元数据键报错(self, tmp_path):
        """用户裁定 B 的附加条件：混进 hits 不能把 strict 守卫惹毛。

        直接调用 ``aggregate``，传入元数据命中 + 与 handler 一致的
        ``key_of``。若不抛错，说明键函数与 locations 是配套的。
        """
        src = make_pdf(
            tmp_path / "strict.pdf", meta={"title": f"报告 {PHONE}"}
        )
        doc = fitz.open(str(src))
        try:
            result = scrub_metadata(doc, engine())
        finally:
            doc.close()

        locations = {
            location_key(m): f"{label} 第 {i} 处"
            for i, (m, label) in enumerate(
                zip(result.matches, result.labels), start=1
            )
        }
        # 不抛 ValueError 即为通过
        stats = RedactionEngine.aggregate(
            result.matches, locations, key_of=location_key
        )
        assert sum(s.count for s in stats) == len(result.matches)

    def test_键函数不一致时严格守卫确实会抛错(self, tmp_path):
        """**反例强度对照**：证明上面的"不抛错"不是因为守卫失效。

        故意用二元组键生成 locations、却传三元组 ``key_of`` ——
        守卫必须抛 ``ValueError``。若这条不失败，说明守卫形同虚设，
        上一条"不抛错"就毫无意义。
        """
        src = make_pdf(
            tmp_path / "guard.pdf", meta={"title": f"报告 {PHONE}"}
        )
        doc = fitz.open(str(src))
        try:
            result = scrub_metadata(doc, engine())
        finally:
            doc.close()

        mismatched = {
            (m.rule_name, m.start): "元数据 /Title 第 1 处"
            for m in result.matches
        }
        with pytest.raises(ValueError, match="取不到位置标签"):
            RedactionEngine.aggregate(
                result.matches, mismatched, key_of=location_key
            )


# ============================================================ 其他
class TestMetadataMisc:
    """边界与契约。"""

    def test_元数据命中不进raw字段泄露到报告(self, tmp_path):
        """``Match.raw`` 是敏感数据，报告只用 rule_name / 占位符 / 位置。

        这条守住"报告不含原文"这条产品底线（元数据是新增的命中来源，
        必须同样遵守）。
        """
        src = make_pdf(
            tmp_path / "raw.pdf", meta={"title": f"报告 {PHONE}"}
        )
        out = tmp_path / "raw_out.pdf"
        res = PdfHandler().process(src, out, engine())

        blob = res.message + "".join(
            f"{h.rule_name}{h.placeholder}{h.locations}" for h in res.hits
        )
        assert PHONE not in blob, (
            f"报告里出现了手机号原文 —— 严重泄露：{blob}"
        )

    def test_dry_run不改源文件(self, tmp_path):
        """dry-run 只在内存里改，源文件字节必须不变。"""
        src = make_pdf(
            tmp_path / "dry.pdf",
            body=f"联系电话 {PHONE}",
            meta={"title": f"报告 {PHONE}"},
        )
        before = src.read_bytes()

        res = PdfHandler().process(src, tmp_path / "dry_out.pdf", engine(),
                                   dry_run=True)

        assert res.output is None
        assert src.read_bytes() == before, "dry-run 改动了源文件"
        assert PHONE in fitz.open(str(src)).metadata.get("title", ""), (
            "dry-run 竟把源文件的元数据改了"
        )
        assert sum(h.count for h in res.hits) == 2, (
            f"dry-run 的命中数应含元数据，实际 {res.hits}"
        )

    def test_dry_run报告带元数据定位说明(self, tmp_path):
        src = make_pdf(tmp_path / "dry2.pdf", meta={"title": f"报告 {PHONE}"})
        res = PdfHandler().process(src, tmp_path / "dry2_out.pdf", engine(),
                                   dry_run=True)
        assert "元数据" in res.message, (
            f"dry-run 报告未说明命中在元数据里：{res.message}"
        )

    def test_无元数据的干净文件不产生告警(self, tmp_path):
        """回归保护：普通文件不该被元数据逻辑影响。"""
        src = make_pdf(tmp_path / "clean.pdf", body="这是一份普通文件")
        out = tmp_path / "clean_out.pdf"
        res = PdfHandler().process(src, out, engine())

        assert res.status == "ok"
        assert sum(h.count for h in res.hits) == 0
        assert "⚠️" not in res.message
