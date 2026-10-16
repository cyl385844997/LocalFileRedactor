"""词库识别器（Aho-Corasick 备用路径 + 逐词 find）的单元测试。

重点覆盖：
- 归一化后的偏移量必须能对齐回原文（整个设计的地基）
- ``Match.raw`` 必须来自原文而非归一化文本
- AC 自动机与逐词 find 结果必须一致（含重叠命中与后缀继承）
- 词库与内置正则的优先级行为
"""

from __future__ import annotations

from pathlib import Path

import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.lexicon import LexiconEntry, load_lexicon_from_text, normalize_word
from redactor.core.models import Match
from redactor.core.rules import load_rules
from redactor.recognizers.lexicon_recognizer import (
    LexiconRecognizer,
    MultiEntryLexiconRecognizer,
    build_automaton,
    build_lexicon_recognizer,
    scan,
)

BUILTIN = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


def entries_from(text: str):
    return load_lexicon_from_text(text, path=Path("<test>"))


def entry(name: str, *words: str) -> LexiconEntry:
    """便捷构造：words 已归一化。"""
    return LexiconEntry(
        name=name,
        words=tuple(words),
        normalized=tuple(normalize_word(w) for w in words),
    )


def ac_hits_for(text: str, entries):
    """用 AC 自动机扫描归一化文本，返回 set((start, end))。"""
    auto = build_automaton(entries)
    return {(s, e) for s, e, _n in scan(auto, normalize_word(text))}


def find_hits_for(text: str, entries):
    """用各条目find() 的命中，返回 set((start, end))。"""
    out = set()
    for ent in entries:
        for m in LexiconRecognizer(ent).find(text):
            out.add((m.start, m.end))
    return out


def multi_hits_for(text: str, entries):
    """用共用自动机的生产实现扫描，返回 set((start, end))。"""
    return {(m.start, m.end) for m in build_lexicon_recognizer(entries).find(text)}


def lexicon_yaml(spec: list) -> str:
    """由 [(条目名, [词, ...]), ...] 拼出词库 YAML 文本。

    单独抽出来是因为多处测试都要构造 YAML，用统一的拼接方式可避免
    手写缩进出错（本次就因拼 YAML 的缩进出过一次问题）。
    """
    lines = ["lexicon:"]
    for name, words in spec:
        lines.append("  - name: %s" % name)
        lines.append("    words: [%s]" % ", ".join(repr(w) for w in words))
    return "\n".join(lines) + "\n"


