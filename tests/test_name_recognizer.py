"""P8 人名识别（jieba）测试。

语料全部来自 .verify/probe_p8/probe_p8.py 的实测探针，
**实测数据**：正例 6/6、原始误伤 2/12、大字可消除 3/3 = 100%、
剔除后误伤 0/12。

5 组：
    A. TestNameRecognizerBasic  基本行为（偏移 / 词性 / 占位符）
    B. TestDaZiStrip             大字金额剔除（核心）
    C. TestEngineNames           引擎集成（默认关 / 开启后生效 / 与格式规则共存）
    D. TestGuiNamesSwitch        GUI 开关（默认关闭）
    E. TestReverseValidation     ★去掉剔除逻辑 → 必须红
"""
from __future__ import annotations

from pathlib import Path

import pytest

from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rule_dir
from redactor.recognizers import name_recognizer as mod
from redactor.recognizers.name_recognizer import (
    DA_ZI_CHARS,
    NAME_TAGS,
    NameRecognizer,
    build_name_recognizer,
    is_da_zi_amount,
    jieba_available,
)

# 实测探针的正例语料
POSITIVES = [
    ("张三在重庆创锐公司工作", "张三"),
    ("项目负责人是李四", "李四"),
    ("王五已经批准了这个方案", "王五"),
    ("张伟明博士做了报告", "张伟明"),
    ("欧阳修写过一篇文章", "欧阳修"),
    ("司马光砸缸的故事", "司马光"),
]

# 实测误伤大字金额的两条
DA_ZI_CASES = [
    "已支付伍拾万元整",
    "本次付款金额为叁佰伍拾陆万元柒仟捌佰玖拾元",
]

# 实测确认 0 误伤的其余负例
CLEAN_NEGATIVES = [
    "甲方为重庆创锐电气技术有限公司",
    "我方与西南政法大学签署协议",
    "本项目由重庆水务集团实施",
    "该项目建设周期为十八个月",
    "合同总价为壹佰贰拾万元",
    "第五条第二款约定",
    "根据民法典第九百九十条",
    "这个项目很难做",
    "他今天去公司了",
]


@pytest.fixture(scope="module")
def builtin_rules():
    return load_rule_dir(Path(__file__).resolve().parent.parent / "rules")


@pytest.fixture(scope="module")
def jieba_ok():
    """jieba 未安装时整组 skip —— 它是**可选**依赖，缺失不该让测试红。"""
    if not jieba_available():
        pytest.skip("jieba 未安装（P8 是可选依赖）")
    return True


# ============================================================ A. 基本行为
class TestNameRecognizerBasic:
    def test_只取人名词性(self):
        assert NAME_TAGS == ("nr", "nrfg", "nrt")

    def test_正例识别(self, jieba_ok):
        r = build_name_recognizer()
        hits = {m.raw for m in r.find("张三在重庆创锐公司工作")}
        assert "张三" in hits, hits

    def test_标记偏移可切原文(self, jieba_ok):
        """★偏移必须指向原文 —— 与其他识别器同构，能直接用于替换。"""
        r = build_name_recognizer()
        for text, _want in POSITIVES:
            for m in r.find(text):
                assert text[m.start:m.end] == m.raw, (
                    f"偏移错位：{m.raw!r} @ ({m.start},{m.end})，"
                    f"实际切出 {text[m.start:m.end]!r}"
                )

    def test_占位符与人名(self, jieba_ok):
        r = build_name_recognizer()
        m = r.find("项目负责人是李四")[0]
        assert m.placeholder == "[人名]"
        assert m.rule_name == "人名"

    def test_无地名误伤(self, jieba_ok):
        """ns（地名）不该被当人名。"""
        r = build_name_recognizer()
        assert r.find("甲方为重庆创锐电气技术有限公司") == []
        assert r.find("我方与西南政法大学签署协议") == []

    def test_多次出现各自命中(self, jieba_ok):
        r = build_name_recognizer()
        hits = [m.raw for m in r.find("张三和李四会面，王五也在")]
        print(f"  多命中: {hits}")
        assert "张三" in hits and "李四" in hits

    def test_空文本安全(self, jieba_ok):
        r = build_name_recognizer()
        assert r.find("") == []

    def test_自定义占位符(self, jieba_ok):
        r = build_name_recognizer(placeholder="[姓名]", rule_name="姓名")
        m = r.find("张三来了")[0]
        assert m.placeholder == "[姓名]"
        assert m.rule_name == "姓名"

    def test_工厂返回None当jieba缺失(self, monkeypatch):
        """jieba 缺失时返回 None，**不抛** —— 可选依赖不该阻断主流程。"""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "jieba":
                raise ImportError("模拟未安装")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert build_name_recognizer() is None
        assert jieba_available() is False

    def test_标记find在缺依赖时返回空(self, monkeypatch):
        """已构造的识别器遇缺依赖也应返回空而非崩。"""
        r = NameRecognizer()
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "jieba":
                raise ImportError("模拟未安装")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert r.find("张三来了") == []


