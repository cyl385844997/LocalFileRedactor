"""CLI ``--lexicon`` 接线的单元测试。

重点覆盖：
- 显式指定但文件不存在必须报错（静默放行 = 脱敏失效）
- 默认词库不存在属正常，静默跳过
- ``--no-lexicon`` 明确禁用
- 词库条目名与内置规则冲突时报错
- ``rules`` 子命令**不打印词条内容**（词条即敏感清单，打到 stdout 等于泄露）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from redactor.cli import _build_engine, _load_lexicon_if_requested, build_parser
from redactor.core.models import RuleError

BUILTIN = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"

LEX = """\
lexicon:
  - name: 甲方单位
    words:
      - 重庆某某水务
      - 重庆某某环保
    note: 2026 年框架合同
  - name: 关联公司
    words:
      - 某某建设
  - name: 已停用单位
    words:
      - 某某已注销公司
    enabled: false
"""


def write_lex(tmp_path: Path, text: str = LEX, name: str = "lexicon.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def parse(argv: list):
    return build_parser().parse_args(argv)


# ============================================================== 参数解析
class TestArgumentParsing:
    """两个子命令都要有 --lexicon 与 --no-lexicon。"""

    @pytest.mark.parametrize("cmd", ["run", "dry-run"])
    def test_run与dryrun都有词库参数(self, cmd):
        args = parse([cmd, "x.txt", "--lexicon", "a.yaml"])
        assert args.lexicon == "a.yaml"
        assert args.no_lexicon is False

    def test_no_lexicon标志(self):
        args = parse(["run", "x.txt", "--no-lexicon"])
        assert args.no_lexicon is True

    def test_rules子命令也有词库参数(self):
        args = parse(["rules", "--lexicon", "a.yaml"])
        assert args.lexicon == "a.yaml"

    def test_rules子命令支持no_lexicon(self):
        args = parse(["rules", "--no-lexicon"])
        assert args.no_lexicon is True

    def test_不传时默认为空(self):
        args = parse(["run", "x.txt"])
        assert args.lexicon is None
        assert args.no_lexicon is False


# ============================================================== 加载决策
class TestLoadDecision:
    """三种情形：显式指定 / 明确禁用 / 默认路径。"""

    def test_显式指定正常加载(self, tmp_path):
        args = parse(["run", "x.txt", "--lexicon", str(write_lex(tmp_path))])
        entries = _load_lexicon_if_requested(args, [])
        assert [e.name for e in entries] == ["甲方单位", "关联公司", "已停用单位"]

    def test_显式指定但文件不存在必须报错(self, tmp_path):
        """静默放行等于脱敏失效，而用户以为已经脱敏。"""
        missing = tmp_path / "nope.yaml"
        args = parse(["run", "x.txt", "--lexicon", str(missing)])
        with pytest.raises(RuleError) as exc:
            _load_lexicon_if_requested(args, [])
        assert "词库文件不存在" in str(exc.value)
        assert str(missing) in str(exc.value)

    def test_no_lexicon禁用默认词库(self, tmp_path, monkeypatch):
        """即使默认词库存在，--no-lexicon 也不加载。"""
        fake_home = tmp_path / "home"
        (fake_home / ".redactor").mkdir(parents=True)
        (fake_home / ".redactor" / "lexicon.yaml").write_text(LEX, encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        args = parse(["run", "x.txt", "--no-lexicon"])
        assert _load_lexicon_if_requested(args, []) == []

    def test_默认词库存在时自动加载(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        (fake_home / ".redactor").mkdir(parents=True)
        (fake_home / ".redactor" / "lexicon.yaml").write_text(LEX, encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        args = parse(["run", "x.txt"])
        entries = _load_lexicon_if_requested(args, [])
        assert len(entries) == 3

    def test_默认词库不存在时静默跳过(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "empty_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        args = parse(["run", "x.txt"])
        assert _load_lexicon_if_requested(args, []) == []

    def test_显式指定优先于默认路径(self, tmp_path, monkeypatch):
        """两个都存在时，--lexicon 优先。"""
        fake_home = tmp_path / "home"
        (fake_home / ".redactor").mkdir(parents=True)
        (fake_home / ".redactor" / "lexicon.yaml").write_text(
            "lexicon:\n  - name: 默认条目\n    words: [默认词]\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        explicit = write_lex(tmp_path)
        args = parse(["run", "x.txt", "--lexicon", str(explicit)])
        entries = _load_lexicon_if_requested(args, [])
        assert [e.name for e in entries][0] == "甲方单位"


# ============================================================== 冲突校验
class TestReservedNames:
    """词库条目名不得与内置规则同名。"""

    def test_与内置规则同名报错(self, tmp_path):
        text = "lexicon:\n  - name: 手机号\n    words: [某某公司]\n"
        args = parse(["run", "x.txt", "--lexicon", str(write_lex(tmp_path, text))])
        from redactor.core.rules import load_rules

        rules = load_rules(BUILTIN)
        with pytest.raises(RuleError) as exc:
            _load_lexicon_if_requested(args, rules)
        assert "与内置规则同名" in str(exc.value)


# ============================================================== 引擎接线
class TestEngineWiring:
    """_build_engine 必须把词库传到引擎。"""

    def test_词库进入引擎(self, tmp_path):
        args = parse(["run", "x.txt", "--rules", str(BUILTIN),
                      "--lexicon", str(write_lex(tmp_path))])
        engine = _build_engine(args)
        assert len(engine.lexicon_entries) == 3

    def test_不传词库时引擎词库为空(self):
        args = parse(["run", "x.txt", "--rules", str(BUILTIN)])
        engine = _build_engine(args)
        assert engine.lexicon_entries == []

    def test_词库参与脱敏(self, tmp_path):
        args = parse(["run", "x.txt", "--rules", str(BUILTIN),
                      "--lexicon", str(write_lex(tmp_path))])
        engine = _build_engine(args)
        red, matches = engine.redact("甲方为重庆某某水务，电话13800138000")
        assert "重庆某某水务" not in red
        assert "13800138000" not in red
        assert {m.rule_name for m in matches} == {"甲方单位", "手机号"}

    def test_禁用条目不参与脱敏(self, tmp_path):
        args = parse(["run", "x.txt", "--rules", str(BUILTIN),
                      "--lexicon", str(write_lex(tmp_path))])
        engine = _build_engine(args)
        red, _ = engine.redact("某某已注销公司已清算")
        assert "某某已注销公司" in red, "enabled: false 的条目不应匹配"


# ============================================================== 词条保密
class TestWordSecrecy:
    """词条本身是敏感清单，绝不能打印到 stdout。"""

    def test_rules子命令不打印词条(self, tmp_path, capsys):
        from redactor.cli import main

        lex = write_lex(tmp_path)
        code = main(["rules", "--rules", str(BUILTIN), "--lexicon", str(lex)])
        captured = capsys.readouterr().out
        assert code == 0
        # 词条内容绝不能出现
        assert "重庆某某水务" not in captured
        assert "某某建设" not in captured
        assert "某某已注销公司" not in captured
        # 但条目名与词数应该出现（这是元数据，不敏感）
        assert "甲方单位" in captured
        assert "词条 2 条" in captured

    def test_rules子命令未启用词库时给出说明(self, tmp_path, capsys, monkeypatch):
        from redactor.cli import main

        fake_home = tmp_path / "empty_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        main(["rules", "--rules", str(BUILTIN)])
        captured = capsys.readouterr().out
        assert "自定义词库：未启用" in captured
