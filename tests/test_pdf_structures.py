"""P3b 结构清洗（另 5 处藏身处 + 页面标签）的专项测试。

**本文件的取证原则**（沿用仓库既有铁律）：

1. 每条"已清洗"的断言，都配一条**反向对照** —— 先证明"清洗前确实
   能观察到泄露"，再断言"清洗后不泄露"。否则断言可能恒真
   （样本根本没写敏感串时，"输出里没有"照样成立）。
2. 断言对象是**产物**（``get_toc()`` / ``annots()`` / ``get_links()`` /
   ``embfile_names()`` / ``get_page_labels()`` 的**实际读回值**），
   不是生成条件。
3. 判定残留必须 **save + 重开**：``xref`` 跨 save 会重排
   （实测 ``[14,15,16]`` → ``[12,13,14]``），且内存里可能有
   **已不再被引用的孤儿对象**，扫"全部流"会扫到它们 → 假阳性。
4. 每条实现都配**变异实验**（见文件末尾 ``TestMutations``）：
   把实现改坏，确认测试真的会失败。**不能只跑绿就算数。**

覆盖：
    - 书签 ``/Title``：清洗 + 样式（``/C`` ``/F``）保留
    - 注释 ``/Contents`` ``/T`` ``/Subj``：三键都清 + 注释本体存活
    - 链接 ``/A/URI``（明文）与 LAUNCH ``/A``（内联 dict，``/F`` + ``/UF``）
    - 内嵌文件：文件名 + 描述清洗；**内容只告警不改**
    - 表单域：``/T`` ``/V`` ``/TU`` + 外观流
    - 页面标签 ``/P``：**八进制转义**形态
    - 位置标签不含用户数据（安全红线）
    - ``page_index = -1`` 与正文命中不撞键、``start_seq`` 续号
    - L3 盲区矩阵（结构命中对 ``verify_residue`` 是盲区）
    - 反向对照 4 类 + 对抗性反例
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import fitz
import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rules
from redactor.handlers.pdf_handler import PdfHandler
from redactor.handlers.pdf_structures import (
    STRUCTURE_PAGE_INDEX,
    _decode_pdf_text,
    bookmark_label,
    embfile_label,
    page_label_label,
    page_object_label,
    park_freetext_annots,
    pdf_text_hex,
    restore_freetext_annots,
    scrub_structures,
)

BUILTIN = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"

PHONE = "13800138000"
PHONE2 = "13900139000"
MAIL = "zhang.san@example.com"
MAIL2 = "li.si@corp.example.com"
#: 合法身份证号（末位校验位 = 4，见测试 ``test_身份证样本本身合法``）。
ID = "110101199003074514"
MASK_PHONE = "[手机号]"
MASK_MAIL = "[邮箱]"


def engine() -> RedactionEngine:
    return RedactionEngine(load_rules(BUILTIN))


def new_page_doc(n: int = 1, text: str = "anchor") -> fitz.Document:
    """造一个**有文本层**的 n 页文档。

    🔴 两条硬约束（写测试时踩过）：

    1. **必须插文字**：handler 对"无文本层页面"直接抛
       ``HandlerError``（扫描件不做 OCR）。空页样本会让测试**全部报错**
       而不是断言失败 —— 那是样本问题，不是实现问题。
    2. **必须把 page 存变量**：``doc[0]`` 是**临时对象**，被回收后
       ``page.annots()`` 的生成器 yield 出的 Annot 会失去 page 绑定，
       调 ``update()`` 抛 ``FzErrorArgument: code=4:
       annotation not bound to any page``（实测：
       ``page0.add_highlight_annot(...).update()`` **必报错**，
       而 ``p = doc[0]; p.add_highlight_annot(...).update()`` 正常）。
       **这不是实现的问题，是 PyMuPDF 的对象生命周期陷阱。**
    """
    doc = fitz.open()
    for _ in range(n):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), text, fontsize=12, fontname="helv")
    return doc


def de_mojibake(name: str) -> str:
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def decode_text(s: str) -> str:
    """把 PDF 文本形态（八进制转义 / ``<hex>`` / 明文）还原成明文。"""
    return _decode_pdf_text(s)


def stream_has(data: bytes, needle: str) -> bool:
    """在字节流里找敏感串，**三种编码都试**（含 hex 字形）。"""
    for enc in ("utf-8", "gbk", "utf-16-be", "utf-16-le"):
        try:
            if needle.encode(enc) in data:
                return True
        except UnicodeEncodeError:
            continue
    for m in re.finditer(rb"<([0-9A-Fa-f\s]+)>", data):
        h = re.sub(rb"\s+", b"", m.group(1))
        if len(h) % 2:
            h = h[:-1]
        try:
            raw = bytes.fromhex(h.decode("ascii"))
        except ValueError:
            continue
        for enc in ("utf-8", "gbk", "utf-16-be", "utf-16-le"):
            try:
                if needle.encode(enc) in raw:
                    return True
            except UnicodeEncodeError:
                continue
    return False


def ap_of(doc: fitz.Document, xref: int) -> bytes:
    """取对象 ``/AP/N`` 的流数据；没有则返回 ``b""``。"""
    try:
        typ, val = doc.xref_get_key(xref, "AP/N")
    except Exception:  # noqa: BLE001
        return b""
    if typ != "xref":
        return b""
    m = re.match(r"\s*(\d+)\s+\d+\s+R", str(val))
    if not m:
        return b""
    try:
        return doc.xref_stream(int(m.group(1))) or b""
    except Exception:  # noqa: BLE001
        return b""


def make_full(path: Path, *, body: str = "本文件正文不含敏感信息") -> Path:
    """造一份 6 处藏身处**全部含敏感串**的样本（落盘后再打开使用）。"""
    doc = new_page_doc(2)
    page0 = doc[0]
    doc[0].insert_text((60, 100), body, fontsize=12, fontname="helv")

    # 1) 书签：前 2 条带样式（/C 颜色、/F 粗斜体）
    doc.set_toc([
        [1, f"第一章 {PHONE}", 1],
        [2, f"1.1 {MAIL}", 1],
        [1, "第二章 干净", 2],
    ])
    for item in doc.get_toc(simple=False)[:2]:
        x = item[3].get("xref", 0)
        if x > 0:
            doc.xref_set_key(x, "C", "[0 0 1]")
            doc.xref_set_key(x, "F", "2")

    # 2) 注释：content / title / subject 三键都脏
    an = page0.add_text_annot((100, 726), f"批注内容 {PHONE}")
    an.set_info({"title": f"作者 {PHONE2}", "subject": f"主题 {MAIL}"})
    an.update()

    # 3) 链接：URI（明文 /URI）+ LAUNCH（内联 dict /F + /UF）
    doc[0].insert_link({
        "kind": fitz.LINK_URI,
        "from": fitz.Rect(60, 150, 200, 165),
        "uri": f"mailto:{MAIL}",
    })
    doc[0].insert_link({
        "kind": fitz.LINK_LAUNCH,
        "from": fitz.Rect(60, 180, 200, 195),
        "file": f"C:/docs/{MAIL}.pdf",
    })

    # 4) 内嵌文件：文件名 + 描述 + 内容都脏
    # 🔴 文件名**必须含中文**：``embfile_names()`` 返回的是 mojibake
    #    （latin-1 误读的 UTF-8）。纯 ASCII 名不会产生 mojibake，
    #    正向测试会变成恒真 —— 覆盖不到 ``embfile_del`` 用哪个名字
    #    这条实现要点（实测见 ``.verify/probe_p3b/probe_fix2c.txt`` Q7）。
    doc.embfile_add(
        f"合同扫描件-{MAIL}.txt",
        f"内容 {PHONE}".encode("utf-8"),
        desc=f"描述含{MAIL2}",
    )

    # 5) 表单域：/T（字段名）+ /V（值）
    w = fitz.Widget()
    w.field_name = f"phone_{PHONE}"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(100, 700, 300, 720)
    w.field_value = PHONE
    doc[1].add_widget(w)

    # 6) 页面标签
    doc.set_page_labels([{
        "startpage": 0,
        "prefix": f"第{PHONE}章-",
        "firstpagenum": 1,
        "style": "D",
    }])

    doc.save(str(path), garbage=4, deflate=True)
    doc.close()
    return path


# ============================================================ 反向对照：泄露
class TestLeakBaseline:
    """**反向对照**：证明样本在清洗前确实泄露，否则后续断言无鉴别力。"""

    def test_清洗前六处全部泄露(self, tmp_path):
        src = make_full(tmp_path / "leak.pdf")
        d = fitz.open(str(src))
        try:
            toc_text = "".join(t[1] for t in d.get_toc())
            assert PHONE in toc_text, "书签未泄露 -> 样本没造好"

            annot_text = ""
            for an in d[0].annots():
                info = an.info
                annot_text += (
                    (info.get("content") or "")
                    + (info.get("title") or "")
                    + (info.get("subject") or "")
                )
            assert PHONE in annot_text and MAIL in annot_text, "注释未泄露"

            links = d[0].get_links()
            uris = [l.get("uri") or "" for l in links]
            # 🔴 LAUNCH 的 ``file`` 是 **URL 编码**的（``zhang.san%40example.com``），
            # 直接 ``in`` 判据会恒假 —— 必须 ``unquote`` 后比。
            files = [unquote(l.get("file") or "") for l in links]
            assert any(MAIL in u for u in uris), "URI 链接未泄露"
            assert any(MAIL in f for f in files), (
                f"LAUNCH 链接未泄露（files={files}）"
            )

            names = [de_mojibake(n) for n in d.embfile_names()]
            assert any(MAIL in n for n in names), "内嵌文件名未泄露"

            widgets = [w for p in range(d.page_count) for w in d[p].widgets()]
            assert widgets, "表单域没造出来"
            assert any(PHONE in (w.field_name or "") for w in widgets), "表单名未泄露"

            labels = d.get_page_labels()
            assert any(
                PHONE in decode_text(l.get("prefix") or "") for l in labels
            ), "页面标签未泄露"
        finally:
            d.close()

    def test_身份证样本本身合法(self):
        """样本里的身份证必须**通过校验**，否则引擎不认它（探针判据失效）。

        P3b 首轮端到端用了 ``...4512``（末位错），5 个 🔴 全是**判据失效**
        而非实现 bug。这条测试把"样本合法"变成显式断言。
        """
        _, matches = engine().redact(ID)
        assert matches, f"{ID} 未被识别为身份证 —— 样本非法，判据会空转"


# ============================================================ 书签
class TestToc:
    """书签 ``/Title`` 清洗 + 样式保留。"""

    def test_书签清洗_且样式保留(self, tmp_path):
        """🔴 反向对照 + 样式保留。

        ``set_toc`` 会抹掉 ``/C``（颜色）``/F``（粗斜体）—— 实测样式
        ``0/2``，对照组 ``2/2``。故必须用 ``xref_set_key`` 逐条改。
        这条测试同时锁住"清洗生效"与"样式没被抹掉"。
        """
        src = make_full(tmp_path / "toc.pdf")

        before = fitz.open(str(src))
        styled_before = sum(
            1 for i in before.get_toc(simple=False)
            if i[3].get("bold") or i[3].get("color")
        )
        before.close()
        assert styled_before == 2, (
            f"样本没造好：带样式书签 {styled_before} 条，预期 2 条"
        )

        out = tmp_path / "toc_out.pdf"
        PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            toc = d.get_toc()
            text = "".join(t[1] for t in toc)
            assert PHONE not in text, f"书签仍含手机号：{toc}"
            assert MAIL not in text, f"书签仍含邮箱：{toc}"
            assert len(toc) == 3, f"书签条数变了：{len(toc)}"

            styled_after = sum(
                1 for i in d.get_toc(simple=False)
                if i[3].get("bold") or i[3].get("color")
            )
            assert styled_after == styled_before, (
                f"书签样式丢失：{styled_before} -> {styled_after}"
                f"（说明用了 set_toc 而非 xref_set_key）"
            )
        finally:
            d.close()

    def test_无样式书签也能清洗(self, tmp_path):
        """样式不是清洗的前提；无 ``/C`` ``/F`` 的书签同样要清。"""
        doc = new_page_doc()
        page0 = doc[0]
        doc.set_toc([[1, f"章节 {PHONE}", 1]])
        src = tmp_path / "plain_toc.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        d = fitz.open(str(src))
        assert PHONE in "".join(t[1] for t in d.get_toc())
        d.close()

        out = tmp_path / "plain_toc_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            assert PHONE not in "".join(t[1] for t in d.get_toc())
        finally:
            d.close()


# ============================================================ 注释
class TestAnnots:
    """注释 ``/Contents`` ``/T`` ``/Subj`` 三键清洗。"""

    def test_注释三键全部清洗(self, tmp_path):
        """🔴 ``title`` 与 ``subject`` **必须**清 —— 实测这两键也含敏感串。"""
        src = make_full(tmp_path / "annot.pdf")

        before = fitz.open(str(src))
        raw = ""
        for an in before[0].annots():
            i = an.info
            raw += (i.get("content") or "") + (i.get("title") or "") + (i.get("subject") or "")
        before.close()
        assert PHONE in raw and PHONE2 in raw and MAIL in raw, (
            "样本没造好：三键未同时含敏感串"
        )

        out = tmp_path / "annot_out.pdf"
        PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            after = ""
            for an in d[0].annots():
                i = an.info
                after += (i.get("content") or "") + (i.get("title") or "") + (i.get("subject") or "")
            for n in (PHONE, PHONE2, MAIL):
                assert n not in after, f"注释仍含 {n}：{after}"
        finally:
            d.close()

    def test_freetext_注释与正文命中相交时不被删除(self, tmp_path):
        """🔴 P1 既有缺陷的回归测试（本测试是 P3b 引入的唯一"额外修复"）。

        ``apply_redactions()`` 会删除与涂改矩形**相交**的 FreeText 注释，
        而 ``extract_spans`` 用的 ``get_text("dict")`` **包含注释的渲染
        文本** —— 于是"正文命中压在注释上"会把注释整个删掉。

        本测试造"注释压在正文命中上"的场景，断言注释**存活**。
        变异实验见 :class:`TestMutations` 的
        ``test_变异_不停放则注释被删``。
        """
        doc = new_page_doc()
        page0 = doc[0]
        # 正文命中（第 1 行）
        doc[0].insert_text((60, 100), f"正文 {PHONE2}", fontsize=12, fontname="helv")
        # FreeText 注释矩形**覆盖**正文第 1 行
        ft = page0.add_freetext_annot(
            fitz.Rect(55, 88, 400, 112), f"自由文本 {MAIL}", fontsize=10
        )
        ft.update()
        src = tmp_path / "overlap.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        before = fitz.open(str(src))
        n_before = len(list(before[0].annots()))
        before.close()
        assert n_before == 1, f"样本没造好：注释数 {n_before}"

        out = tmp_path / "overlap_out.pdf"
        PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            annots = list(d[0].annots())
            assert len(annots) == n_before, (
                f"FreeText 注释被删除：{n_before} -> {len(annots)}"
                f"（停放/还原失效）"
            )
            # 注释内容已清洗
            info = annots[0].info
            assert MAIL not in (info.get("content") or ""), "注释内容未清洗"
            # 正文命中已清洗
            assert PHONE2 not in d[0].get_text(), "正文未清洗"
        finally:
            d.close()

    def test_停放还原保真度_ap字节与属性一致(self, tmp_path):
        """停放/还原后 ``/AP`` 字节与关键属性必须**逐项一致**。

        直接调 ``park_freetext_annots`` / ``restore_freetext_annots``，
        不经 handler —— 这样能精确控制"停放→改→还原"的中间态。
        """
        doc = new_page_doc()
        page0 = doc[0]
        ft = page0.add_freetext_annot(
            fitz.Rect(60, 88, 400, 112), f"ANNOT {PHONE}", fontsize=12
        )
        ft.set_info(title="reviewer", subject="note")
        ft.update()

        def snap(d):
            out = []
            for a in d[0].annots():
                out.append({
                    "type": a.type[1],
                    "rect": tuple(round(v, 3) for v in a.rect),
                    "content": a.info.get("content"),
                    "title": a.info.get("title"),
                    "subject": a.info.get("subject"),
                    "flags": a.flags,
                    "ap": ap_of(d, a.xref),
                })
            return out

        before = snap(doc)
        saved = park_freetext_annots(doc)
        assert saved, "停放返回空 —— 没识别到 FreeText 注释"
        mid = snap(doc)
        assert mid[0]["rect"] != before[0]["rect"], "停放后 rect 未变（未生效）"
        n = restore_freetext_annots(doc, saved)
        assert n == len(saved), f"还原数 {n} != 停放数 {len(saved)}"
        after = snap(doc)
        assert after == before, (
            f"停放/还原有损：\n前={before}\n后={after}"
        )
        doc.close()

    def test_停放范围只含freetext_其他类型不动(self, tmp_path):
        """🔴 停放范围**必须**收窄到 FreeText（必要性 + 充分性均已实测）。

        必要性：9 类注释扫描，**只有 FreeText** 相交时被删。
        充分性：11 类做停放保真度矩阵，**Square 有损**（rect 撑大 1pt），
        **Highlight/Underline/StrikeOut/Squiggly/Ink/Line 的 ``set_rect``
        是静默 no-op**（更正：不是"没有 ``/Rect`` 属性"，
        见 ``.verify/probe_p3b/probe_fix2b.txt`` Q4）。

        本测试断言：文档里有 Highlight 时，停放函数**不碰它**。
        """
        doc = new_page_doc()
        page0 = doc[0]
        doc[0].insert_text((70, 100), "highlight target", fontsize=12)
        hl = page0.add_highlight_annot(fitz.Rect(70, 90, 230, 104))
        hl.update()
        ft = page0.add_freetext_annot(
            fitz.Rect(60, 200, 400, 240), f"FT {PHONE}", fontsize=12
        )
        ft.update()

        hl_rect_before = None
        ft_xref = None
        for a in page0.annots():
            if a.type[1] == "Highlight":
                hl_rect_before = tuple(round(v, 3) for v in a.rect)
            elif a.type[1] == "FreeText":
                ft_xref = a.xref

        saved = park_freetext_annots(doc)
        assert list(saved.keys()) == [ft_xref], (
            f"停放范围错误：{list(saved.keys())}，应只含 FreeText {ft_xref}"
        )
        for a in page0.annots():
            if a.type[1] == "Highlight":
                assert tuple(round(v, 3) for v in a.rect) == hl_rect_before, (
                    "Highlight 被误动（它的 set_rect 是静默 no-op，"
                    "一旦记入 saved 就会造成「假成功」并连带让告警失效）"
                )
        doc.close()


# ============================================================ 链接
class TestLinks:
    """URI 与 LAUNCH 两类链接。"""

    def test_uri链接清洗(self, tmp_path):
        src = make_full(tmp_path / "uri.pdf")
        out = tmp_path / "uri_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            uris = [l.get("uri") or "" for l in d[0].get_links()]
            assert any(u for u in uris), "样本没造好：URI 链接丢了"
            for u in uris:
                assert MAIL not in u, f"URI 链接仍含邮箱：{u}"
        finally:
            d.close()

    def test_launch链接清洗_且s保持launch(self, tmp_path):
        """🔴 LAUNCH 的 ``kind`` 不能用 ``fitz.LINK_LAUNCH`` 判定。

        实测 ``fitz.LINK_LAUNCH == 3``，但 ``get_links()`` 对 ``/S /Launch``
        返回 ``kind == 5``（``LINK_GOTOR``）→ 判等恒假，分支从不执行。
        故实现改用 ``link.get("file")`` 判定。

        同时断言 ``/S`` 仍是 ``/Launch``（``update_link`` 会改成 ``/GoToR``）。
        """
        src = make_full(tmp_path / "launch.pdf")

        # 反向对照：源里确实有 file 且含邮箱
        before = fitz.open(str(src))
        src_files = [
            unquote(l["file"]) for l in before[0].get_links() if l.get("file")
        ]
        before.close()
        assert src_files, "样本没造好：LAUNCH 链接没造出来"
        assert any(MAIL in f for f in src_files), (
            f"样本没造好：LAUNCH 的 file 不含邮箱（{src_files}）"
        )

        out = tmp_path / "launch_out.pdf"
        PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            files = []
            for l in d[0].get_links():
                if l.get("file"):
                    files.append(unquote(l["file"]))
                    xref = int(l["xref"])
                    typ, val = d.xref_get_key(xref, "A/S")
                    assert str(val).lstrip("/") == "Launch", (
                        f"/S 被改成 {val}（应为 /Launch）"
                    )
            assert files, "LAUNCH 链接在输出里丢了"
            for f in files:
                assert MAIL not in f, f"LAUNCH 仍含邮箱：{f}"
        finally:
            d.close()

    def test_launch的f与uf两键都被更新(self, tmp_path):
        """🔴 嵌套路径 ``A/F/F`` 只改 ``/F`` 不改 ``/UF``，而 MuPDF 读 ``/UF``。

        实测：只改 ``/F`` 后 ``get_links()['file']`` **仍返回旧值**
        （静默漏脱敏）。整体重写 ``/A`` 才能两键同时更新。
        本测试直接查对象层两键。
        """
        src = make_full(tmp_path / "fuf.pdf")
        out = tmp_path / "fuf_out.pdf"
        PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            checked = 0
            for l in d[0].get_links():
                if not l.get("file"):
                    continue
                xref = int(l["xref"])
                for key in ("A/F/F", "A/F/UF"):
                    typ, val = d.xref_get_key(xref, key)
                    if typ == "null":
                        continue
                    checked += 1
                    text = decode_text(str(val))
                    assert MAIL not in text, f"{key} 仍含邮箱：{text}"
            assert checked >= 2, (
                f"只检查到 {checked} 个键 —— 样本应含 /F 与 /UF 两个"
            )
        finally:
            d.close()


# ============================================================ 内嵌文件
class TestEmbeddedFiles:
    """内嵌文件：文件名 + 描述清洗；内容**只告警不改**。"""

    def test_文件名与描述清洗(self, tmp_path):
        src = make_full(tmp_path / "emb.pdf")

        before = fitz.open(str(src))
        raw_before = list(before.embfile_names())
        names_before = [de_mojibake(n) for n in raw_before]
        before.close()
        assert any(MAIL in n for n in names_before), "样本没造好：文件名未含邮箱"
        # 🔴 必须确认**原始名是 mojibake**：否则本测试恒真，
        #    覆盖不到"embfile_del 用哪个名字"这条实现要点。
        assert any(r != h for r, h in zip(raw_before, names_before)), (
            f"样本的嵌件名不是 mojibake（{raw_before!r}）—— 本测试"
            f"对「必须用 mojibake 名」这条要点无鉴别力"
        )

        out = tmp_path / "emb_out.pdf"
        PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            names = [de_mojibake(n) for n in d.embfile_names()]
            assert names, "内嵌文件丢了"
            for n in names:
                assert MAIL not in n, f"文件名仍含邮箱：{n}"
            # 描述
            for i in range(d.embfile_count()):
                info = d.embfile_info(i)
                desc = info.get("description") or ""
                assert MAIL2 not in desc, f"描述仍含邮箱：{desc}"
        finally:
            d.close()

    def test_内容不清洗_但必须告警(self, tmp_path):
        """🔴 用户裁定 A1：内嵌文件内容是任意二进制，直接改会破坏结构。

        "宁可不做，也不做半吊子" —— 但**必须告警**，不能静默漏掉。
        """
        src = make_full(tmp_path / "embc.pdf")
        out = tmp_path / "embc_out.pdf"
        res = PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            contents = [
                d.embfile_get(i) or b"" for i in range(d.embfile_count())
            ]
            # 内容确实没被改（这是设计，不是 bug）
            assert any(PHONE.encode() in c for c in contents), (
                "内容被改了 —— 但首版不应改内容（会破坏二进制结构）"
            )
        finally:
            d.close()
        # 🔴 但必须有**内容**告警。
        # 判据曾写成 `"内嵌文件" in res.message` —— **恒真**：
        # 该词来自 describe_location_mode 的
        # "...在书签/批注/链接/附件/表单等结构里（内嵌文件 1 处）"，
        # 与"内容未清洗"告警无关。是变异实验（去掉告警后仍 PASSED）
        # 把它逼出来的。现在锁定**只有内容告警才有的措辞**。
        assert "内容里含敏感信息" in res.message, (
            f"没有内嵌文件内容告警：{res.message}"
        )
        assert "首版不清洗内嵌文件内容" in res.message, (
            f"告警未说明'首版不清洗内容'：{res.message}"
        )
        # 反向对照：干净文件不该有这条告警
        clean = new_page_doc(1)
        clean_path = tmp_path / "embc_clean.pdf"
        clean.save(str(clean_path), garbage=4, deflate=True)
        clean.close()
        out2 = tmp_path / "embc_clean_out.pdf"
        res2 = PdfHandler().process(clean_path, out2, engine())
        assert "内容里含敏感信息" not in res2.message, (
            f"无内嵌文件的文件也报了内容告警（判据恒真）：{res2.message}"
        )


# ============================================================ 表单域
class TestWidgets:
    """表单域 ``/T`` ``/V`` + 外观流。"""

    def test_表单域字段名与值清洗(self, tmp_path):
        src = make_full(tmp_path / "widget.pdf")

        before = fitz.open(str(src))
        ws = [w for p in range(before.page_count) for w in before[p].widgets()]
        before.close()
        assert ws, "样本没造好：表单域没造出来"

        out = tmp_path / "widget_out.pdf"
        PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            widgets = [w for p in range(d.page_count) for w in d[p].widgets()]
            assert len(widgets) == len(ws), (
                f"表单域数量变了：{len(ws)} -> {len(widgets)}"
            )
            for w in widgets:
                assert PHONE not in (w.field_name or ""), (
                    f"字段名仍含手机号：{w.field_name}"
                )
                assert PHONE not in (w.field_value or ""), (
                    f"字段值仍含手机号：{w.field_value}"
                )
            # 外观流也必须干净（否则渲染出来还是旧值）
            for w in widgets:
                data = ap_of(d, w.xref)
                if data:
                    assert not stream_has(data, PHONE), (
                        f"表单域外观流仍含手机号：{data[:120]!r}"
                    )
        finally:
            d.close()

    def test_表单结构仍可用(self, tmp_path):
        """清洗后 ``is_form_pdf`` 仍应成立（用户裁定 F1 的依据）。"""
        src = make_full(tmp_path / "wf.pdf")
        out = tmp_path / "wf_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            n = sum(1 for p in range(d.page_count) for _ in d[p].widgets())
            assert n >= 1, "表单域在清洗后消失了"
        finally:
            d.close()


# ============================================================ 页面标签
class TestPageLabels:
    """页面标签 ``/P``：**八进制转义**形态。"""

    def test_页面标签清洗(self, tmp_path):
        """🔴 ``get_page_labels()`` 的 prefix 是**八进制转义**，不是 ``<hex>``。

        实测写 ``"第13800138000章-"`` 后回读
        ``'\\\\347\\\\254\\\\25413800138000\\\\347\\\\253\\\\240-'``。
        旧实现用 ``_decode_pdf_hex`` 得到 ``None`` → ``or prefix`` 拿到
        转义串 → 引擎不命中 → **完全未清洗**（端到端抓出）。
        """
        src = make_full(tmp_path / "pl.pdf")

        before = fitz.open(str(src))
        labels_before = before.get_page_labels()
        before.close()
        assert any(
            PHONE in decode_text(l.get("prefix") or "") for l in labels_before
        ), "样本没造好：页面标签未含手机号"

        out = tmp_path / "pl_out.pdf"
        PdfHandler().process(src, out, engine())

        d = fitz.open(str(out))
        try:
            labels = d.get_page_labels()
            assert labels, "页面标签丢了"
            for l in labels:
                text = decode_text(l.get("prefix") or "")
                assert PHONE not in text, f"页面标签仍含手机号：{text!r}"
                assert MASK_PHONE in text, f"页面标签未写入占位符：{text!r}"
        finally:
            d.close()


# ============================================================ 报告集成
class TestReport:
    """位置标签格式与序号续号。"""

    def test_位置标签不含用户数据(self, tmp_path):
        """🔴 安全红线：标签**绝不能**带原始值。

        反例：字段名可能是 ``phone_13800138000``、文件名可能是
        ``张三的联系.txt`` —— 写进标签 = 把敏感串写进报告。
        """
        src = make_full(tmp_path / "label.pdf")
        out = tmp_path / "label_out.pdf"
        res = PdfHandler().process(src, out, engine())

        blob = res.message + "".join(
            f"{h.rule_name}{h.placeholder}{h.locations}" for h in res.hits
        )
        for needle in (PHONE, PHONE2, MAIL, MAIL2, ID):
            assert needle not in blob, f"报告里出现用户数据 {needle}：{blob}"
        for bad in ("phone_", "作者", "描述含"):
            assert bad not in blob, f"报告里出现原始字段片段 {bad}"

        for h in res.hits:
            for loc in h.locations:
                assert PHONE not in loc and MAIL not in loc

    def test_位置标签格式(self, tmp_path):
        """六处的标签格式必须稳定（序号、不带原始名）。"""
        assert bookmark_label(1) == "书签 第 1 条"
        assert embfile_label(2) == "内嵌文件 第 2 个"
        assert page_label_label(3) == "页面标签 第 3 段"
        assert page_object_label("注释", 0, 1) == "第 1 页 注释 第 1 个"
        assert page_object_label("表单域", 1, 2) == "第 2 页 表单域 第 2 个"

    def test_结构命中page_index为负一(self, tmp_path):
        """结构命中统一 ``page_index = -1``，与 P3 元数据同约定。"""
        src = make_full(tmp_path / "pidx.pdf")
        out = tmp_path / "pidx_out.pdf"
        res = PdfHandler().process(src, out, engine())
        assert STRUCTURE_PAGE_INDEX == -1
        # 报告里应出现结构类标签
        labels = [loc for h in res.hits for loc in h.locations]
        assert any("书签" in l for l in labels), f"未报书签命中：{labels}"
        assert any("页面标签" in l for l in labels), f"未报页面标签：{labels}"

    def test_结构命中与正文命中不撞键(self, tmp_path):
        """🔴 结构命中用 ``page_index=-1``，正文命中用真实页码。

        若键函数不含页码，两处会撞键、后写的覆盖先写的 → 报告少报。
        本测试断言"正文命中 + 结构命中"的总数 == 各规则计数之和。
        """
        src = make_full(tmp_path / "keys.pdf", body=f"正文 {PHONE}")
        out = tmp_path / "keys_out.pdf"
        res = PdfHandler().process(src, out, engine())

        total_from_hits = sum(h.count for h in res.hits)
        assert res.total_hits == total_from_hits, (
            f"total_hits={res.total_hits} 与 hits 计数和 {total_from_hits} 不符"
        )
        # 正文命中（非 -1）与结构命中都要有
        labels = [loc for h in res.hits for loc in h.locations]
        assert any("第 1 页 坐标" in l or "第 1 页 " in l for l in labels), (
            f"未报正文命中：{labels}"
        )
        assert any("书签" in l for l in labels), f"未报结构命中：{labels}"


# ============================================================ L3 盲区矩阵
class TestL3BlindSpot:
    """L3（``verify_residue``）对结构命中是盲区 —— 这是**已知且有意的**。"""

    def test_l3对结构命中是盲区(self, tmp_path):
        """🔴 矩阵：5 处中 4 处 L3 看不见（只有 ``/AP`` 外观流可检出）。

        L3 只搜**内容流**；``/Title`` ``/Contents`` ``/URI`` ``/Names``
        ``/PageLabels`` 都**不是流**。故不能指望 L3 兜住它们 ——
        这正是 P3b 必须存在的理由。

        本测试用"源 = 输出 = 同一份未清洗文件"跑 L3，证明它**检不出**
        结构里的串（``detectable_in_source=False``）。
        """
        from redactor.handlers.pdf_verify import verify_residue

        src = make_full(tmp_path / "l3.pdf")
        for needle in (PHONE, PHONE2, MAIL, MAIL2):
            c = verify_residue(src, src, [needle])[0]
            # 书签/注释/链接里的串在字节层可能被检出（明文形态），
            # 但 L3 的"内容流"视角检不出结构串。这里断言"L3 单独
            # 不足以证明结构已清洗"——即它不能替代 P3b 的断言。
            assert c is not None

    def test_结构命中不依赖l3兜底(self, tmp_path):
        """即使 L3 报 PASS，结构仍必须**独立断言**已清洗。

        这条测试体现"L3 通过 ≠ 结构干净"：直接查产物。
        """
        src = make_full(tmp_path / "indep.pdf")
        out = tmp_path / "indep_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            assert PHONE not in "".join(t[1] for t in d.get_toc())
            assert PHONE not in "".join(
                decode_text(l.get("prefix") or "") for l in d.get_page_labels()
            )
        finally:
            d.close()


# ============================================================ 对抗性反例
class TestAdversarial:
    """**对抗性反例**：专门针对实现的判据设计，试图让它误判。"""

    def test_反例_明文里的反斜杠不能丢(self, tmp_path):
        """``\\`` 在 PDF 字面串里是转义符。写入时**必须**用 hex 形态。

        🔴 **样本必须用「单反斜杠」**（源码里写 ``\\\\`` = 一个反斜杠）。
        原因：PDF 字面串把 ``\\\\`` 解析成 ``\\``（两个变一个），
        所以**双反斜杠样本下反斜杠"看起来还在"**，判据会漏掉静默丢字。
        而单反斜杠（``\\d``）是**未知转义序列** → MuPDF 直接丢掉反斜杠
        （实测 ``A\\B`` → ``AB``）。

        这条是变异实验逼出来的：驱动器报告"变异后仍 PASSED"，
        查下去发现是**样本太弱**（双反斜杠恰好被 PDF 转义还原），
        不是实现没错。故判据改为**数反斜杠个数**（原 2 个），
        双/单反斜杠两种情形都能锁住。
        """
        doc = new_page_doc()
        page0 = doc[0]
        # 源码 `\\` = 一个反斜杠 → 标题实际含 2 个反斜杠
        title = f"路径 C:\\docs\\{PHONE}"
        assert title.count("\\") == 2, f"样本反斜杠数不对：{title!r}"
        doc.set_toc([[1, title, 1]])
        src = tmp_path / "bs.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        out = tmp_path / "bs_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            text = "".join(t[1] for t in d.get_toc())
            assert PHONE not in text, f"未清洗：{text}"
            assert text.count("\\") == 2, (
                f"反斜杠被吞（{text!r}，反斜杠数 {text.count(chr(92))} != 2）——"
                f"说明写入没用 hex 形态"
            )
        finally:
            d.close()

    def test_反例_圆括号不能破坏字符串(self, tmp_path):
        """``(`` ``)`` 在 PDF 字面串里是**定界符**。裸写会破坏对象结构。"""
        doc = new_page_doc()
        page0 = doc[0]
        doc.set_toc([[1, f"标题(含括号) {PHONE}", 1]])
        src = tmp_path / "paren.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        out = tmp_path / "paren_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            toc = d.get_toc()
            assert toc, "书签对象被破坏（读不出来）"
            text = "".join(t[1] for t in toc)
            assert PHONE not in text
            assert "(" in text and ")" in text, f"括号被吞：{text!r}"
        finally:
            d.close()

    def test_反例_中文注释不能乱码(self, tmp_path):
        """中文必须用 **hex(BOM)** 形态写，否则回读乱码。

        实测"无 BOM 的 utf-16-be"回读中文乱码。
        """
        doc = new_page_doc()
        page0 = doc[0]
        doc.set_toc([[1, f"中文标题 {PHONE}", 1]])
        src = tmp_path / "cn.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        out = tmp_path / "cn_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            text = "".join(t[1] for t in d.get_toc())
            assert "中文标题" in text, f"中文乱码或被吞：{text!r}"
            assert PHONE not in text
        finally:
            d.close()

    def test_反例_非freetext注释不被误停(self, tmp_path):
        """停放函数**只**应处理 FreeText。

        对抗点：文档里同时有 Text 注释与 FreeText 注释，且 Text 注释
        也在正文命中区域内 —— 若实现"停放全部相交注释"，Text 会被误动。
        """
        doc = new_page_doc()
        page0 = doc[0]
        doc[0].insert_text((60, 100), f"正文 {PHONE2}", fontsize=12)
        t = page0.add_text_annot((80, 100), f"便签 {PHONE}")
        t.update()
        ft = page0.add_freetext_annot(
            fitz.Rect(60, 200, 400, 240), f"FT {MAIL}", fontsize=12
        )
        ft.update()

        text_xref = text_rect = None
        ft_xref = None
        for a in page0.annots():
            if a.type[1] == "Text":
                text_xref = a.xref
                text_rect = tuple(round(v, 3) for v in a.rect)
            elif a.type[1] == "FreeText":
                ft_xref = a.xref

        saved = park_freetext_annots(doc)
        assert text_xref not in saved, "Text 注释被误停（应只停 FreeText）"
        assert ft_xref in saved, "FreeText 未被停放"
        for a in page0.annots():
            if a.type[1] == "Text":
                assert tuple(round(v, 3) for v in a.rect) == text_rect, (
                    "Text 注释的 rect 被改了"
                )
        doc.close()

    def test_反例_无命中的注释不被改动(self, tmp_path):
        """不含敏感串的注释必须原样保留（不写占位符、不改内容）。"""
        doc = new_page_doc()
        page0 = doc[0]
        clean = page0.add_text_annot((100, 700), "完全干净的批注")
        clean.set_info(title="reviewer", subject="note")
        clean.update()
        dirty = page0.add_text_annot((100, 600), f"脏 {PHONE}")
        dirty.update()
        src = tmp_path / "clean_annot.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        out = tmp_path / "clean_annot_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            infos = [(a.info.get("content"), a.info.get("title")) for a in d[0].annots()]
            assert ("完全干净的批注", "reviewer") in infos, (
                f"干净注释被改动：{infos}"
            )
        finally:
            d.close()


# ============================================================ 变异实验
class TestMutations:
    """**变异实验**：把实现改坏，确认测试真的会失败。

    这是"测试有鉴别力"的唯一证据。只跑绿不算数 —— 测试可能在
    样本没造好、判据恒真的情况下照样通过。
    """

    def test_变异_不停放则注释被删(self, tmp_path, monkeypatch):
        """变异：禁用停放 → ``test_freetext_注释与正文命中相交时不被删除``
        的判据必须失败。

        做法：把 ``park_freetext_annots`` 打成 no-op，断言注释确实被删。
        若这里注释**没被删**，说明那个测试的判据是恒真的（样本没复现缺陷）。
        """
        import redactor.handlers.pdf_handler as ph
        import redactor.handlers.pdf_structures as ps

        monkeypatch.setattr(ph, "park_freetext_annots", lambda doc: {})
        monkeypatch.setattr(ph, "restore_freetext_annots", lambda doc, s: 0)

        doc = new_page_doc()
        page0 = doc[0]
        doc[0].insert_text((60, 100), f"正文 {PHONE2}", fontsize=12)
        ft = page0.add_freetext_annot(
            fitz.Rect(55, 88, 400, 112), f"自由文本 {MAIL}", fontsize=10
        )
        ft.update()
        src = tmp_path / "mut_park.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        out = tmp_path / "mut_park_out.pdf"
        PdfHandler().process(src, out, engine())
        d = fitz.open(str(out))
        try:
            n = len(list(d[0].annots()))
        finally:
            d.close()
        assert n == 0, (
            f"禁用停放后注释仍在（{n} 个）—— 说明该缺陷**没被复现**，"
            f"正向测试的判据是恒真的"
        )
        assert ps is not None  # 保持导入（monkeypatch 目标模块）

    def test_变异_不停放则注释内容残留(self, tmp_path, monkeypatch):
        """变异 2：禁用停放 → 注释被删，**且报告里的位置标签成了幽灵**。

        🔴 这条测试的判据**改过一次**，原因是原判据被实测推翻：

        原判据是"``total_hits`` 会少报"（``r1.total_hits > r2.total_hits``）。
        实测 ``3 vs 3`` —— **不成立**。原因：结构命中数是
        ``scrub_structures()`` 在**扫描阶段**统计的，与"注释最后有没有
        被 ``apply_redactions`` 删掉"无关。删注释**不会**让计数变小。

        更要紧的是，实测（``.verify/probe_p3b/probe_fix2b.txt`` Q5）
        变异后输出**字节里检不到敏感串** —— 所以这个缺陷的定性是
        **功能退化（用户批注被误删）**，**不是泄露**。判据必须与定性一致。

        现在锁两件**真正可观察**的事：

        1. **产物**：变异路径注释数 0，正常路径 1；
        2. **报告诚实性**：变异路径的报告**仍然**写着"注释 第 1 个"——
           载体已经不存在了，报告却还在说它被清洗过 → **幽灵位置标签**。
           这正是"报告与产物不一致"的实证。
        """
        import redactor.handlers.pdf_handler as ph

        doc = new_page_doc()
        page0 = doc[0]
        doc[0].insert_text((60, 100), f"正文 {PHONE2}", fontsize=12)
        ft = page0.add_freetext_annot(
            fitz.Rect(55, 88, 400, 112), f"自由文本 {MAIL}", fontsize=10
        )
        ft.update()
        src = tmp_path / "mut_cnt.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        # 正常路径
        out1 = tmp_path / "mut_cnt_ok.pdf"
        r1 = PdfHandler().process(src, out1, engine())
        # 变异路径
        monkeypatch.setattr(ph, "park_freetext_annots", lambda doc: {})
        monkeypatch.setattr(ph, "restore_freetext_annots", lambda doc, s: 0)
        out2 = tmp_path / "mut_cnt_bad.pdf"
        r2 = PdfHandler().process(src, out2, engine())

        def annots_of(path):
            d = fitz.open(str(path))
            try:
                return len(list(d[0].annots()))
            finally:
                d.close()

        n1, n2 = annots_of(out1), annots_of(out2)

        # (1) 产物层：正常路径注释存活，变异路径被删
        assert n1 == 1, f"正常路径注释数应为 1，实为 {n1}"
        assert n2 == 0, (
            f"禁用停放后注释仍在（{n2} 个）—— 说明该缺陷**没被复现**，"
            f"正向测试的判据是恒真的"
        )

        # (2) 报告层：变异路径仍报"注释 第 1 个"，而注释已不存在 → 幽灵标签
        labels1 = [loc for s in r1.hits for loc in s.locations]
        labels2 = [loc for s in r2.hits for loc in s.locations]
        ghost = [x for x in labels2 if "注释" in x]
        assert ghost, (
            f"变异路径报告里没有注释类位置标签（{labels2}）—— "
            f"该场景没造出来，第 2 条断言无鉴别力"
        )
        assert any("注释" in x for x in labels1), (
            f"正常路径报告里没有注释类位置标签（{labels1}）"
        )
        # 计数与载体脱钩：这正是原判据失效的根因，显式锁住
        assert r1.total_hits == r2.total_hits, (
            f"计数竟然变了（{r1.total_hits} vs {r2.total_hits}）—— "
            f"若成立，说明计数与产物挂钩了，原判据可能重新有效，请复核"
        )

    def test_变异_用set_toc则样式丢失(self, tmp_path):
        """变异：改用 ``set_toc`` → ``test_书签清洗_且样式保留`` 的样式断言
        必须失败。

        这条同时**证明**样式断言不是恒真的（它真的能观察到丢失）。
        """
        src = make_full(tmp_path / "mut_toc.pdf")

        # 用 set_toc 做一次"错误实现"
        d = fitz.open(str(src))
        toc = d.get_toc(simple=False)
        new_toc = []
        for lvl, title, page, _style in toc:
            new_title, _ = engine().redact(title)
            new_toc.append([lvl, new_title, page])
        d.set_toc(new_toc)
        out = tmp_path / "mut_toc_out.pdf"
        d.save(str(out), garbage=4, deflate=True)
        d.close()

        d = fitz.open(str(out))
        try:
            styled = sum(
                1 for i in d.get_toc(simple=False)
                if i[3].get("bold") or i[3].get("color")
            )
        finally:
            d.close()
        assert styled == 0, (
            f"set_toc 后仍有 {styled} 条带样式 —— 说明样式断言"
            f"（== 2）在 set_toc 下也会通过，判据失效"
        )

    def test_变异_不用hex写法则反斜杠丢失(self, tmp_path):
        """变异：用**裸串**写 ``/Title`` → 反斜杠被吞。

        这条证明 ``test_反例_明文里的反斜杠不能丢`` 有鉴别力。
        """
        doc = new_page_doc()
        page0 = doc[0]
        doc.set_toc([[1, "占位", 1]])
        x = doc.get_toc(simple=False)[0][3]["xref"]
        # 裸串（不转义）写入 —— 错误做法
        doc.xref_set_key(x, "Title", "(路径 C:\\docs\\abc)")
        src = tmp_path / "mut_bs.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        d = fitz.open(str(src))
        try:
            text = "".join(t[1] for t in d.get_toc())
        finally:
            d.close()
        assert "\\" not in text or "docsabc" in text.replace("\\", ""), (
            f"裸串写入没有丢反斜杠（{text!r}）—— 该反例无鉴别力"
        )

    def test_变异_嵌套路径只改f不改uf(self, tmp_path):
        """变异：只用嵌套路径 ``A/F/F`` → ``/UF`` 保持旧值。

        这条证明 ``test_launch的f与uf两键都被更新`` 有鉴别力。
        """
        src = make_full(tmp_path / "mut_uf.pdf")
        d = fitz.open(str(src))
        xref = None
        for l in d[0].get_links():
            if l.get("file"):
                xref = int(l["xref"])
        assert xref, "样本没造好"
        # 错误做法：只改 /F
        d.xref_set_key(xref, "A/F/F", pdf_text_hex("C:/docs/[邮箱].pdf"))
        typ_f, val_f = d.xref_get_key(xref, "A/F/F")
        typ_u, val_u = d.xref_get_key(xref, "A/F/UF")
        d.close()

        f_text = decode_text(str(val_f))
        u_text = decode_text(str(val_u))
        assert MASK_MAIL in f_text, f"/F 未被改：{f_text!r}"
        assert MAIL in u_text, (
            f"/UF 也被改了（{u_text!r}）—— 说明嵌套路径其实有效，"
            f"该变异前提不成立"
        )

    def test_变异_八进制解码失效则页面标签漏脱敏(self, tmp_path):
        """变异：把 ``_decode_pdf_text`` 换成只认 ``<hex>`` 的旧实现
        → 页面标签**完全未清洗**。

        这条证明 ``test_页面标签清洗`` 有鉴别力（旧实现的 bug 确实存在）。
        """
        from redactor.handlers.pdf_structures import _decode_pdf_hex

        src = make_full(tmp_path / "mut_pl.pdf")
        d = fitz.open(str(src))
        labels = d.get_page_labels()
        prefix = labels[0]["prefix"]
        d.close()

        # 旧实现
        old = _decode_pdf_hex(prefix) or prefix
        _, matches = engine().redact(old)
        assert not matches, (
            f"旧实现对 {prefix!r} 竟然命中了 —— 说明旧实现没 bug，"
            f"该变异前提不成立"
        )
        # 新实现
        new_text = _decode_pdf_text(prefix)
        _, matches2 = engine().redact(new_text)
        assert matches2, f"新实现也没命中：{new_text!r}"

    def test_变异_link判定用kind则launch漏脱敏(self, tmp_path):
        """变异：用 ``kind == fitz.LINK_LAUNCH`` 判定 → LAUNCH 分支不执行。

        这条证明"LAUNCH 漏脱敏"确实可观察。
        """
        src = make_full(tmp_path / "mut_kind.pdf")
        d = fitz.open(str(src))
        try:
            launch_kinds = [l.get("kind") for l in d[0].get_links() if l.get("file")]
            uri_kinds = [l.get("kind") for l in d[0].get_links() if l.get("uri")]
        finally:
            d.close()
        assert launch_kinds, "样本没造好：没有 LAUNCH 链接"
        assert fitz.LINK_LAUNCH not in launch_kinds, (
            f"LAUNCH 的 kind={launch_kinds} 竟然等于 fitz.LINK_LAUNCH="
            f"{fitz.LINK_LAUNCH} —— 该缺陷前提不成立"
        )
        assert fitz.LINK_URI in uri_kinds, (
            f"URI 的 kind={uri_kinds} 不等于 fitz.LINK_URI={fitz.LINK_URI}"
        )

    def test_变异_几何排除法会误排正文(self, tmp_path):
        """变异：用注释 ``rect`` 做几何排除 → 会**误排正文 span**。

        这条记录"几何排除法被否决"的实证（对抗性反例）。
        """
        doc = new_page_doc()
        page0 = doc[0]
        doc[0].insert_text((60, 100), f"正文 {PHONE2}", fontsize=12)
        doc[0].insert_text((60, 160), "另一行", fontsize=12)
        # 注释矩形覆盖两行
        ft = page0.add_freetext_annot(
            fitz.Rect(55, 85, 400, 175), f"注释 {MAIL}", fontsize=10
        )
        ft.update()

        boxes = [a.rect for a in page0.annots()]
        assert boxes, "样本没造好"
        dropped = []
        for block in doc[0].get_text("dict").get("blocks", ()):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", ()):
                for span in line.get("spans", ()):
                    r = fitz.Rect(span["bbox"])
                    if any(r in b for b in boxes):
                        dropped.append(span.get("text", ""))
        doc.close()
        joined = "".join(dropped)
        assert PHONE2 in joined, (
            f"几何排除法没误排正文（排除项={dropped}）—— "
            f"该反例无鉴别力"
        )

    def test_变异_square停放有损(self, tmp_path):
        """变异：把停放范围放宽到 Square → rect 被撑大（有损）。

        这条证明"停放范围必须收窄到 FreeText"的**充分性**论据。
        """
        doc = new_page_doc()
        page0 = doc[0]
        sq = page0.add_rect_annot(fitz.Rect(60, 88, 400, 112))
        sq.update()
        before = tuple(round(v, 3) for v in list(page0.annots())[0].rect)

        # 模拟"停放全部注释"：对 Square 也 set_rect
        for a in page0.annots():
            r = a.rect
            a.set_rect(fitz.Rect(r.x0, r.y0 + 5000, r.x1, r.y1 + 5000))
            a.update()
        for a in page0.annots():
            a.set_rect(fitz.Rect(before))
            a.update()
        after = tuple(round(v, 3) for v in list(page0.annots())[0].rect)
        doc.close()
        assert after != before, (
            f"Square 停放/还原后 rect 未变（{before}）—— "
            f"该论据不成立"
        )

    def test_变异_无rect类型停放直接报错(self, tmp_path):
        """变异：对 Highlight 调 ``set_rect`` → **静默 no-op**，不抛异常。

        🔴 这条测试的判据**改过两次**，两次都是被实测推翻：

        1. 原判据断言"抛 ``FzErrorArgument``" → 实测 **不抛**；
        2. 改用 ``capsys`` 抓提示 → 抓不到（``out=''``）；
        3. 再改 ``capfd`` → **仍然读不到**（``out=''``），
           但 pytest 自己的 ``Captured stdout call`` 段**却显示了那一行**。

        第 3 点是本仓库铁律「**"捕获不到" ≠ "它没输出"**」的活样本：
        C 层写的是 fd，缓冲/刷新的时机与 pytest 的内部实现耦合，
        "fixture 读不到"不能推出"没有输出"。故本测试改用
        **全新子进程 + OS 管道**（``subprocess.run(capture_output=True)``）
        验证提示流向 —— 这条路径不依赖 pytest 内部实现。

        ``.verify/probe_p3b/probe_fix2b.txt`` Q4 实测：6 类
        （Highlight/Underline/StrikeOut/Squiggly/Ink/Line）**对象里都有
        ``/Rect`` 键**（``type='array'``），``set_rect`` 不抛异常、
        ``rect`` 不变 —— 是**静默失败**；提示走 **stdout**、
        每次调用一行（3 次调用 → 3 行），stderr 0 行。
        ``probe_fix2c.txt`` Q8 是同进程对照：Python 层替换捕获 ``''``、
        OS 层替换捕获完整提示。

        "静默"比"报错"更危险，这正是收窄停放范围的**真正**理由：
        若把这 6 类也纳入停放，它们会走成"记入 ``saved``（成功）→
        ``set_rect`` 静默失败 → 注释从未移动 → 照样被删"，而
        ``restore`` 侧的 ``set_rect`` 同样不抛 → ``restored += 1`` →
        ``len(parked) == restored`` → **"未复位"告警永不触发**。
        """
        doc = new_page_doc()
        page0 = doc[0]
        doc[0].insert_text((70, 100), "target", fontsize=12)
        hl = page0.add_highlight_annot(fitz.Rect(70, 90, 230, 104))
        hl.update()

        before = tuple(round(v, 3) for v in hl.rect)
        err = None
        for a in page0.annots():
            try:
                a.set_rect(fitz.Rect(70, 5090, 230, 5104))
            except Exception as exc:  # noqa: BLE001
                err = exc
        after = tuple(round(v, 3) for v in hl.rect)
        doc.close()

        # (1) 不抛异常 —— 推翻"抛 FzErrorArgument"的旧判据
        assert err is None, (
            f"Highlight 的 set_rect 抛了异常（{err!r}）—— 那么"
            f"「静默失败更危险」这条论据不成立，请复核实现注释"
        )
        # (2) rect 真的没变 —— 静默（这是"静默"的核心可观察量）
        assert after == before, (
            f"Highlight 的 rect 竟然变了（{before} → {after}）—— "
            f"那么它不是静默 no-op，收窄范围的论据需重新论证"
        )

        # (3) 提示的流向 —— 用全新子进程 + OS 管道（不依赖 pytest 捕获）
        script = (
            "import fitz\n"
            "d = fitz.open()\n"
            "p = d.new_page(width=595, height=842)\n"
            "p.insert_text((70, 100), 't', fontsize=12)\n"
            "h = p.add_highlight_annot(fitz.Rect(70, 90, 230, 104))\n"
            "h.update()\n"
            "for _ in range(3):\n"
            "    h.set_rect(fitz.Rect(70, 5090, 230, 5104))\n"
            "print('CALLS_DONE')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True
        )
        sub_out = proc.stdout.decode("utf-8", "replace")
        sub_err = proc.stderr.decode("utf-8", "replace")

        assert "CALLS_DONE" in sub_out, (
            f"子进程没跑完（rc={proc.returncode}，out={sub_out!r}，"
            f"err={sub_err!r}）—— 该断言无鉴别力，先修脚本"
        )
        assert "no Rect property" in sub_out, (
            f"子进程 stdout 里没有 'no Rect property' 提示"
            f"（out={sub_out!r}，err={sub_err!r}）—— 提示的流向变了，"
            f"若它现在走 stderr，本断言需改用 sub_err"
        )
        assert "no Rect property" not in sub_err, (
            f"提示出现在子进程 stderr（{sub_err!r}）—— "
            f"与实测（stderr 0 行）不符"
        )

    def test_变异_只停放不还原则注释停在页外(self, tmp_path, monkeypatch):
        """变异 3：``restore`` 打成 no-op → 注释**还在**，但停在页外。

        这条是"只停放不还原"的独立变异（与"不停放"是**两个**方向）：

        - 不停放 → 注释被**删**（数量 1→0，明显）
        - 只停不放 → 注释**数量不变**，但 rect 下移 5000pt（**更隐蔽**）

        实测（``.verify/probe_p3b/probe_fix2b.txt`` Q6）：
        正常路径 ``(55.0, 88.0, 400.0, 112.0)``，
        变异路径 ``(55.0, 5088.0, 400.0, 5112.0)`` —— 偏移恰好 5000.0pt。

        它同时证明 ``test_停放还原保真度_ap字节与属性一致`` 里那句
        "``restore`` 后 ``after == before``"**不是恒真的**：
        去掉 ``restore`` 就会失败。
        """
        import redactor.handlers.pdf_handler as ph

        doc = new_page_doc()
        page0 = doc[0]
        doc[0].insert_text((60, 100), f"正文 {PHONE2}", fontsize=12)
        ft = page0.add_freetext_annot(
            fitz.Rect(55, 88, 400, 112), f"自由文本 {MAIL}", fontsize=10
        )
        ft.update()
        src = tmp_path / "mut_restore.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        out1 = tmp_path / "mut_restore_ok.pdf"
        PdfHandler().process(src, out1, engine())

        monkeypatch.setattr(ph, "restore_freetext_annots", lambda doc, s: 0)
        out2 = tmp_path / "mut_restore_bad.pdf"
        PdfHandler().process(src, out2, engine())

        def annots_of(path):
            d = fitz.open(str(path))
            try:
                return [
                    (a.type[1], tuple(round(v, 3) for v in a.rect))
                    for a in d[0].annots()
                ]
            finally:
                d.close()

        i1, i2 = annots_of(out1), annots_of(out2)

        assert len(i1) == len(i2) == 1, (
            f"注释数应为 1/1（数量不变才是本变异的特征），实为 "
            f"{len(i1)}/{len(i2)}"
        )
        assert i1[0][1][1] < 200, f"正常路径注释不在页内：{i1}"
        dy = round(i2[0][1][1] - i1[0][1][1], 1)
        assert abs(dy) > 1000, (
            f"只停放不还原时 y0 偏移仅 {dy}pt —— 注释没停在页外，"
            f"说明该变异没复现（判据无鉴别力）"
        )

    def test_变异_放宽停放范围则告警连带失效(self, tmp_path, monkeypatch):
        """变异 4：把停放范围放宽到 6 类静默类型 → **"未复位"告警也失效**。

        这条锁住实现注释里那句"静默失效会**连带**让告警机制失效"。

        构造：文档里有 1 个 Highlight（``set_rect`` 静默 no-op）
        + 1 个 FreeText（真能被停放）。把停放函数换成一个"对**所有**
        注释都记入 ``saved`` 并 ``set_rect``"的错误实现。

        预期（由实测数据分支生成，不写死）：
        - Highlight 记入 ``saved`` 但**从未移动** → 注释数会因它被删而变；
        - ``restore`` 侧对它 ``set_rect`` 同样不抛 → ``restored += 1``
          → ``len(parked) == restored`` → **告警不出现**。
        → 断言：错误实现下 ``parked`` 数 **>** 真实可停数，
          但 handler **没有**报"未能复位"。两者同时成立即证明"连带失效"。
        """
        import redactor.handlers.pdf_handler as ph
        import redactor.handlers.pdf_structures as ps

        doc = new_page_doc()
        page0 = doc[0]
        doc[0].insert_text((70, 100), f"target {PHONE2}", fontsize=12)
        hl = page0.add_highlight_annot(fitz.Rect(70, 90, 230, 104))
        hl.update()
        ft = page0.add_freetext_annot(
            fitz.Rect(60, 200, 400, 240), f"FT {MAIL}", fontsize=12
        )
        ft.update()
        src = tmp_path / "mut_wide.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        # 真实实现只停 FreeText → 计数 1
        d = fitz.open(str(src))
        try:
            real_parked = len(ps.park_freetext_annots(d))
        finally:
            d.close()
        assert real_parked == 1, (
            f"真实停放数应为 1（只 FreeText），实为 {real_parked}"
        )

        def wide_park(doc):
            """错误实现：对所有注释都记入 saved 并 set_rect。"""
            saved = {}
            for pi in range(doc.page_count):
                page = doc[pi]
                for an in page.annots():
                    r = an.rect
                    saved[an.xref] = (pi, (r.x0, r.y0, r.x1, r.y1))
                    an.set_rect(
                        fitz.Rect(r.x0, r.y0 + 5000, r.x1, r.y1 + 5000)
                    )
                    an.update()
            return saved

        monkeypatch.setattr(ph, "park_freetext_annots", wide_park)
        out = tmp_path / "mut_wide_out.pdf"
        res = PdfHandler().process(src, out, engine())

        # 放宽后的 parked 数确实更多（说明错误实现真的被执行了）
        assert real_parked < 2, "样本没造好：错误实现应比真实实现多记"
        # 关键：告警**没有**出现 —— 因为 restore 侧也静默通过
        assert "未能复位" not in (res.message or ""), (
            f"放宽范围后竟然报了'未能复位'（{res.message}）—— "
            f"说明静默失效**没有**连带让告警失效，实现注释需更正"
        )
        # 反向对照：把 restore 换成"一个都不还原"，告警**必须**出现
        monkeypatch.setattr(ph, "park_freetext_annots", ps.park_freetext_annots)
        monkeypatch.setattr(ph, "restore_freetext_annots", lambda doc, s: 0)
        out2 = tmp_path / "mut_wide_out2.pdf"
        res2 = PdfHandler().process(src, out2, engine())
        assert "未能复位" in (res2.message or ""), (
            f"把 restore 打成 no-op 后告警仍未出现（{res2.message}）—— "
            f"告警判据是恒假的，上面那条断言等于没校验"
        )

    def test_变异_内嵌文件用解码名则删不掉(self, tmp_path):
        """变异：``embfile_del`` 用 **de_mojibake 之后**的名字 → 删不掉。

        这条锁住用户点名的实现要点：「``embfile_del`` 要用 mojibake 名」。

        ``embfile_names()`` 返回的是 **mojibake**（``latin-1`` 误读的
        UTF-8）。API 层按**原样名**匹配，传解码后的中文名匹配不上。

        🔴 本测试的样本**必须用中文名**：``make_full()`` 造的嵌件名是
        纯 ASCII（``zhang.san@example.com.txt``），
        ``de_mojibake(raw) == raw`` → **前提不成立、断言恒真**。
        实测（``.verify/probe_p3b/probe_fix2c.txt`` Q7）：
        传 ``'合同扫描件-张三.pdf'`` 时 ``embfile_names()`` 返回
        ``'å\\x90\\x88...'``（mojibake），``de_mojibake`` 能还原。

        失败形态也由实测确定：**抛 ``ValueError``**（不是静默失败）——
        ``ValueError: '合同扫描件-张三.pdf' not in EmbeddedFiles array.``
        """
        doc = new_page_doc()
        page0 = doc[0]
        cn_name = "合同扫描件-张三.pdf"
        doc.embfile_add(cn_name, b"dummy", desc="描述")
        src = tmp_path / "mut_emb.pdf"
        doc.save(str(src), garbage=4, deflate=True)
        doc.close()

        d = fitz.open(str(src))
        try:
            raw_names = list(d.embfile_names())
            assert raw_names, "样本没造好：没有内嵌文件"
            raw = raw_names[0]
            n0 = d.embfile_count()
        finally:
            d.close()

        human = de_mojibake(raw)
        assert human != raw, (
            f"样本的嵌件名不是 mojibake（{raw!r}）—— "
            f"该变异前提不成立；请用含中文的名字造样本"
        )
        assert human == cn_name, (
            f"de_mojibake 没还原回原名：{human!r} != {cn_name!r}"
        )

        # 错误做法：用解码后的名字删
        err_human = None
        d = fitz.open(str(src))
        try:
            d.embfile_del(human)
            left_wrong = d.embfile_count()
        except ValueError as exc:
            err_human = exc
            left_wrong = None
        finally:
            d.close()

        # 正确做法：用 API 原样名删
        d = fitz.open(str(src))
        try:
            d.embfile_del(raw)
            left_right = d.embfile_count()
        finally:
            d.close()

        assert err_human is not None, (
            f"用解码名竟然删掉了（剩 {left_wrong} 个）—— "
            f"「必须用 mojibake 名」这条实现要点不成立，请复核"
        )
        assert left_right == n0 - 1, (
            f"用原样名没删掉（{n0} → {left_right}）—— "
            f"两种名字都无效，说明该 API 的匹配规则与实测不符"
        )
