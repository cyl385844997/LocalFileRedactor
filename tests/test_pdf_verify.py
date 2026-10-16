"""L3 字节流差分验证的测试。

**背景（这是本文件的全部意义）**：
原先的 L3 断言是「在输出文件的原始字节里搜明文，计数为 0 即 PASS」。
Marvis 指出它**在未脱敏的源文件上也会 PASS**，因此不构成任何删净证据
—— 属"空转断言"。

根因（``.verify/probe_l3_content.py`` 实测）：PDF 把文本存在内容流里，
且是**十六进制字形字节**形态，例如

    BT /helv 12 Tf [<b7b7203133383030313338303030>]TJ ET

明文 ``13800138000`` 在原始字节里**根本不存在**（脱敏与否都一样），
内容流默认还被 Flate 压缩。所以"原始字节计数为 0"恒真。

修法：解压内容流 + 解码其中的十六进制/字面字符串，得到「字形字节搜索域」，
然后在**源文件与输出文件上各搜一次**做差分。两者同时成立才算 PASS：

    源文件必须能检出   —— 证明搜索域有鉴别力（否则仍是空转）
    输出文件必须为 0   —— 这才是删净证据

本文件既测正向（真脱敏应 PASS），也测**反向对照**（未脱敏文件必须 FAIL）。
反向对照是关键：它证明这套断言**真的能失败**，不是恒真。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import fitz
import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.lexicon import load_lexicon_from_text
from redactor.core.rules import load_rules
from redactor.handlers.pdf_handler import PdfHandler
from redactor.handlers.pdf_verify import (
    ENCODINGS,
    ResidueCheck,
    decode_stream_strings,
    format_report,
    verify_residue,
)

BUILTIN = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"

PHONE = "13800138000"
EMAIL = "zhangsan@example.com"
COMPANY = "重庆某某水务"


def engine():
    rules = load_rules(BUILTIN)
    entries = load_lexicon_from_text(
        f"lexicon:\n  - name: 甲方单位\n    words: [{COMPANY}]\n",
        path=Path("<t>"),
    )
    return RedactionEngine(rules, lexicon_entries=entries)


def make_pdf(path: Path, *, fontname: str = "china-s") -> Path:
    """合成一份含手机号/邮箱/单位名的 PDF。

    **必须用 ``china-s``**：``helv`` 没有 CJK 字形，中文会被写成 ``··``
    （实测 ``.verify/probe_cjk_font.py``），中文串在搜索域里根本不存在，
    差分验证会因"源检不出"而失败——这是**字体问题**，不是脱敏问题。

    ``fontname`` 仍保留可配，用于验证不同字体下明文的编码形态差异：
        china-s -> UTF-16 形态（内置 CJK 字体按双字节编码）
        helv    -> UTF-8/GBK 形态（ASCII 单字节）
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 100), f"甲方{COMPANY}", fontsize=12, fontname=fontname)
    page.insert_text((60, 130), f"电话 {PHONE}", fontsize=12, fontname=fontname)
    page.insert_text((60, 160), f"邮箱 {EMAIL}", fontsize=12, fontname=fontname)
    doc.save(str(path))
    doc.close()
    return path