# ============================================================== 共用自动机架构
class TestSharedAutomaton:
    """架构保证：所有条目共用一台自动机，一次扫描。"""

    def test_只造一个识别器而非每条目一个(self):
        """**核心架构约束**：若每条目造一个识别器，文本仍被扫 N 遍，
        只省了构建，复杂度几乎不变——等于没改。"""
        from redactor.core.engine import RedactionEngine

        entries = entries_from(
            lexicon_yaml([("A", ["甲"]), ("B", ["乙"]), ("C", ["丙"])])
        )
        rules = load_rules(BUILTIN)
        engine = RedactionEngine(rules, lexicon_entries=entries)
        lexicon_recognizers = [
            r for r in engine._recognizers if isinstance(r, MultiEntryLexiconRecognizer)
        ]
        assert len(lexicon_recognizers) == 1, (
            "词库识别器必须只有 1 个（所有条目共用），"
            f"实际 {len(lexicon_recognizers)} 个 —— 架构被破坏"
        )
        assert len(engine._recognizers) == len(rules) + 1

    def test_一次扫描产出全部条目命中(self):
        """单个识别器的 find() 必须返回所有条目的命中（靠 rule_name 分组）。"""
        entries = entries_from(
            lexicon_yaml([("A", ["甲公司"]), ("B", ["乙公司"]), ("C", ["丙公司"])])
        )
        matches = build_lexicon_recognizer(entries).find("甲公司、乙公司、丙公司")
        assert {m.rule_name for m in matches} == {"A", "B", "C"}

    def test_禁用条目不命中(self):
        text = lexicon_yaml([("A", ["甲"]), ("B", ["乙"])])
        text += "    enabled: false\n"
        entries = entries_from(text)
        matches = build_lexicon_recognizer(entries).find("甲乙")
        assert {m.rule_name for m in matches} == {"A"}

    def test_全禁用时engine不加词库识别器(self):
        from redactor.core.engine import RedactionEngine

        entries = entries_from(lexicon_yaml([("A", ["甲"])]))
        disabled = entries_from(lexicon_yaml([("A", ["甲"])]) + "    enabled: false\n")
        engine = RedactionEngine(load_rules(BUILTIN), lexicon_entries=disabled)
        assert not any(
            isinstance(r, MultiEntryLexiconRecognizer) for r in engine._recognizers
        )
        assert len(entries) == 1  # 禁用条目仍会被加载，只是不匹配

    def test_entry_names列出覆盖条目(self):
        entries = entries_from(lexicon_yaml([("A", ["甲"]), ("B", ["乙"])]))
        rec = build_lexicon_recognizer(entries)
        assert set(rec.entry_names) == {"A", "B"}

    def test_空条目列表安全(self):
        rec = build_lexicon_recognizer([])
        assert rec.find("任何文本") == []

    @pytest.mark.parametrize(
        "spec,text",
        [
            ([("A", ["甲公司"]), ("B", ["乙公司"])], "甲公司与乙公司"),
            ([("A", ["甲"]), ("B", ["甲公司"])], "甲甲公司"),
            ([("A", ["测试敏感词000", "测试敏感词"]), ("B", ["000"])],
             "联系测试敏感词000，编号000"),
            ([("A", ["x"]), ("B", ["y"]), ("C", ["z"])], "xyz"),
            ([("A", ["重庆　某某"]), ("B", ["ＡＢＣ"])], "重庆 某某与ABC"),
        ],
    )
    def test_共用自动机与逐词一致(self, spec, text):
        """生产实现必须与逐词实现产出完全相同的命中集合。"""
        entries = entries_from(lexicon_yaml(spec))
        assert multi_hits_for(text, entries) == find_hits_for(text, entries)

    def test_共用自动机raw来自原文(self):
        entries = entries_from(lexicon_yaml([("外资", ["Ａｃｍｅ Corp"])]))
        text = "外方为Ａｃｍｅ Corp。"
        matches = build_lexicon_recognizer(entries).find(text)
        assert len(matches) == 1
        assert matches[0].raw == "Ａｃｍｅ Corp"
        assert text[matches[0].start:matches[0].end] == matches[0].raw

    def test_占位符来自所属条目(self):
        entries = entries_from(
            lexicon_yaml([("甲方单位", ["甲公司"]), ("关联公司", ["乙公司"])])
        )
        matches = build_lexicon_recognizer(entries).find("甲公司与乙公司")
        ph = {m.rule_name: m.placeholder for m in matches}
        assert ph == {"甲方单位": "[甲方单位]", "关联公司": "[关联公司]"}


# ============================================================== 归一化地基
class TestOffsetAlignment:
    """归一化文本的偏移量必须能直接用于切原文。"""

    @pytest.mark.parametrize(
        "text",
        ["ＡＢＣ公司", "重庆　某某科技", "ＡＢＣＤ", "正常文本", "①甲公司"],
    )
    def test_归一化保持长度(self, text):
        assert len(normalize_word(text)) == len(text)

    def test_归一化位置可切原文且等长(self):
        text = "重庆　某某科技受托"
        word = "重庆 某某科技"
        norm = normalize_word(text)
        pos = norm.find(word)
        assert pos >= 0
        assert len(text[pos:pos + len(word)]) == len(word), "偏移必须对齐"

    def test_raw来自原文而非归一化文本(self):
        """全角形态的原文，raw 必须保留全角。"""
        text = "外方为Ａｃｍｅ Corp。"
        ent = entry("外资单位", "Acme Corp")
        matches = LexiconRecognizer(ent).find(text)
        assert len(matches) == 1
        m = matches[0]
        assert m.raw == "Ａｃｍｅ Corp", "raw 必须取自原文"
        assert m.raw != "Acme Corp"

    def test_find内部断言长度一致(self):
        """若归一化破坏了长度，find() 内的 assert 会触发（地基被破坏）。"""
        ent = entry("A", "甲公司")
        # 正常情况不应抛异常
        assert LexiconRecognizer(ent).find("这里是甲公司啊") != []


