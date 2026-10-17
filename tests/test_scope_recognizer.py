"""P7 范围脱敏 —— 识别器（反向扫描算法）测试。

分6 组：
    A. TestScanScopeBasics   算法基础（正向例 / 偏移 / 边界）
    B. TestNoFalsePositive   负例零误伤（默认平衡档）
    C. TestQ4NoDigitSwallow  ★Q4：命中区间绝不含数字（吞内置规则防线）
    D. TestTiers             三档严格度行为差异
    E. TestScopeRecognizer   识别器契约（Match / 占位符 / rule_name）
    F. TestReverseValidation ★反向验证：改成正向正则 → 测试必须红
"""
from __future__ import annotations

import re
from typing import List, Tuple

import pytest

from redactor.core.scopes import (
    HIGH_SUFFIXES,
    LOW_SUFFIXES,
    ScopeEntry,
    expand_suffixes,
)
from redactor.recognizers.scope_recognizer import (
    FUNC_CHARS,
    STOP_CHARS,
    ScopeRecognizer,
    build_scope_recognizer,
    scan_scope,
)


ALL_SUFFIXES = HIGH_SUFFIXES + LOW_SUFFIXES
SUF3 = tuple(expand_suffixes([ScopeEntry(name="单位", suffixes=ALL_SUFFIXES)]))


# ---------------------------------------------------------- 语料
POSITIVES: List[Tuple[str, str]] = [
    ("我们联系了重庆创锐电气技术有限公司", "重庆创锐电气技术有限公司"),
    ("北京某某科技公司", "北京某某科技公司"),
    ("华为技术有限公司", "华为技术有限公司"),
    ("重庆大学", "重庆大学"),
    ("西南政法大学", "西南政法大学"),
    ("重庆市污水处理技改项目", "重庆市污水处理技改项目"),
    ("中国移动重庆公司签订合同", "中国移动重庆公司"),
    ("甲方为重庆某某建筑工程有限公司", "重庆某某建筑工程有限公司"),
    ("本项目由重庆水务集团实施", "重庆水务集团"),
    ("重庆某某工程咨询有限公司", "重庆某某工程咨询有限公司"),
]

NEGATIVES: List[str] = [
    "他今天去公司了", "我在公司加班到很晚", "明天到公司开会",
    "这家公司实力不错", "去大学讲课", "这个项目很难做",
    "该项目已结题", "大家都在公司", "从公司回来", "在公司附近吃了饭",
    "关于项目的意见", "本项目已完成", "公司", "大学很好",
    "我们已经完成了项目验收工作", "该项目由我负责",
    "每个项目都要评审", "多家公司参与投标", "本次项目验收合格",
    "该公司已注销", "各大学响应号召", "该项目负责人",
]

OVERLAP: List[str] = [
    "联系人张三，电话13812345678，公司在重庆",
    "重庆创锐电气技术有限公司13812345678",
    "身份证110101199001011234，重庆大学",
    "项目编号2024ABC，公司名称重庆某某公司",
    "公司电话是13812345678，请拨打",
    "重庆大学的学生电话13812345678",
]


# ============================================================ A. 算法基础
class TestScanScopeBasics:
    def test_简单公司命中(self):
        r = scan_scope("北京某某科技公司", SUF3)
        assert len(r) == 1
        s, e, raw, suf = r[0]
        assert raw == "北京某某科技公司"
        assert suf == "公司"
        assert (s, e) == (0, 8)

    def test_偏移量指向原文(self):
        text = "前缀文字，北京某某科技公司，后缀"
        r = scan_scope(text, SUF3)
        assert len(r) == 1
        s, e, raw, _ = r[0]
        # ★ 偏移必须能直接切出原文（无任何文本变换）
        assert text[s:e] == raw == "北京某某科技公司"

    def test_标记有限公司优先于公司(self):
        """长后缀必须先匹配，否则"有限公司"会被切在"公司"处丢掉"有限"。"""
        r = scan_scope("重庆某某有限公司", SUF3)
        assert len(r) == 1
        assert r[0][2] == "重庆某某有限公司"
        assert r[0][3] == "有限公司"

    def test_多个实体依次命中(self):
        """逗号分隔的多个实体 —— 无歧义场景。"""
        text = "重庆大学、西南政法大学、重庆水务集团"
        r = scan_scope(text, SUF3)
        assert len(r) == 3
        assert [x[2] for x in r] == ["重庆大学", "西南政法大学", "重庆水务集团"]

    def test_多个实体紧邻_产出不重叠(self):
        """★ 无分隔符紧邻时也不能产出重叠命中（scan_floor 下界的作用）。"""
        r = scan_scope("重庆大学与西南政法大学合作", SUF3)
        assert len(r) == 2
        for k in range(len(r) - 1):
            assert r[k][1] <= r[k + 1][0], f"{r[k]} 与 {r[k+1]} 重叠"

    def test_单字实体名不命中(self):
        """裸"公司"没有实体信息（min_name_len=2），不该脱敏。"""
        assert scan_scope("公司", SUF3) == []
        assert scan_scope("这公司", SUF3) == []

    def test_空文本返回空(self):
        assert scan_scope("", SUF3) == []
        assert scan_scope("任意文字", ()) == []

    def test_超长实体名被截断到上限(self):
        long_name = "重庆" + "创锐电气技术" * 5 + "有限公司"
        r = scan_scope(long_name, SUF3)
        assert len(r) == 1
        assert len(r[0][2]) <= 20 + 4

    def test_命中区间不重叠(self):
        text = "重庆大学、西南政法大学、重庆水务集团"
        r = scan_scope(text, SUF3)
        for i in range(len(r) - 1):
            assert r[i][1] <= r[i + 1][0], f"{r[i]} 与 {r[i+1]} 重叠"