def make_ascii_pdf(path: Path) -> Path:
    """只含 ASCII 内容的 PDF（用 ``helv``），用于 UTF-8/GBK 形态的验证。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 130), f"phone {PHONE}", fontsize=12, fontname="helv")
    page.insert_text((60, 160), f"mail {EMAIL}", fontsize=12, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def processed(tmp_path):
    """返回 ``(src, out)``：源文件与真正脱敏后的输出。"""
    src = make_pdf(tmp_path / "src.pdf")
    out = tmp_path / "out.pdf"
    PdfHandler().process(src, out, engine())
    return src, out


# ============================================================== 正向
class TestPositive:
    """真脱敏的输出：源可检出、输出为 0。"""

    def test_手机号差分通过(self, processed):
        src, out = processed
        check = verify_residue(src, out, [PHONE])[0]
        assert check.detectable_in_source, (
            f"源文件检不出 {PHONE} —— 搜索域无鉴别力，断言会空转。"
            f"源计数={check.source_counts}"
        )
        assert not check.residue_in_output, (
            f"输出仍有残留：{check.output_counts} @ 对象 {check.output_xrefs}"
        )
        assert check.ok

    def test_邮箱差分通过(self, processed):
        src, out = processed
        check = verify_residue(src, out, [EMAIL])[0]
        assert check.ok, check.describe()

    def test_词库单位名差分通过(self, processed):
        src, out = processed
        check = verify_residue(src, out, [COMPANY])[0]
        assert check.ok, check.describe()

    def test_多串一次验证(self, processed):
        src, out = processed
        checks = verify_residue(src, out, [PHONE, EMAIL, COMPANY])
        assert all(c.ok for c in checks), format_report(checks)

    def test_中文内置字体样本也通过(self, tmp_path):
        """china-s 字体让明文以 UTF-16 形态落盘，必须也能检出。"""
        src = make_pdf(tmp_path / "cn.pdf")
        out = tmp_path / "cn_out.pdf"
        PdfHandler().process(src, out, engine())
        check = verify_residue(src, out, [PHONE])[0]
        assert check.detectable_in_source, (
            f"UTF-16 形态未被检出，源计数={check.source_counts}"
        )
        assert check.ok, check.describe()

    def test_ascii字体样本也通过(self, tmp_path):
        """helv 字体让明文以 UTF-8/GBK 形态落盘，必须也能检出。

        只测一种字体编码会漏掉另一类——编码维度必须都覆盖。
        """
        src = make_ascii_pdf(tmp_path / "ascii.pdf")
        out = tmp_path / "ascii_out.pdf"
        PdfHandler().process(src, out, engine())
        check = verify_residue(src, out, [PHONE, EMAIL])[0]
        assert check.detectable_in_source, (
            f"UTF-8/GBK 形态未被检出，源计数={check.source_counts}"
        )
        assert check.ok, check.describe()


# ============================================================== 反向对照
class TestNegativeControl:
    """**本组是整个 L3 断言有鉴别力的证明**。

    若这些用例失败，说明 verify_residue 退化成了恒真断言。
    """

    def test_未脱敏文件必须被判残留(self, tmp_path):
        """把源文件当"输出"传进去 —— 必须报残留（证明断言能失败）。"""
        src = make_pdf(tmp_path / "src.pdf")
        fake_out = tmp_path / "fake_out.pdf"
        shutil.copyfile(src, fake_out)

        check = verify_residue(src, fake_out, [PHONE])[0]
        assert check.detectable_in_source, "源应能检出"
        assert check.residue_in_output, (
            "未脱敏文件竟被判为无残留 —— 断言在空转！"
        )
        assert not check.ok, "未脱敏文件不得通过"
        assert check.output_xrefs, "应给出残留所在的对象号"

    def test_仅删一半的中间产物必须被判残留(self, tmp_path):
        """只删手机号、留着邮箱 —— 邮箱必须被判残留。"""
        src = make_pdf(tmp_path / "src.pdf")
        partial = tmp_path / "partial.pdf"

        doc = fitz.open(str(src))
        page = doc[0]
        # 只涂掉手机号，邮箱不动
        for rect in page.search_for(PHONE):
            page.add_redact_annot(rect)
        page.apply_redactions()
        doc.save(str(partial), garbage=4, deflate=True)
        doc.close()

        phone = verify_residue(src, partial, [PHONE])[0]
        email = verify_residue(src, partial, [EMAIL])[0]
        assert phone.ok, f"手机号已删，应判通过：{phone.describe()}"
        assert not email.ok, (
            f"邮箱未删，必须判残留 —— 否则断言检不出漏删。"
            f"{email.describe()}"
        )

    def test_无鉴别力的搜索串必须被拒绝(self, processed):
        """源文件也搜不到的串 -> 不得算 PASS。

        这正是原 L3 的病根：拿一个"源文件也没有"的东西去断言"输出也没有"。
        """
        src, out = processed
        check = verify_residue(src, out, ["绝不存在的内容XYZ123"])[0]
        assert not check.detectable_in_source
        assert not check.ok, (
            "源文件都检不出的串竟判 PASS —— 这就是空转断言"
        )
        assert "无鉴别力" in check.describe()

    def test_原始字节域确实无鉴别力(self, processed):
        """把病根本身固化成断言：原始字节搜不到明文，故旧 L3 恒真。"""
        src, out = processed
        for needle in (PHONE, EMAIL):
            for enc in ENCODINGS:
                raw_hits = src.read_bytes().count(needle.encode(enc))
                assert raw_hits == 0, (
                    f"源文件原始字节竟含 {needle!r}({enc}) —— "
                    f"若如此则旧 L3 尚有鉴别力，本结论需修正"
                )


# ============================================================== 搜索域
class TestSearchDomain:
    """搜索域构造本身的测试。"""

    def test_解码域含明文(self, tmp_path):
        """hex 字符串解码后应能直接看到明文（人眼可复核）。

        **编码不固定**：china-s 下是 UTF-16，helv 下是 UTF-8/GBK。
        故按多种编码任一命中即算通过——这正是 ENCODINGS 存在的理由。
        """
        src = make_pdf(tmp_path / "s.pdf")
        blob = decode_stream_strings(src)
        hit = [enc for enc in ENCODINGS if PHONE.encode(enc) in blob]
        assert hit, (
            f"解码域应含 {PHONE} 的字节形态（任一编码），"
            f"实际前 200 字节={blob[:200]!r}"
        )

    def test_输出解码域不含明文(self, processed):
        _, out = processed
        blob = decode_stream_strings(out)
        for enc in ENCODINGS:
            assert PHONE.encode(enc) not in blob, (
                f"{PHONE} 以 {enc} 仍留在内容流解码域中"
            )

    def test_源与输出搜索域都非空(self, processed):
        """搜索域为空会让所有断言自动通过 —— 必须防这个退化。"""
        src, out = processed
        assert decode_stream_strings(src), "源搜索域为空，断言无意义"
        assert decode_stream_strings(out), "输出搜索域为空，断言无意义"

    def test_字体程序流被排除(self, processed):
        """字体子集可能恰好含目标字节，属误报源，须排除。

        排除后搜索域应仍能检出明文（说明排除没把文本流一起排掉）。
        """
        src, _ = processed
        blob = decode_stream_strings(src)
        assert any(PHONE.encode(enc) in blob for enc in ENCODINGS), (
            f"排除字体流后明文应仍在，实际前 200 字节={blob[:200]!r}"
        )

    def test_helv写中文会丢字_故须用china_s(self, tmp_path):
        """把「字体陷阱」固化成断言，防止 fixture 退回 helv。

        ``helv`` 无 CJK 字形，中文落成 ``··``，于是中文串在搜索域里
        根本不存在——差分验证会报"源检不出"。这不是脱敏失败，
        是**样本本身没写进去**。故中文样本必须用 ``china-s``。
        """
        src = make_pdf(tmp_path / "helv_cn.pdf", fontname="helv")
        blob = decode_stream_strings(src)
        assert COMPANY.encode("utf-8") not in blob, (
            "helv 竟然写出了中文？本断言的前提已变，需重新核对字体行为"
        )
        text = fitz.open(str(src))[0].get_text()
        assert COMPANY not in text, (
            f"helv 字体下中文应丢失，实际读到 {text!r}"
        )

    def test_china_s写中文不丢字(self, tmp_path):
        """对照组：china-s 下中文必须完整可读，搜索域可检出。"""
        src = make_pdf(tmp_path / "cns_cn.pdf", fontname="china-s")
        blob = decode_stream_strings(src)
        assert COMPANY.encode("utf-16-be") in blob, (
            f"china-s 下中文应以 UTF-16-BE 形态落盘，"
            f"实际搜索域前 120 字节={blob[:120]!r}"
        )
        text = fitz.open(str(src))[0].get_text()
        assert COMPANY in text


# ============================================================== 报告
class TestResidueCheck:
    """ResidueCheck 的语义。"""

    def test_ok需同时满足两个条件(self):
        only_src = ResidueCheck(PHONE, {"utf-8": 1}, {"utf-8": 0})
        assert only_src.ok

        both = ResidueCheck(PHONE, {"utf-8": 1}, {"utf-8": 1})
        assert not both.ok, "输出有残留不得通过"

        neither = ResidueCheck(PHONE, {"utf-8": 0}, {"utf-8": 0})
        assert not neither.ok, "源无鉴别力不得通过"

    def test_describe三种情形可区分(self):
        assert "无鉴别力" in ResidueCheck(
            PHONE, {"utf-8": 0}, {"utf-8": 0}).describe()
        assert "残留" in ResidueCheck(
            PHONE, {"utf-8": 1}, {"utf-8": 1}).describe()
        assert "PASS" in ResidueCheck(
            PHONE, {"utf-8": 1}, {"utf-8": 0}).describe()

    def test_format_report汇总失败项(self):
        checks = [
            ResidueCheck(PHONE, {"utf-8": 1}, {"utf-8": 0}),
            ResidueCheck(EMAIL, {"utf-8": 1}, {"utf-8": 1}),
        ]
        report = format_report(checks)
        assert "1 项未通过" in report
        assert EMAIL in report

    def test_报告文本不含其他串的原文(self):
        """报告只应出现被检查的串，不得夹带未检查的敏感内容。"""
        checks = [ResidueCheck(PHONE, {"utf-8": 1}, {"utf-8": 0})]
        report = format_report(checks)
        assert EMAIL not in report


# ============================================================== 已知缺口
class TestKnownGapInfoMetadata:
    """**已知产品级缺口**：L3 不覆盖 ``/Info`` 元数据。

    本组用例把缺口**固化成可执行的记录**，而不是只写在注释里：

    - 用 ``xfail(strict=True)`` 标注。一旦后续轮次修好元数据清洗，
      这些用例会变成 ``XPASS``，``strict=True`` 会让它**报错**——
      从而强制把这里的断言和文档一起改掉，不会留下过期说明。

    缺口来源：``pdf_verify`` 只搜内容流（``doc.xref_stream``），
    而 ``/Info`` 是**普通字符串对象、不是流**，完全不在搜索域内。
    """

    @pytest.mark.xfail(
        strict=True,
        reason="已知缺口：PDF /Info 元数据未清洗（自 P1.1 起，列入后续轮次）",
    )
    def test_元数据里的手机号应被清洗_当前未实现(self, tmp_path):
        """脱敏后元数据不应再含敏感串。当前会失败——这就是缺口本身。"""
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 130), f"电话 {PHONE}", fontsize=12,
                         fontname="helv")
        doc.set_metadata({
            "title": f"报告 {PHONE}",
            "author": f"张三 {PHONE}",
            "subject": f"主题 {PHONE}",
            "keywords": f"关键词 {PHONE}",
            "creator": f"创建者 {PHONE}",
        })
        src = tmp_path / "meta.pdf"
        doc.save(str(src))
        doc.close()

        out = tmp_path / "meta_out.pdf"
        PdfHandler().process(src, out, engine())

        after = fitz.open(str(out)).metadata
        leaked = {k: v for k, v in after.items() if v and PHONE in v}
        assert not leaked, (
            f"元数据仍含手机号：{leaked} —— 这正是待修的缺口"
        )

    def test_L3对元数据缺口判PASS_把盲区记录在案(self, tmp_path):
        """**这条必须 PASS**：它记录"L3 会漏判元数据"这一事实。

        若哪天它失败了，说明 L3 的搜索域已经覆盖到元数据——
        那时上面的 xfail 应随之转为正式断言，本文档也要改。
        """
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 130), f"电话 {PHONE}", fontsize=12,
                         fontname="helv")
        doc.set_metadata({"title": f"报告 {PHONE}"})
        src = tmp_path / "meta2.pdf"
        doc.save(str(src))
        doc.close()

        out = tmp_path / "meta2_out.pdf"
        PdfHandler().process(src, out, engine())

        check = verify_residue(src, out, [PHONE])[0]
        still_in_meta = PHONE in fitz.open(str(out)).metadata.get("title", "")
        assert still_in_meta, (
            "元数据里的手机号竟然没了？缺口可能已被修，"
            "请把 xfail 用例转正并更新文档"
        )
        assert check.ok, (
            "L3 对含元数据残留的文件判 PASS —— 这是已知盲区，"
            "记录在案以免被误当作'整份文件已删净'"
        )

    def test_缺口已写进模块文档(self):
        """局限必须写在模块 docstring 里 —— 防止"代码知道、文档不说"。

        取证对象是**模块文档字符串的实际文本**（不是生成条件），
        故不是重言式。
        """
        import redactor.handlers.pdf_verify as mod

        doc = mod.__doc__ or ""
        assert "不覆盖 /Info 元数据" in doc, (
            "pdf_verify 的局限段未写明不覆盖 /Info 元数据"
        )