# ============================================================== 逐词 find
class TestLexiconRecognizer:
    """LexiconRecognizer 基本行为。"""

    def test_命中基本词条(self):
        ent = entry("甲方单位", "重庆某某水务")
        ms = LexiconRecognizer(ent).find("本项目甲方为重庆某某水务。")
        assert len(ms) == 1
        assert ms[0].rule_name == "甲方单位"
        assert ms[0].placeholder == "[甲方单位]"
        assert ms[0].raw == "重庆某某水务"

    def test_多次出现全部命中(self):
        ent = entry("甲方单位", "某某水务")
        ms = LexiconRecognizer(ent).find("某某水务与某某水务")
        assert len(ms) == 2

    def test_重叠命中(self):
        """'甲公司' 与 '甲公司分公司' 在同一处文本上都应命中。"""
        ent = entry("A", "甲公司", "甲公司分公司")
        ms = LexiconRecognizer(ent).find("甲公司分公司")
        spans = {(m.start, m.end) for m in ms}
        assert (0, 3) in spans and (0, 6) in spans

    def test_空文本返回空(self):
        assert LexiconRecognizer(entry("A", "甲")).find("") == []

    def test_禁用条目不命中(self):
        ent = LexiconEntry(name="A", words=("甲",), normalized=("甲",), enabled=False)
        assert LexiconRecognizer(ent).find("甲公司") == []

    def test_空词条不命中(self):
        assert LexiconRecognizer(entry("A")).find("任何文本") == []

    def test_构造工厂接受条目列表(self):
        """工厂函数接受**条目列表**并返回共用自动机识别器。"""
        rec = build_lexicon_recognizer([entry("A", "甲")])
        assert isinstance(rec, MultiEntryLexiconRecognizer)
        assert rec.entry_names == ("A",)


# ============================================================== AC 自动机
class TestAutomaton:
    """AC 自动机（备用路径）的正确性。"""

    def test_空条目集得空树(self):
        auto = build_automaton([])
        assert auto.children == {}
        assert scan(auto, "任何文本") == []

    def test_后缀继承(self):
        """'测试敏感词' 是 '测试敏感词000' 的后缀，两处都应命中。"""
        ents = entries_from(
            "lexicon:\n  - name: A\n    words: ['测试敏感词000', '测试敏感词']\n"
        )
        text = "请联系测试敏感词000。"
        hits = ac_hits_for(text, ents)
        assert (3, 8) in hits, "短词应命中"
        assert (3, 11) in hits, "长词应命中"

    def test_与逐词find结果一致(self):
        """这是 AC 正确性的核心判据：两法命中集必须完全相同。"""
        ents = entries_from(
            "lexicon:\n"
            "  - name: A\n"
            "    words: ['测试敏感词000', '测试敏感词001', '测试敏感词', '000']\n"
        )
        text = "请联系测试敏感词000与测试敏感词001，编号000。"
        assert ac_hits_for(text, ents) == find_hits_for(text, ents)

    @pytest.mark.parametrize(
        "words,text",
        [
            (["甲公司"], "甲公司与甲公司"),
            (["a", "b"], "abab"),
            (["甲"], "甲甲甲"),
            (["测试敏感词000", "测试敏感词"], "测试敏感词000"),
            (["xyz", "y", "yz", "z"], "xyz"),
            (["甲公司", "甲"], "甲公司"),
        ],
    )
    def test_多种组合下两法一致(self, words, text):
        ents = entries_from(
            "lexicon:\n  - name: A\n    words: [%s]\n"
            % ", ".join(repr(w) for w in words)
        )
        assert ac_hits_for(text, ents) == find_hits_for(text, ents)

    def test_多条目多词全部一致(self):
        ents = entries_from(
            "lexicon:\n"
            "  - name: A\n"
            "    words: ['甲公司', '乙公司']\n"
            "  - name: B\n"
            "    words: ['甲', '乙']\n"
        )
        text = "甲公司与乙公司都由甲和乙承办"
        assert ac_hits_for(text, ents) == find_hits_for(text, ents)

    def test_禁用条目不入自动机(self):
        ents = entries_from(
            "lexicon:\n  - name: A\n    words: [甲公司]\n    enabled: false\n"
        )
        auto = build_automaton(ents)
        assert scan(auto, normalize_word("甲公司")) == []