# ============================================================ B. 负例
class TestNoFalsePositive:
    def test_标记平衡档误伤不超过2(self):
        """实测基线：2/22（"这家公司""这个项目"，属真歧义不可消除）。"""
        hits = [(t, scan_scope(t, SUF3)) for t in NEGATIVES]
        bad = [(t, r[0][2]) for t, r in hits if r]
        assert len(bad) <= 2, f"误伤 {len(bad)}/22 超标：{bad}"

    def test_已知两处误伤就是歧义案例(self):
        """明确锁定这两处 —— 它们是已知且被接受的（预 preview 展示给用户）。"""
        assert scan_scope("这家公司实力不错", SUF3)
        assert scan_scope("这个项目很难做", SUF3)

    def test_动词介词句零误伤(self):
        for t in ["他今天去公司了", "我在公司加班到很晚", "明天到公司开会",
                  "从公司回来", "在公司附近吃了饭", "关于项目的意见"]:
            assert scan_scope(t, SUF3) == [], f"{t!r} 不该命中"

    def test_数量修饰句零误伤(self):
        """★这组是实现期发现的缺陷（FUNC_CHARS 漏了"每多次本"）的守护。"""
        for t in ["每个项目都要评审", "多家公司参与投标", "本次项目验收合格"]:
            assert scan_scope(t, SUF3) == [], f"{t!r} 不该命中"

    def test_保守档零误伤(self):
        cons = tuple(expand_suffixes(
            [ScopeEntry(name="单位", suffixes=ALL_SUFFIXES)], tier="conservative"
        ))
        bad = [t for t in NEGATIVES if scan_scope(t, cons)]
        assert bad == [], f"保守档应零误伤，实际 {bad}"


# ============================================================ C. Q4
class TestQ4NoDigitSwallow:
    """★Q4 防线：范围脱敏**绝不能**吞掉内置手机号/身份证规则。"""

    @pytest.mark.parametrize("text", OVERLAP)
    def test_命中区间绝不含数字(self, text: str):
        for _s, _e, raw, _suf in scan_scope(text, SUF3):
            assert not any(c.isdigit() for c in raw), \
                f"命中区间含数字 → 会吞掉内置规则：{raw!r} in {text!r}"

    def test_逗号挡住反向扫描(self):
        """'，' 是停止字符 → '电话13812345678，公司' 不该被连起来。"""
        assert scan_scope("联系人张三，电话13812345678，公司在重庆", SUF3) == []

    def test_实体名后接数字仍能命中(self):
        r = scan_scope("重庆创锐电气技术有限公司13812345678", SUF3)
        assert len(r) == 1
        assert r[0][2] == "重庆创锐电气技术有限公司"

    def test_标记与内置规则对比_正则会吞反向扫描不会(self):
        """把实测的"正则版"写进来当对照，证明反向扫描的必要性。"""
        text = "联系人张三，电话13812345678，公司在重庆"
        # 正则版（实测会吞掉手机号）
        #★ 必须是 [^\s] 这种"只排空白"的字符类 —— 排除数字字母的写法
        #        在数字处就断了，压根复现不出吞数字（已实测 0 处）。
        evil = re.compile(r"[^\s]{1,14}(公司|大学|项目)")
        evil_hits = [m.group() for m in evil.finditer(text)]
        # 反向扫描版（安全）
        good_hits = [x[2] for x in scan_scope(text, SUF3)]
        assert evil_hits, "对照：正则版在本例会误吞"
        assert any("13812345678" in h for h in evil_hits), \
            "对照失效：正则版竟然没吞数字，说明对照用例选错了"
        assert good_hits == [], "反向扫描不该命中（',' 挡住了）"

    def test_停止字符集覆盖数字字母标点(self):
        for ch in "0189azAZ,.，。!！?？:：;；（）()《》-":
            assert ch in STOP_CHARS, f"停止字符集漏了 {ch!r}"

    def test_功能字符集含数量修饰字(self):
        """★ 缺这组会让平衡档多误伤 3 处（每个项目/多家公司/本次项目）。"""
        for ch in "每多次本":
            assert ch in FUNC_CHARS, f"FUNC_CHARS 漏了 {ch!r}"