# ============================================================ B. 大字剔除
class TestDaZiStrip:
    def test_字符集覆盖大写数字(self):
        for ch in "零壹贰叁肆伍陆柒捌玖拾佰仟万亿兆元角分圆":
            assert ch in DA_ZI_CHARS, f"字符集漏了 {ch!r}"

    def test_普通汉字不算大字(self):
        for ch in "重庆创锐公司大学项目":
            assert ch not in DA_ZI_CHARS, f"{ch!r} 不该在大字字符集里"

    def test_标记按字符判不按长度判(self):
        """★实现要点：jieba 把"柒仟捌佰玖拾"切成 5 个单字（标 m），
        只把"伍拾""陆"误判成 nr —— 误判片段长度在 1~2 字间浮动。
        按长度剔除会漏，按字符剔除则100% 命中（实测）。"""
        assert is_da_zi_amount("伍拾")     # 2 字
        assert is_da_zi_amount("陆")# 1 字
        assert is_da_zi_amount("壹佰元")    # 3 字
        assert not is_da_zi_amount("张三")
        assert not is_da_zi_amount("李四")

    def test_剔除后大字金额零命中(self, jieba_ok):
        r = build_name_recognizer(strip_da_zi=True)
        for text in DA_ZI_CASES:
            hits = [m.raw for m in r.find(text)]
            print(f"  {text[:26]:<28} {hits}")
            assert hits == [], f"{text!r} 仍有误伤：{hits}"

    def test_其余负例零误伤(self, jieba_ok):
        """实测：剔除后 12 条负例误伤 0。"""
        r = build_name_recognizer(strip_da_zi=True)
        bad = [(t, [m.raw for m in r.find(t)]) for t in CLEAN_NEGATIVES
               if r.find(t)]
        assert bad == [], f"误伤：{bad}"

    def test_开关状态可读(self, jieba_ok):
        assert build_name_recognizer(strip_da_zi=True).strip_da_zi is True
        assert build_name_recognizer(strip_da_zi=False).strip_da_zi is False


# ============================================================ C. 引擎集成
class TestEngineNames:
    def test_标记默认关闭(self, builtin_rules):
        eng = RedactionEngine(builtin_rules)
        assert eng.redact("张三在重庆工作")[0] == "张三在重庆工作", \
            "未显式开启时不该脱敏人名（默认关闭）"

    def test_开启后生效(self, builtin_rules):
        eng = RedactionEngine(builtin_rules, enable_names=True)
        out, _ = eng.redact("张三在重庆工作")
        print(f"  out: {out!r}")
        assert "[人名]" in out, out
        assert "张三" not in out

    def test_开启后仍不误伤大字(self, builtin_rules):
        eng = RedactionEngine(builtin_rules, enable_names=True)
        for text in DA_ZI_CASES:
            out, _ = eng.redact(text)
            print(f"  {text[:26]:<28} {out!r}")
            assert "[人名]" not in out, f"{text!r} 误伤：{out}"

    def test_标记人名与格式规则共存不互吞(self, builtin_rules):
        """张三13812345678：两者必须各自脱敏。"""
        eng = RedactionEngine(builtin_rules, enable_names=True)
        out, matches = eng.redact("张三13812345678")
        names = {m.rule_name for m in matches}
        print(f"  out: {out!r}  rules: {names}")
        assert "13812345678" not in out, f"手机号被吞：{out}"
        assert "[手机号]" in out
        assert "张三" not in out, f"人名没脱：{out}"
        assert "[人名]" in out
        assert names == {"手机号", "人名"}, names

    def test_人名与范围脱敏共存(self, builtin_rules):
        from redactor.core.scopes import ScopeEntry
        eng = RedactionEngine(
            builtin_rules,
            scope_entries=[ScopeEntry(name="单位", suffixes=("公司",))],
            enable_names=True,
        )
        out, _ = eng.redact("张三在重庆创锐公司")
        print(f"  out: {out!r}")
        assert "[人名]" in out
        assert "[公司]" in out

    def test_strip_da_zi_false时误伤出现(self, builtin_rules):
        """反向确认：关掉剔除，大字金额立刻被误脱 —— 说明剔除真的在起作用。"""
        eng = RedactionEngine(
            builtin_rules, enable_names=True, strip_da_zi=False
        )
        out, _ = eng.redact("已支付伍拾万元整")
        print(f"  关剔除后: {out!r}")
        assert "[人名]" in out, "关掉剔除后应该出现误伤，否则剔除没起作用"

    def test_关闭时不造识别器(self, builtin_rules):
        eng = RedactionEngine(builtin_rules, enable_names=False)
        assert eng._name_recognizer is None