# ============================================================== 引擎接入
class TestEngineIntegration:
    """engine._recognizers 接入后的行为。"""

    def _rules(self):
        return load_rules(BUILTIN)

    def test_识别器数量为内置加一(self):
        """词库只造 1 个识别器（所有条目共用），故总数 = 内置 + 1。"""
        ents = entries_from(
            "lexicon:\n"
            "  - name: A\n    words: [甲]\n"
            "  - name: B\n    words: [乙]\n"
            "  - name: C\n    words: [丙]\n    enabled: false\n"
        )
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        assert len(engine._recognizers) == len(self._rules()) + 1

    def test_词库识别器在末尾(self):
        """必须追加在末尾，这样 priority 才是最低。"""
        ents = entries_from("lexicon:\n  - name: Z\n    words: [甲]\n")
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        recognizers = engine._recognizers
        assert isinstance(recognizers[-1], MultiEntryLexiconRecognizer)
        assert not isinstance(recognizers[0], MultiEntryLexiconRecognizer)

    def test_词库与正则同时命中(self):
        ents = entries_from("lexicon:\n  - name: 甲方\n    words: [重庆某某水务]\n")
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        red, ms = engine.redact("甲方重庆某某水务，电话13800138000。")
        assert "重庆某某水务" not in red
        assert "13800138000" not in red
        names = {m.rule_name for m in ms}
        assert names == {"甲方", "手机号"}

    def test_占位符为条目名(self):
        ents = entries_from("lexicon:\n  - name: 甲方单位\n    words: [某某公司]\n")
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        red, _ = engine.redact("甲方为某某公司。")
        assert red == "甲方为[甲方单位]。"

    def test_同条目多个词同一占位符(self):
        """同一类别下的多个词统一脱敏为 [类别名]，不编号。"""
        ents = entries_from(
            "lexicon:\n  - name: 甲方\n    words: [甲公司, 乙公司]\n"
        )
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        red, _ = engine.redact("甲公司与乙公司")
        assert red == "[甲方]与[甲方]"
        assert "#" not in red, "首版不支持编号"

    def test_词库为空行为不变(self):
        rules = self._rules()
        text = "电话13800138000，邮箱a@example.com。"
        assert RedactionEngine(rules).redact(text)[0] == \
            RedactionEngine(rules, lexicon_entries=[]).redact(text)[0]

    def test_aggregate统计含词库(self):
        ents = entries_from("lexicon:\n  - name: 甲方\n    words: [某某公司]\n")
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        _, ms = engine.redact("某某公司与某某公司，电话13800138000")
        stats = {s.rule_name: s for s in RedactionEngine.aggregate(ms)}
        assert stats["甲方"].count == 2
        assert stats["甲方"].placeholder == "[甲方]"
        assert stats["手机号"].count == 1

    def test_全角词条匹配半角文本(self):
        ents = entries_from(
            "lexicon:\n  - name: 外资\n    words: ['Ａｃｍｅ Corp']\n"
        )
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        red, ms = engine.redact("外方为Acme Corp")
        assert red == "外方为[外资]"
        assert ms[0].raw == "Acme Corp"

    def test_表意空格词条匹配半角空格文本(self):
        ents = entries_from(
            "lexicon:\n  - name: 科技\n    words: ['重庆　某某']\n"
        )
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        red, _ = engine.redact("甲方为重庆 某某")
        assert red == "甲方为[科技]"

    def test_词库长词条吃掉起点靠后的正则(self):
        """已知机制：start 升序先到先得，词库起点更靠前时会赢。"""
        ents = entries_from(
            "lexicon:\n  - name: 整句\n    words: ['联系人张三13800138000']\n"
        )
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        red, ms = engine.redact("联系人张三13800138000")
        assert red == "[整句]"
        assert [m.rule_name for m in ms] == ["整句"]

    def test_同起点冲突时词库不压过正则(self):
        """priority 只在同起点生效；词库不进 _priority，故拿到最大值垫底。"""
        ents = entries_from("lexicon:\n  - name: 甲公司\n    words: ['13800138000']\n")
        engine = RedactionEngine(self._rules(), lexicon_entries=ents)
        # 词条与手机号规则在同一起点(0)命中，priority: 手机号=0 < 词库=9999
        red, ms = engine.redact("13800138000")
        names = {m.rule_name for m in ms}
        assert "手机号" in names
        assert red == "[手机号]"