# ============================================================ D. 严格度分档
class TestTiers:
    def test_conservative只出HIGH(self):
        cons = expand_suffixes(
            [ScopeEntry(name="单位", suffixes=ALL_SUFFIXES)], tier="conservative"
        )
        got = {s for s, _, _ in cons}
        assert set(HIGH_SUFFIXES) <= got
        assert not (got & set(LOW_SUFFIXES))

    def test_balanced出全部(self):
        bal = expand_suffixes(
            [ScopeEntry(name="单位", suffixes=ALL_SUFFIXES)], tier="balanced"
        )
        got = {s for s, _, _ in bal}
        assert set(ALL_SUFFIXES) <= got

    def test_保守档不命中公司大学项目(self):
        cons = tuple(expand_suffixes(
            [ScopeEntry(name="单位", suffixes=ALL_SUFFIXES)], tier="conservative"
        ))
        assert scan_scope("北京某某科技公司", cons) == []
        assert scan_scope("重庆大学", cons) == []
        # 但 HIGH 后缀仍命中
        assert scan_scope("重庆水务集团", cons)

    def test_激进档启用首字黑名单(self):
        text = "这家公司实力不错"
        assert scan_scope(text, SUF3), "默认档应命中（已知歧义）"
        assert scan_scope(text, SUF3, strict_lead=True) == [], \
            "激进档首字黑名单应拦下'这家'"

    def test_激进档不误杀真实体(self):
        """★ 首字黑名单曾误杀"重庆水务集团"/"西南大学" —— 必须守住。"""
        for text, want in [("本项目由重庆水务集团实施", "重庆水务集团"),
                           ("重庆某某有限公司", "重庆某某有限公司")]:
            r = scan_scope(text, SUF3, strict_lead=True)
            assert r, f"{text!r} 在激进档被误杀了"
            assert want in r[0][2], f"激进档命中不完整：{r[0][2]!r}"


# ============================================================ E. 识别器契约
class TestScopeRecognizer:
    @pytest.fixture
    def rec(self):
        return ScopeRecognizer(
            [ScopeEntry(name="单位", suffixes=ALL_SUFFIXES)], tier="balanced"
        )

    def test_标记占位符按后缀不按组(self, rec):
        """★裁定 4：重庆大学 → [大学]，不是 [单位]。"""
        m = rec.find("重庆大学")[0]
        assert m.placeholder == "[大学]"
        assert m.rule_name == "大学"

    def test_不同后缀不同占位符(self, rec):
        m1 = rec.find("北京某某科技公司")[0]
        m2 = rec.find("重庆大学")[0]
        assert m1.placeholder == "[公司]"
        assert m2.placeholder == "[大学]"
        assert m1.placeholder != m2.placeholder

    def test_有限公司占位符(self, rec):
        m = rec.find("重庆某某有限公司")[0]
        assert m.placeholder == "[有限公司]"

    def test_match_偏移可切原文(self, rec):
        text = "前缀 重庆大学 后缀"
        for m in rec.find(text):
            assert text[m.start:m.end] == m.raw

    def test_空条目返回空(self):
        r = ScopeRecognizer([])
        assert r.is_empty()
        assert r.find("重庆大学") == []

    def test_全部禁用返回空(self):
        r = ScopeRecognizer([ScopeEntry(name="x", suffixes=("公司",), enabled=False)])
        assert r.is_empty()
        assert r.find("北京某某科技公司") == []

    def test_find_in_span_偏移换算(self, rec):
        got = rec.find_in_span("重庆大学", offset=100)
        assert got[0].start == 100
        assert got[0].end == 104

    def test_group_names_去重(self):
        r = ScopeRecognizer([
            ScopeEntry(name="甲组", suffixes=("公司", "集团")),
            ScopeEntry(name="乙组", suffixes=("大学",)),
        ])
        assert set(r.group_names) == {"甲组", "乙组"}

    def test_工厂函数等价(self):
        a = build_scope_recognizer([ScopeEntry(name="单位", suffixes=("公司",))])
        b = ScopeRecognizer([ScopeEntry(name="单位", suffixes=("公司",))])
        assert a.active_suffixes == b.active_suffixes
        assert a.find("北京某某科技公司") == b.find("北京某某科技公司")