# ============================================================ D. GUI 开关
class TestGuiNamesSwitch:
    @pytest.fixture
    def qapp(self):
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        yield app
        app.processEvents()

    def test_工具栏有人名开关(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        assert w.names_action().text() == "人名识别"
        w.close()

    def test_标记默认关闭(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        assert w.names_action().isChecked() is False, "★必须默认关闭"
        assert w.controller().names_enabled() is False
        w.close()

    def test_勾选后传controller(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        w.names_action().setChecked(True)
        app = qapp
        app.processEvents()
        assert w.controller().names_enabled() is True
        w.close()

    def test_大字剔除开关跟随(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        # 人名未开时，大字开关 disabled
        assert w.strip_da_zi_action().isEnabled() is False
        w.names_action().setChecked(True)
        qapp.processEvents()
        assert w.strip_da_zi_action().isEnabled() is True
        w.close()

    def test_日志记录启用(self, qapp):
        from redactor.gui import MainWindow
        w = MainWindow()
        w.names_action().setChecked(True)
        qapp.processEvents()
        assert "人名识别已启用" in w.log_panel().toPlainText()
        w.close()

    def test_controller运行中拒绝(self, qapp, tmp_path: Path):
        from redactor.gui.controller import RedactorController

        def _mk_docx(p):
            from docx import Document
            d = Document()
            d.add_paragraph("张三在重庆工作")
            d.save(str(p))

        src = tmp_path / "a.docx"
        _mk_docx(src)
        c = RedactorController()
        c.start([str(src)], tmp_path / "out", dry_run=False)
        assert c.is_running()
        c.set_names(enabled=True)
        assert c.names_enabled() is False, "运行中应拒绝改配置"
        import time as _t
        deadline = _t.monotonic() + 15
        while _t.monotonic() < deadline and c.is_running():
            qapp.processEvents()
            _t.sleep(0.05)


# ============================================================ E. 反向验证
class TestReverseValidation:
    def test_标记去掉剔除逻辑_守护测试会红(self, jieba_ok):
        """★核心反向验证：剔除逻辑失效时，误伤必须出现。

        本测试**主动确认误伤用例有鉴别力** ——
        若去掉剔除后仍然零误伤，说明语料选错了。
        """
        # 真版：剔除开启 -> 零误伤
        good = build_name_recognizer(strip_da_zi=True)
        good_hits = [m.raw for t in DA_ZI_CASES for m in good.find(t)]
        assert good_hits == [], f"真版不该有命中，却有 {good_hits}"

        # 变异版：剔除关闭 -> 误伤必须出现
        evil = build_name_recognizer(strip_da_zi=False)
        evil_hits = [m.raw for t in DA_ZI_CASES for m in evil.find(t)]
        print(f"  真版命中: {good_hits}")
        print(f"  变异命中: {evil_hits}")
        assert evil_hits, (
            "❌ 对照失效：关掉剔除后竟然仍零误伤，"
            "说明 DA_ZI_CASES 语料已不能区分两种实现"
        )
        assert set(evil_hits) == {"伍拾", "陆"}, (
            f"变异版命中与探针实测不符：{evil_hits}（探针实测是 ['伍拾','陆']）"
        )

    def test_标记模块级开关STRIP_DA_ZI守护(self, jieba_ok):
        """模块级默认开关必须是 True —— 关掉它就是产品回归。"""
        assert mod.STRIP_DA_ZI is True

    def test_标记把字符集清空_误伤会出现(self, jieba_ok, monkeypatch):
        """变异：DA_ZI_CHARS 清空 -> is_da_zi_amount 恒 False -> 误伤。"""
        monkeypatch.setattr(mod, "DA_ZI_CHARS", frozenset())
        assert not is_da_zi_amount("伍拾"), "变异后不该判为大字"
        # 重新构造（让识别器读到新的字符集）
        r = NameRecognizer(strip_da_zi=True)
        hits = [m.raw for t in DA_ZI_CASES for m in r.find(t)]
        print(f"  字符集清空后命中: {hits}")
        assert hits, "❌ 变异无效：字符集清空后竟然仍零误伤"

    def test_标记偏移断言有鉴别力(self, jieba_ok):
        """偏移断言必须能抓到错位 —— 用"故意错位"的变体验证。"""
        r = build_name_recognizer()
        text = "张三在重庆工作"
        m = r.find(text)[0]
        # 真版：偏移正确
        assert text[m.start:m.end] == m.raw
        # 变异：偏移 +1 -> 断言必须不成立
        assert text[m.start + 1:m.end + 1] != m.raw, (
            "❌ 对照失效：偏移 +1 后竟然仍等于原文，"
            "说明偏移断言抓不住错位"
        )