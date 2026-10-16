"""自定义词库加载与校验的单元测试。

覆盖七组场景：加载 / 归一化 / 去重 / 行号定位 / 边界 / 引擎不变性 / 端到端。

设计原则（对应 docs）：词库出问题必须**报错并指出行号**，绝不静默跳过——
跳过一个词条等于漏脱敏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.lexicon import (
    LexiconEntry,
    default_lexicon_path,
    load_lexicon,
    load_lexicon_from_text,
    normalize_word,
)
from redactor.core.models import RuleError
from redactor.core.rules import load_rules


def write_lexicon(tmp_path: Path, text: str, name: str = "lexicon.yaml") -> Path:
    """把词库 YAML 写到临时文件并返回路径。"""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


BASIC = """
lexicon:
  - name: 甲方单位
    words:
      - 重庆某某水务
      - 重庆某某环保
    note: 2026年框架合同
  - name: 关联公司
    words:
      - 某某建设
"""


# ============================================================== 1. 加载
class TestLoading:
    """基本加载行为。"""

    def test_加载正常条目(self, tmp_path):
        entries = load_lexicon(write_lexicon(tmp_path, BASIC))
        assert len(entries) == 2
        assert [e.name for e in entries] == ["甲方单位", "关联公司"]
        assert entries[0].words == ("重庆某某水务", "重庆某某环保")
        assert entries[0].note == "2026年框架合同"

    def test_默认占位符格式正确(self, tmp_path):
        entries = load_lexicon(write_lexicon(tmp_path, BASIC))
        assert all(e.placeholder == f"[{e.name}]" for e in entries)

    def test_字段缺省值(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: [甲公司]\n"
        entry = load_lexicon(write_lexicon(tmp_path, text))[0]
        assert entry.enabled is True
        assert entry.numbered is False
        assert entry.note == ""

    def test_显式设置布尔字段(self, tmp_path):
        text = (
            "lexicon:\n"
            "  - name: A\n"
            "    words: [甲公司]\n"
            "    enabled: false\n"
            "    numbered: true\n"
        )
        entry = load_lexicon(write_lexicon(tmp_path, text))[0]
        assert entry.enabled is False
        assert entry.numbered is True

    def test_不加载禁用条目不报错(self, tmp_path):
        """``enabled: false`` 的条目仍会被加载，由调用方过滤（与 rules.py 分工一致）。"""
        text = "lexicon:\n  - name: A\n    words: [甲公司]\n    enabled: false\n"
        entries = load_lexicon(write_lexicon(tmp_path, text))
        assert len(entries) == 1
        assert entries[0].enabled is False
        assert entries[0].active_words == ()

    def test_启用条目active_words为归一化词(self, tmp_path):
        entries = load_lexicon(write_lexicon(tmp_path, BASIC))
        assert entries[0].active_words == ("重庆某某水务", "重庆某某环保")

    def test_文件不存在报错并给出路径(self, tmp_path):
        missing = tmp_path / "nope.yaml"
        with pytest.raises(RuleError) as exc:
            load_lexicon(missing)
        assert "词库文件不存在" in str(exc.value)
        assert str(missing) in str(exc.value)

    def test_默认路径不存在时返回None(self, monkeypatch, tmp_path):
        """默认路径是~/.redactor/lexicon.yaml；不存在时返回 None 而非报错。"""
        from pathlib import Path as P

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(P, "home", classmethod(lambda cls: fake_home))

        # 不存在 -> None（未指定 --lexicon 时这是正常情况）
        assert default_lexicon_path() is None

    def test_默认路径存在时返回该路径(self, monkeypatch, tmp_path):
        """默认路径存在时应返回它，供 CLI 未指定 --lexicon 时自动加载。"""
        from pathlib import Path as P

        fake_home = tmp_path / "fakehome"
        (fake_home / ".redactor").mkdir(parents=True)
        target = fake_home / ".redactor" / "lexicon.yaml"
        target.write_text("lexicon:\n  - name: A\n    words: [甲]\n", encoding="utf-8")
        monkeypatch.setattr(P, "home", classmethod(lambda cls: fake_home))

        assert default_lexicon_path() == target


# ============================================================== 2. 严格校验
class TestStrictValidation:
    """错误必须显式抛出并带行号。"""

    def test_yaml语法错误报行号(self, tmp_path):
        path = write_lexicon(tmp_path, "lexicon:\n  - name: A\n    words: 'x'\n   bad\n")
        with pytest.raises(RuleError) as exc:
            load_lexicon(path)
        assert "YAML 语法错误" in str(exc.value)
        # 显式断言行号：语法错误必须定位到具体行，否则用户无从下手
        assert exc.value.line == 4, f"语法错误应指向第 4 行，实际 {exc.value.line}"
        assert "第 4 行" in str(exc.value)

    def test_缺少顶层lexicon键报错(self, tmp_path):
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, "rules: []\n"))
        assert "缺少顶层 'lexicon'" in str(exc.value)

    def test_缺顶层键时行号为None(self, tmp_path):
        """**如实记录一个限制**：顶层键缺失指向的是整个文件的结构，
        PyYAML 节点树里没有可对应的节点，故 line 为 None。

        这类错误改用"路径 + 键名"定位，用户看提示就知道要检查文件开头的
        ``lexicon:`` 行。锁住该行为，避免以后误以为它也带行号。
        """
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, "rules: []\n"))
        assert exc.value.line is None
        assert exc.value.path is not None
        assert "lexicon" in str(exc.value)

    def test_顶层非映射报错(self, tmp_path):
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, "- a\n- b\n"))
        assert "顶层必须是键值映射" in str(exc.value)

    def test_lexicon不是列表报错(self, tmp_path):
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, "lexicon:\n  words: x\n"))
        assert "必须是列表" in str(exc.value)

    def test_lexicon空列表报错(self, tmp_path):
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, "lexicon: []\n"))
        assert "没有任何词库条目" in str(exc.value)

    def test_条目不是映射报错(self, tmp_path):
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, "lexicon:\n  - 甲公司\n"))
        assert "必须是键值映射" in str(exc.value)

    def test_缺name报错(self, tmp_path):
        text = "lexicon:\n  - words: [甲公司]\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "缺少必填字段 'name'" in str(exc.value)

    def test_缺words报错(self, tmp_path):
        text = "lexicon:\n  - name: A\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "缺少必填字段 'words'" in str(exc.value)

    def test_未知字段报错并列出白名单(self, tmp_path):
        """拼错的字段必须报错，否则用户以为生效了。"""
        text = "lexicon:\n  - name: A\n    words: [甲公司]\n    wordz: [甲]\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        message = str(exc.value)
        assert "未知字段 'wordz'" in message
        # 显式断言白名单内容：用户要靠这个列表知道正确的字段名叫什么
        for allowed in ("name", "words", "enabled", "numbered", "note"):
            assert "'%s'" % allowed in message, f"白名单应包含 {allowed}"
        assert exc.value.line == 4

    def test_words为空列表报错(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: []\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "没有任何词条" in str(exc.value)

    def test_words不是列表报错(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: 甲公司\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "必须是列表" in str(exc.value)

    def test_词条为空串报错(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: ['']\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "必须是非空字符串" in str(exc.value)

    def test_词条为纯空白报错(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: ['   ']\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "必须是非空字符串" in str(exc.value)

    def test_布尔字段类型错误报错(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: [甲公司]\n    enabled: 是不是\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "必须是 true/false" in str(exc.value)

    def test_重复yaml键报错(self, tmp_path):
        """safe_load 对重复键是静默取后者，必须靠节点树检出。"""
        text = (
            "lexicon:\n"
            "  - name: A\n"
            "    words: [甲公司]\n"
            "    words: [乙公司]\n"
        )
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "重复字段 'words'" in str(exc.value)

    def test_条目名重复报错(self, tmp_path):
        text = (
            "lexicon:\n"
            "  - name: 甲方\n"
            "    words: [甲公司]\n"
            "  - name: 甲方\n"
            "    words: [乙公司]\n"
        )
        with pytest.raises(RuleError) as exc:
            load_lexicon(write_lexicon(tmp_path, text))
        assert "词库条目名 '甲方' 重复" in str(exc.value)

    def test_非utf8文件报错(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_bytes(b"lexicon:\n  - name: \xff\xfe\n")
        with pytest.raises(RuleError) as exc:
            load_lexicon(path)
        assert "不是 UTF-8 编码" in str(exc.value)


# ============================================================== 3. 归一化
class TestNormalize:
    """归一化：两条1:1 映射，必须保持长度不变。"""

    def test_全角数字转半角(self):
        assert normalize_word("１３８５５") == "13855"

    def test_全角字母转半角(self):
        assert normalize_word("ＡＢＣ") == "ABC"

    def test_全角标点转半角(self):
        assert normalize_word("（），；：！？") == "(),;:!?"

    def test_表意空格转半角空格(self):
        assert normalize_word("重庆\u3000某某") == "重庆 某某"

    def test_半角原样保留(self):
        assert normalize_word("ABC-123_xyz") == "ABC-123_xyz"

    def test_中文原样保留(self):
        assert normalize_word("重庆某某水务") == "重庆某某水务"

    def test_变长字符不被展开(self):
        """关键性质：完整 NFKC 会把 Ⅳ/㍿/⑴ 展开成多字符，导致偏移错位。
        归一化乙不做这件事。"""
        for ch in ("Ⅳ", "㍿", "⑴", "…", "℃"):
            assert normalize_word(ch) == ch

    def test_简繁不转换(self):
        """简繁互转需额外依赖，本项目不做，用户两种写法都写。"""
        assert normalize_word("陳毅") == "陳毅"
        assert normalize_word("陈毅") == "陈毅"

    @pytest.mark.parametrize(
        "text",
        ["", "Ａ", "１", "重庆　某某", "（重庆）", " ", "aＡbＢ", "Ｇｏｏｇｌｅ１３８"],
    )
    def test_长度恒定(self, text):
        assert len(normalize_word(text)) == len(text)

    @pytest.mark.parametrize(
        "text",
        ["重庆ＡＢＣ", "１38", "重庆　某某", "（重庆）ＡＢＣ"],
    )
    def test_位置偏移恒等(self, text):
        """归一化是逐字符 1:1 替换，故归一化偏移可直接用于定位原文。

        这是整个归一化设计的地基：若长度或位置会漂移，
        匹配器算出的start/end 就不能用来切原文，替换会错位。
        """
        norm = normalize_word(text)
        assert len(norm) == len(text), "归一化必须保持长度"
        # 逐位置对比：每个归一化字符只由该位置的原文决定
        for i, ch in enumerate(text):
            assert normalize_word(ch) == norm[i]


# ============================================================== 4. 去重
class TestDeduplication:
    """条目内重复必须报错；跨条目重复静默去重。"""

    def test_条目内重复词报错并给两个行号(self):
        text = (
            "lexicon:\n"
            "  - name: A\n"
            "    words:\n"
            "      - 甲公司\n"
            "      - 乙公司\n"
            "      - 甲公司\n"
        )
        with pytest.raises(RuleError) as exc:
            load_lexicon_from_text(text, path=Path("<t>"))
        message = str(exc.value)
        assert "重复词 '甲公司'" in message
        assert "第 6 行" in message
        assert "第 4 行" in message

    def test_条目内全角半角重复也报错(self):
        """归一化后相同的两个词也算重复。"""
        text = "lexicon:\n  - name: A\n    words: [ＡＢＣ, ABC]\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon_from_text(text, path=Path("<t>"))
        assert "重复词" in str(exc.value)

    def test_同行重复的报错信息不指向本行(self):
        """行内列表 ['ＡＢＣ', ABC] 触发重复时，'首次出现行'就是本行本身。
        若照搬"与第 N 行相同"的措辞，报错会指向自己、失去意义。"""
        text = "lexicon:\n  - name: A\n    words: ['ＡＢＣ', ABC]\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon_from_text(text, path=Path("<t>"))
        message = str(exc.value)
        assert "第 3 行" in message
        assert "归一化后与第 3 行相同" not in message, "不能把当前行说成'首次出现行'"
        assert "该行与列表中已有词归一化后相同" in message

    def test_条目内表意空格与半角空格重复也报错(self):
        text = "lexicon:\n  - name: A\n    words: ['重庆\u3000某某', '重庆 某某']\n"
        with pytest.raises(RuleError) as exc:
            load_lexicon_from_text(text, path=Path("<t>"))
        assert "重复词" in str(exc.value)

    def test_跨条目重复词静默去重保留首次(self):
        text = (
            "lexicon:\n"
            "  - name: A\n"
            "    words: [甲公司, 乙公司]\n"
            "  - name: B\n"
            "    words: [乙公司, 丙公司]\n"
        )
        entries = load_lexicon_from_text(text, path=Path("<t>"))
        assert entries[0].words == ("甲公司", "乙公司")
        # 第二条里的"乙公司"已归A，保留首次出现
        assert entries[1].words == ("丙公司",)

    def test_跨条目去重不报错(self):
        text = (
            "lexicon:\n"
            "  - name: A\n"
            "    words: [甲公司]\n"
            "  - name: B\n"
            "    words: [甲公司]\n"
        )
        entries = load_lexicon_from_text(text, path=Path("<t>"))
        assert len(entries) == 2
        assert entries[1].words == ()


# ============================================================== 5. 行号定位
class TestLineNumbers:
    """报错必须指向 YAML 真实行号，便于用户定位。"""

    def test_条目line指向条目起始行(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: [甲]\n  - name: B\n    words: [乙]\n"
        entries = load_lexicon(write_lexicon(tmp_path, text))
        assert entries[0].line == 2
        assert entries[1].line == 4

    def test_未知字段报该字段所在行(self):
        text = (
            "lexicon:\n"
            "  - name: A\n"
            "    words: [甲]\n"
            "    note: 正常\n"
            "    bogus: x\n"
        )
        with pytest.raises(RuleError) as exc:
            load_lexicon_from_text(text, path=Path("<t>"))
        assert exc.value.line == 5

    def test_布尔字段错误报该字段行(self):
        text = (
            "lexicon:\n"
            "  - name: A\n"
            "    words: [甲]\n"
            "    enabled: 错\n"
        )
        with pytest.raises(RuleError) as exc:
            load_lexicon_from_text(text, path=Path("<t>"))
        assert exc.value.line == 4

    def test_错误对象带path属性(self, tmp_path):
        path = tmp_path / "x.yaml"
        with pytest.raises(RuleError) as exc:
            load_lexicon_from_text("lexicon:\n  - words: [甲]\n", path=path)
        # path 属性是传入的 Path 的字符串形态，具体分隔符随平台而定
        assert exc.value.path == str(path)


# ============================================================== 6. 边界
class TestEdgeCases:
    """词条内容边界。"""

    def test_词条含正则元字符不被当作正则(self, tmp_path):
        """词库走 AC 而非正则，故这些字符必须原样保留。

        用 yaml.safe_dump 生成 YAML，避免手写引号/转义出错
        （上一次手写 `s\\t` 就因为 repr 双重转义而多了一层反斜杠）。
        """
        import yaml

        tricky = [
            "a.b", "c+d", "e*f", "g?h", "[i]j", "(k)l",
            "m|n", "o^p", "q$r", "s\\t", "u-v", "{x}", "汉字.公司",
        ]
        data = {"lexicon": [{"name": "A", "words": tricky}]}
        path = tmp_path / "tricky.yaml"
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        entries = load_lexicon(path)
        assert list(entries[0].words) == tricky

    def test_单字符词条(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: [甲, 乙]\n"
        entry = load_lexicon(write_lexicon(tmp_path, text))[0]
        assert entry.words == ("甲", "乙")

    def test_超长词条(self, tmp_path):
        long_word = "重庆" * 500
        text = f"lexicon:\n  - name: A\n    words: ['{long_word}']\n"
        entry = load_lexicon(write_lexicon(tmp_path, text))[0]
        assert entry.words == (long_word,)

    def test_词条首尾空白被strip(self, tmp_path):
        text = "lexicon:\n  - name: A\n    words: ['  甲公司  ']\n"
        entry = load_lexicon(write_lexicon(tmp_path, text))[0]
        assert entry.words == ("甲公司",)

    def test_条目名含空格(self, tmp_path):
        text = "lexicon:\n  - name: '甲方 单位'\n    words: [甲]\n"
        entry = load_lexicon(write_lexicon(tmp_path, text))[0]
        assert entry.name == "甲方 单位"
        assert entry.placeholder == "[甲方 单位]"

    def test_dataclass冻结(self, tmp_path):
        """LexiconEntry 是 frozen dataclass：加载后不应被意外改动。"""
        from dataclasses import FrozenInstanceError

        entries = load_lexicon(write_lexicon(tmp_path, BASIC))
        with pytest.raises(FrozenInstanceError):
            entries[0].name = "改名"
        # words 等容器本身也应是不可变的 tuple，否则仍可被 mutate
        assert isinstance(entries[0].words, tuple)
        assert isinstance(entries[0].normalized, tuple)
        with pytest.raises(TypeError):
            entries[0].words[0] = "篡改"  # type: ignore[index]


# ============================================================== 7. 引擎不变性
class TestEngineCompatibility:
    """词库为空时行为必须与改动前完全一致。"""

    def _engine(self, **kwargs):
        rules = load_rules(
            Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"
        )
        return RedactionEngine(rules, **kwargs)

    def test_引擎接受词库参数(self):
        entries = load_lexicon_from_text(BASIC, path=Path("<t>"))
        engine = self._engine(lexicon_entries=entries)
        assert engine.lexicon_entries == entries

    def test_词库为空时条目列表为空(self):
        engine = self._engine()
        assert engine.lexicon_entries == []

    def test_词库参数缺省不报错(self):
        """不传词库参数的老调用方式必须继续可用（CLI 现阶段就是这样调的）。"""
        rules = load_rules(
            Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"
        )
        assert RedactionEngine(rules) is not None

    def test_词库不参与匹配_首版已知限制(self):
        """首版词库只加载不匹配，故文本里的词库词**不应**被脱敏。
        这条测试锁住现状，模块 2 接入识别器后应改成"应当被脱敏"。"""
        entries = load_lexicon_from_text(BASIC, path=Path("<t>"))
        engine = self._engine(lexicon_entries=entries)
        text = "本项目甲方为重庆某某水务，联系人电话13800138000。"
        redacted, matches = engine.redact(text)
        assert "重庆某某水务" in redacted, "首版词库不参与匹配"
        assert "13800138000" not in redacted, "内置正则仍然生效"

    def test_词库为空时脱敏结果与不传参数一致(self):
        text = "联系人张三，电话13800138000，邮箱a@example.com。"
        rules = load_rules(
            Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"
        )
        without = RedactionEngine(rules).redact(text)[0]
        with_empty = RedactionEngine(rules, lexicon_entries=[]).redact(text)[0]
        assert without == with_empty

    def test_优先级字典不含词库(self):
        """词库首版不进 _priority，故 resolve_overlaps 里它永远拿默认值垫底——
        这正好保证'词库优先级低于内置正则'。"""
        entries = load_lexicon_from_text(BASIC, path=Path("<t>"))
        engine = self._engine(lexicon_entries=entries)
        assert all(e.name not in engine._priority for e in entries)


# ============================================================== 8. 端到端
class TestEndToEnd:
    """从 YAML 文件到 LexiconEntry 列表的完整链路。"""

    def test_真实文件加载链路(self, tmp_path):
        path = write_lexicon(
            tmp_path,
            "lexicon:\n"
            "  - name: 甲方单位\n"
            "    words:\n"
            "      - 重庆某某水务\n"
            "      - 重庆某某环保\n"
            "    numbered: false\n"
            "    note: 2026年框架合同甲方\n"
            "  - name: 关联公司\n"
            "    words: [某某建设]\n"
            "    enabled: false\n",
        )
        entries = load_lexicon(path)
        assert [e.name for e in entries] == ["甲方单位", "关联公司"]
        assert entries[0].enabled is True
        assert entries[0].words == ("重庆某某水务", "重庆某某环保")
        assert entries[0].normalized == entries[0].words  # 全角转半角后不变
        assert entries[1].enabled is False
        assert entries[1].active_words == ()

    def test_条目可读回占位符(self, tmp_path):
        entries = load_lexicon(write_lexicon(tmp_path, BASIC))
        assert entries[0].placeholder == "[甲方单位]"

    def test_全角词条归一化后可匹配半角形态(self):
        """词库写全角，文档写半角，应视为同一个词。"""
        text = "lexicon:\n  - name: A\n    words: ['ＡＢＣ公司']\n"
        entry = load_lexicon_from_text(text, path=Path("<t>"))[0]
        assert entry.normalized == ("ABC公司",)
        assert entry.words == ("ＡＢＣ公司",), "原始写法应保留以便导出"