# ============================================================ F. 反向验证
class TestReverseValidation:
    """★把"反向扫描"改成"正向正则"，守护测试必须红。"""

    @staticmethod
    def _positive_regex_version(text: str, suffixes) -> List[Tuple]:
        """变异版：用正则贪婪匹配整个实体名。

        ★ pattern 用 ``[^\\s]{1,14}`` 而不是 ``[一-龥]{2,14}`` —— 后者在
        数字处就断了，**复现不出吞数字**（已实测：0 处）。
        只有"排除空白但**不排除**数字/标点"的字符类才会跨数字，
        这才是真实危险的那一类正则。
        """
        if not suffixes:
            return []
        alts = "|".join(re.escape(s) for s, _, _ in sorted(
            suffixes, key=lambda t: -len(t[0])
        ))
        pat = rf"[^\s]{{1,14}}(?:{alts})"
        return [
            (m.start(), m.end(), m.group(), "regex")
            for m in re.finditer(pat, text)
        ]

    def test_标记反向扫描改正则_Q4守护测试会红(self):
        """对每条 OVERLAP 用例，正则变异版都必须吞数字，而真版不能。"""
        violations = []
        for text in OVERLAP:
            for _s, _e, raw, _suf in self._positive_regex_version(text, SUF3):
                if any(c.isdigit() for c in raw):
                    violations.append((text, raw))
        assert violations, (
            "❌ 对照失效：正则变异版竟然没吞数字，"
            "说明 OVERLAP 用例已不能区分两种实现"
        )
        # 真版（反向扫描）在同样 6 条用例上零吞数字
        for text in OVERLAP:
            for _s, _e, raw, _suf in scan_scope(text, SUF3):
                assert not any(c.isdigit() for c in raw)

    def test_标记反向扫描改正则_误伤守护测试会红(self):
        """正则版误伤必然远多于反向扫描版（实测 13/22 vs 2/22）。"""
        real_bad = sum(1 for t in NEGATIVES if scan_scope(t, SUF3))
        evil_bad = sum(1 for t in NEGATIVES if self._positive_regex_version(t, SUF3))
        assert real_bad <= 2, f"反向扫描版误伤应<=2，实际 {real_bad}"
        assert evil_bad > real_bad * 3, (
            f"❌ 对照失效：正则版误伤 {evil_bad} 未显著高于反向扫描版 {real_bad}"
        )

    def test_标记反向扫描改正则_区间长度守护测试会红(self):
        """正则版会**多吞**前缀（含动词/连接词），真版只取最小贴合。

        对 "我们联系了重庆创锐电气技术有限公司"：
            正则版可能吃到 "我们联系了重庆创锐电气技术有限公司"
            反向扫描版只吃 "重庆创锐电气技术有限公司"
        """
        overlong = []
        for text, _want in POSITIVES:
            evil = self._positive_regex_version(text, SUF3)
            good = scan_scope(text, SUF3)
            if not evil or not good:
                continue
            evil_len = len(evil[0][2])
            good_len = len(good[0][2])
            if evil_len > good_len:
                overlong.append((text, evil[0][2], good[0][2]))
        assert overlong, (
            "❌ 对照失效：正则变异版在全部正例上都没比真版长，"
            "说明正例语料已不能区分两种实现"
        )
        # 真版对带动词前缀的句子必须只取最小贴合
        r = scan_scope("我们联系了重庆创锐电气技术有限公司", SUF3)
        assert r and r[0][2] == "重庆创锐电气技术有限公司"

    def test_标记把停止字符集清空_防护测试会红(self, monkeypatch):
        """变异：STOP_CHARS 变空 → 数字不再阻止扫描 → 必吞手机号。"""
        from redactor.recognizers import scope_recognizer as mod
        monkeypatch.setattr(mod, "STOP_CHARS", frozenset())
        hits = scan_scope("联系人张三，电话13812345678，公司在重庆", SUF3)
        assert any(
            any(c.isdigit() for c in raw) for _s, _e, raw, _suf in hits
        ), "❌ 变异未生效：清空 STOP_CHARS 后竟然没吞数字"