"""报告与 CLI 端到端测试。

重点验证补充约束 A/B/C：
    A. 报告必须包含"未能处理的文件及原因"
    B. --dry-run 只统计不写文件
    C. 规则加载失败报错退出且指明行号
以及"报告零敏感内容""退出码正确"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixtures.make_samples import FAKE_BANK_CARD_BAD, FAKE_EMAIL, FAKE_ID_INVALID, FAKE_PHONE_1
from redactor import cli
from redactor.core.models import FileResult, HitStat
from redactor.report import REASON_LABELS, Report

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_RULES = PROJECT_ROOT / "rules" / "builtin.yaml"


@pytest.fixture()
def workdir(tmp_path):
    """构造混合目录：可处理、格式不支持、空文件、以及校验位错误的文件。"""
    src = tmp_path / "待脱敏"
    src.mkdir()
    (src / "a.txt").write_text(f"手机 {FAKE_PHONE_1}\n邮箱 {FAKE_EMAIL}\n", encoding="utf-8")
    (src / "b.txt").write_text(f"证件 {FAKE_ID_INVALID}\n卡号 {FAKE_BANK_CARD_BAD}\n", encoding="utf-8")
    (src / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0legacy")
    (src / "empty.txt").write_text("", encoding="utf-8")
    return src


class TestReportContent:
    """报告内容。"""

    def test_统计信息正确(self):
        report = Report()
        report.add(
            FileResult(
                source="a.txt",
                output="out/a_redacted.txt",
                status="ok",
                hits=[HitStat("手机号", 2, ("第 1 行", "第 3 行")), HitStat("邮箱", 1, ("第 5 行",))],
            )
        )
        data = report.to_dict()
        assert data["totals"]["files"] == 1
        assert data["totals"]["hits"] == 3
        assert data["totals"]["by_rule"] == {"手机号": 2, "邮箱": 1}

    def test_按规则命中数降序(self):
        report = Report()
        report.add(FileResult(source="a", hits=[HitStat("甲", 1), HitStat("乙", 5), HitStat("丙", 3)]))
        assert list(report.to_dict()["totals"]["by_rule"]) == ["乙", "丙", "甲"]

    def test_报告不含原文(self):
        """补充约束 2：报告零敏感内容。"""
        report = Report()
        report.add(
            FileResult(
                source="a.txt",
                output="out.txt",
                status="ok",
                hits=[HitStat("手机号", 1, ("第 1 行",))],
            )
        )
        blob = report.to_text() + report.to_json()
        assert FAKE_PHONE_1 not in blob
        assert FAKE_EMAIL not in blob

    def test_失败文件带原因码与中文说明(self):
        """补充约束 A：未能处理的文件必须进报告。"""
        report = Report()
        report.add(
            FileResult(
                source="locked.doc",
                status="failed",
                reason="encrypted_file",
                message="文件已加密",
            )
        )
        text = report.to_text()
        assert "需要人工处理的文件" in text
        assert "locked.doc" in text
        assert REASON_LABELS["encrypted_file"] in text

    def test_成功但部分未处理的文件必须提示(self):
        """回归测试：状态为"成功"但存在跳过段落时，报告必须展示。

        早期版本只在 status == "failed" 时渲染 message，
        导致 docx 跳过了含复杂结构的段落却毫无提示——
        漏脱敏风险不可见，等于白做。
        """
        report = Report()
        report.add(
            FileResult(
                source="a.docx",
                status="ok",
                hits=[HitStat("手机号", 1, ("第 4 段",))],
                message="共 4 个正文段落，其中 2 个含换行符/制表符等复杂结构，本版已跳过（未脱敏）",
            )
        )
        text = report.to_text()

        assert "未处理" in text
        assert "需人工复核" in text
        assert "a.docx" in text
        assert "本版已跳过" in text

    def test_成功且无提示的文件不产生噪音(self):
        """没有跳过内容时不应输出多余的"需复核"段落。"""
        report = Report()
        report.add(FileResult(source="clean.txt", status="ok", hits=[HitStat("手机号", 1)]))
        text = report.to_text()
        assert "需人工复核" not in text

    def test_每种原因码都有中文标签(self):
        """防止新增原因码却忘了解释，人工兜底时看不懂。"""
        from redactor.handlers.base import HandlerError

        codes = {
            HandlerError.UNSUPPORTED,
            HandlerError.ENCRYPTED,
            HandlerError.CORRUPTED,
            HandlerError.PERMISSION,
            HandlerError.IO_ERROR,
            HandlerError.OUTPUT_EXISTS,
            HandlerError.OUTPUT_SAME,
            HandlerError.EMPTY,
            HandlerError.UNKNOWN,
        }
        assert codes <= set(REASON_LABELS), f"缺少中文标签：{codes - set(REASON_LABELS)}"

    def test_json可被解析(self):
        report = Report()
        report.add(FileResult(source="a.txt", hits=[HitStat("手机号", 2, ("第 1 行",))]))
        data = json.loads(report.to_json())
        assert data["files"][0]["hits"][0]["rule"] == "手机号"

    def test_save到文件(self, tmp_path):
        report = Report()
        report.add(FileResult(source="a.txt", hits=[HitStat("手机号", 1)]))
        target = tmp_path / "r.json"
        report.save(target, as_json=True)
        assert json.loads(target.read_text(encoding="utf-8"))["totals"]["hits"] == 1


class TestCliDryRun:
    """补充约束 B：dry-run 只统计。"""

    def test_dry_run不产生任何文件(self, workdir, tmp_path, capsys):
        out = tmp_path / "redacted_output"
        code = cli.main(["dry-run", str(workdir), "-o", str(out)])
        capsys.readouterr()

        assert code in (0, 1)
        assert not out.exists(), "dry-run 绝不能创建输出目录"

    def test_dry_run报告命中位置(self, workdir, tmp_path, capsys):
        cli.main(["dry-run", str(workdir), "-o", str(tmp_path / "o")])
        out = capsys.readouterr().out
        assert "试运行" in out
        assert "手机号" in out
        assert "第 1 行" in out

    def test_dry_run报告中无原文(self, workdir, tmp_path, capsys):
        cli.main(["dry-run", str(workdir), "-o", str(tmp_path / "o")])
        assert FAKE_PHONE_1 not in capsys.readouterr().out

    def test_json格式报告(self, workdir, tmp_path, capsys):
        cli.main(["dry-run", str(workdir), "-o", str(tmp_path / "o"), "--report-format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["totals"]["by_rule"]["手机号"] == 1


class TestCliRun:
    """正式处理。"""

    def test_生成输出且保留相对结构(self, workdir, tmp_path, capsys):
        out = tmp_path / "redacted_output"
        cli.main(["run", str(workdir), "-o", str(out)])
        capsys.readouterr()

        assert (out / "a_redacted.txt").is_file()
        content = (out / "a_redacted.txt").read_text(encoding="utf-8")
        assert FAKE_PHONE_1 not in content

    def test_源文件完全未改动(self, workdir, tmp_path, capsys):
        """批量跑完后，源目录必须一个字节都没变。"""
        before = {p.name: p.read_bytes() for p in workdir.iterdir() if p.is_file()}
        cli.main(["run", str(workdir), "-o", str(tmp_path / "out")])
        capsys.readouterr()
        after = {p.name: p.read_bytes() for p in workdir.iterdir() if p.is_file()}

        assert before == after
        assert sorted(before) == sorted(after), "源目录不得新增或删除文件"

    def test_失败文件进报告且退出码为1(self, workdir, tmp_path, capsys):
        code = cli.main(["run", str(workdir), "-o", str(tmp_path / "out")])
        out = capsys.readouterr().out

        assert code == 1, "存在未能处理的文件时必须返回非零码"
        assert "需要人工处理的文件" in out
        assert "old.doc" in out
        assert "empty.txt" in out

    def test_失败原因可读(self, workdir, tmp_path, capsys):
        cli.main(["run", str(workdir), "-o", str(tmp_path / "out")])
        out = capsys.readouterr().out
        assert "格式不支持" in out or ".docx" in out
        assert "文件为空" in out

    def test_报告可另存(self, workdir, tmp_path, capsys):
        report_path = tmp_path / "report.json"
        cli.main(
            [
                "run",
                str(workdir),
                "-o",
                str(tmp_path / "out"),
                "--report",
                str(report_path),
                "--report-format",
                "json",
            ]
        )
        capsys.readouterr()
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["totals"]["files"] == 4
        assert FAKE_PHONE_1 not in report_path.read_text(encoding="utf-8")

    def test_only限定规则(self, workdir, tmp_path, capsys):
        out = tmp_path / "out"
        cli.main(["run", str(workdir), "-o", str(out), "--only", "邮箱"])
        capsys.readouterr()

        content = (out / "a_redacted.txt").read_text(encoding="utf-8")
        assert "[邮箱]" in content
        assert FAKE_PHONE_1 in content, "未被 --only 选中的规则不应生效"

    def test_numbered开关(self, workdir, tmp_path, capsys):
        out = tmp_path / "out"
        cli.main(["run", str(workdir), "-o", str(out), "--numbered"])
        capsys.readouterr()
        assert "[手机号#1]" in (out / "a_redacted.txt").read_text(encoding="utf-8")

    def test_重复运行第二次因输出存在被拒(self, workdir, tmp_path, capsys):
        out = tmp_path / "out"
        cli.main(["run", str(workdir), "-o", str(out)])
        capsys.readouterr()
        first = (out / "a_redacted.txt").read_text(encoding="utf-8")

        code = cli.main(["run", str(workdir), "-o", str(out)])
        capsys.readouterr()
        assert code == 1
        assert (out / "a_redacted.txt").read_text(encoding="utf-8") == first, "已有输出不得被覆盖"

    def test_overwrite_output允许重跑(self, workdir, tmp_path, capsys):
        out = tmp_path / "out"
        cli.main(["run", str(workdir), "-o", str(out)])
        capsys.readouterr()
        cli.main(["run", str(workdir), "-o", str(out), "--overwrite-output"])
        capsys.readouterr()
        assert (out / "a_redacted.txt").is_file()

    def test_递归子目录(self, tmp_path, capsys):
        src = tmp_path / "src"
        (src / "子目录").mkdir(parents=True)
        (src / "子目录" / "n.txt").write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        out = tmp_path / "out"

        cli.main(["run", str(src), "-o", str(out)])
        assert not (out / "子目录").exists(), "未加 -r 不应进入子目录"

        cli.main(["run", str(src), "-o", str(out), "-r", "--overwrite-output"])
        capsys.readouterr()
        assert (out / "子目录" / "n_redacted.txt").is_file()

    def test_路径不存在算失败(self, tmp_path, capsys):
        code = cli.main(["run", str(tmp_path / "nope")])
        capsys.readouterr()
        assert code == 1


class TestSingleFileInput:
    """回归测试：单文件输入（而非目录输入）。

    这组用例针对一个单元测试全绿、但 CLI 直接跑却崩溃的缺陷：
    单文件输入时 ``input_root`` 就是文件自身，
    ``source.relative_to(input_root)`` 抛 ValueError，
    相对路径落到 except 分支变成空名 ``Path()``，
    再对它调 ``with_name`` 就抛 ``ValueError: has an empty name``。
    原有测试全部用目录输入或让 input_root 指向父目录，恰好绕开了这条分支。
    """

    def test_relative_to对自身返回点且with_name会抛异常(self):
        """把这个陷阱本身钉住：单文件输入时 input_root 就是文件自身。

        ``file.relative_to(file)`` 返回 ``Path('.')``，其 ``.name`` 是空串，
        对它调 ``with_name`` 抛 ``ValueError: has an empty name``。
        ``_relative_output_path`` 的守卫条件正是针对这一形态。
        """
        target = Path("samples/demo.txt")
        relative = target.relative_to(target)

        assert relative == Path(".")
        assert relative.name == ""
        with pytest.raises(ValueError):
            relative.with_name("x.txt")

    def test_守卫条件覆盖空名形态(self):
        """守卫条件必须能识别上面那个空名路径。"""
        assert Path() == Path(".")
        assert Path().name == ""
        assert Path(".").name == ""

    def test_单文件输入不崩溃且产出正确路径(self, tmp_path, capsys):
        src = tmp_path / "demo.txt"
        src.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        out = tmp_path / "redacted_output"

        code = cli.main(["run", str(src), "-o", str(out)])
        capsys.readouterr()

        assert code == 0
        assert (out / "demo_redacted.txt").is_file()
        assert "[手机号]" in (out / "demo_redacted.txt").read_text(encoding="utf-8")

    def test_单文件dry_run可用(self, tmp_path, capsys):
        src = tmp_path / "demo.txt"
        src.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        out = tmp_path / "redacted_output"

        code = cli.main(["dry-run", str(src), "-o", str(out)])
        capsys.readouterr()

        assert code == 0
        assert not out.exists()

    def test_相对路径输入也可用(self, tmp_path, monkeypatch, capsys):
        """用相对路径调用，模拟用户在项目目录里直接敲命令的情形。"""
        src = tmp_path / "demo.txt"
        src.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        code = cli.main(["run", "demo.txt", "-o", "out"])
        capsys.readouterr()

        assert code == 0
        assert (tmp_path / "out" / "demo_redacted.txt").is_file()

    def test_单文件源未被改动(self, tmp_path, capsys):
        src = tmp_path / "demo.txt"
        src.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        before = src.read_bytes()

        cli.main(["run", str(src), "-o", str(tmp_path / "out")])
        capsys.readouterr()
        assert src.read_bytes() == before

    def test_多个单文件输入(self, tmp_path, capsys):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        b.write_text(f"邮箱 {FAKE_EMAIL}", encoding="utf-8")
        out = tmp_path / "out"

        code = cli.main(["run", str(a), str(b), "-o", str(out)])
        capsys.readouterr()

        assert code == 0
        assert (out / "a_redacted.txt").is_file()
        assert (out / "b_redacted.txt").is_file()


class TestCliRuleErrors:
    """补充约束 C：规则错误必须报错退出并指明行号。"""

    def test_yaml语法错退出码2且指明行号(self, tmp_path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("rules:\n  - name: A\n     pattern: 'x'\n", encoding="utf-8")
        code = cli.main(["run", str(tmp_path), "--rules", str(bad)])
        err = capsys.readouterr().err

        assert code == 2
        assert "规则错误" in err
        assert "行" in err

    def test_正则编译错退出码2且指明行号(self, tmp_path, capsys):
        src = tmp_path / "s.txt"
        src.write_text("x", encoding="utf-8")
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "rules:\n  - name: 甲\n    pattern: '(?<\\d)'\n", encoding="utf-8"
        )
        code = cli.main(["run", str(src), "--rules", str(bad)])
        err = capsys.readouterr().err

        assert code == 2
        assert "正则编译失败" in err
        assert "第 3 行" in err

    def test_绝不静默跳过坏规则(self, tmp_path, capsys):
        """一条规则坏掉时，必须整个任务中止，而不是"处理其余规则"。"""
        src = tmp_path / "s.txt"
        src.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        out = tmp_path / "out"
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "rules:\n"
            "  - name: 好规则\n    pattern: '1[3-9]\\d{9}'\n"
            "  - name: 坏规则\n    pattern: '(?<\\d)'\n",
            encoding="utf-8",
        )
        code = cli.main(["run", str(src), "-o", str(out), "--rules", str(bad)])
        capsys.readouterr()

        assert code == 2
        assert not out.exists(), "规则有错时不得产出任何文件"

    def test_规则文件不存在退出码2(self, tmp_path, capsys):
        code = cli.main(["run", str(tmp_path), "--rules", str(tmp_path / "nope.yaml")])
        assert code == 2
        assert "不存在" in capsys.readouterr().err

    def test_only指定不存在的规则名报错(self, tmp_path, capsys):
        src = tmp_path / "s.txt"
        src.write_text("x", encoding="utf-8")
        code = cli.main(["run", str(src), "--only", "不存在的规则"])
        assert code == 2
        assert "不存在" in capsys.readouterr().err


class TestCliRulesCommand:
    """rules 子命令。"""

    def test_列出规则(self, capsys):
        assert cli.main(["rules"]) == 0
        out = capsys.readouterr().out
        assert "手机号" in out
        assert "已支持的文件类型" in out

    def test_离线承诺在帮助里可见(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        out = capsys.readouterr().out
        assert "不联网" in out
        assert "绝不覆盖原文件" in out


class TestOfflineGuarantee:
    """离线承诺的机器可验证部分。"""

    def test源码不含网络库导入(self):
        """产品核心承诺是绝不联网——用测试守住这条线。

        注意区分：``urllib.parse`` 是**纯字符串编解码**（quote/unquote/urlparse），
        实测其模块内不含 socket/urlopen/connect 任何网络能力，可以安全使用
        （docx_url.py 用它对占位符做 URL 编码）。
        真正要禁的是 ``urllib.request`` / ``urllib.error`` / ``http`` / ``socket``。
        """
        banned = (
            "import requests",
            "import urllib.request",
            "from urllib.request",
            "from urllib.error",
            "import http.client",
            "import socket",
            "import httpx",
            "import aiohttp",
        )
        offenders = []
        for path in (PROJECT_ROOT / "src").rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            for token in banned:
                if token in content:
                    offenders.append(f"{path.name}: {token}")
        assert not offenders, f"发现网络相关导入：{offenders}"

    def test_允许的urllib子模块确认无网络能力(self):
        """``urllib.parse`` 必须在本地按需核验，确保它确实不含网络符号。

        这条测试的价值：万一将来有人把 ``urllib.request`` 的功能混进来，
        上面的字符串匹配可能漏掉更隐蔽的写法（如 ``__import__``），
        用真实的模块属性检查兜底。
        """
        import urllib.parse

        names = [n for n in dir(urllib.parse) if not n.startswith("_")]
        network_like = [
            n for n in names if any(k in n.lower() for k in ("socket", "urlopen", "connect"))
        ]
        assert not network_like, f"urllib.parse 出现网络相关符号：{network_like}"

    def test依赖中无网络库(self):
        content = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for banned in ("requests", "httpx", "urllib3", "aiohttp", "boto3"):
            assert banned not in content, f"依赖中出现网络库：{banned}"

class TestReportEncoding:
    """报告落盘的编码约定（Windows 中文环境下 type 乱码问题）。

    背景：Windows 的 ``type`` / 记事本按系统默认编码（简体中文 Windows 为
    GBK）解码无 BOM 的 UTF-8，中文显示为乱码。文本报告因此加UTF-8 BOM。

    **JSON 绝不能加 BOM**——``json.load()`` 遇 BOM 会抛
    ``Unexpected UTF-8 BOM``，那会让下游系统读取失败，是净损失。
    """

    def _report(self) -> Report:
        report = Report(dry_run=True)
        report.add(
            FileResult(
                source="a.docx",
                status="ok",
                hits=[HitStat("手机号", 1, ("第 1 段",))],
            )
        )
        return report

    def test_默认不加BOM(self, tmp_path):
        """默认保持无 BOM（标准做法，跨平台一致），不改变既有行为。"""
        path = tmp_path / "r.txt"
        self._report().save(str(path))
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_bom为False时明确不加(self, tmp_path):
        path = tmp_path / "r.txt"
        self._report().save(str(path), bom=False)
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_bom为True时写入UTF8_BOM(self, tmp_path):
        """bom=True 应写入标准 UTF-8 BOM 头（EF BB BF）。"""
        path = tmp_path / "r.txt"
        self._report().save(str(path), bom=True)
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_bom文件用utf8_sig可正确解码(self, tmp_path):
        """带 BOM 的文件必须能被正确读回，且中文内容不损坏。"""
        path = tmp_path / "r.txt"
        self._report().save(str(path), bom=True)
        text = path.read_text(encoding="utf-8-sig")
        assert "手机号" in text
        assert "本地文件脱敏报告" in text

    def test_bom文件用普通utf8解码会带BOM字符(self, tmp_path):
        """记录一个事实：带 BOM 的文件按纯 utf-8 读，首部会多出 BOM 字符。

        这不是缺陷而是取舍——它证明"用 utf-8-sig 读"才是正确姿势。
        """
        path = tmp_path / "r.txt"
        self._report().save(str(path), bom=True)
        text = path.read_text(encoding="utf-8")
        assert text.startswith("﻿")

    def test_JSON报告即使传bom也不加(self, tmp_path):
        """JSON 路径不受 bom 参数影响——必须保持无 BOM 才能被 json.load 读取。"""
        path = tmp_path / "r.json"
        self._report().save(str(path), as_json=True, bom=True)
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")

    def test_JSON报告可被json_load解析(self, tmp_path):
        """端到端：加不加 BOM，JSON 都必须能被标准 json.load 读取。"""
        for bom in (None, False, True):
            path = tmp_path / f"r_{bom}.json"
            self._report().save(str(path), as_json=True, bom=bom)
            data = json.loads(path.read_text(encoding="utf-8"))
            assert "files" in data, f"bom={bom} 时 JSON 结构异常"

    def test_两种模式内容一致仅BOM不同(self, tmp_path):
        """加 BOM 不改变报告内容本身。"""
        plain = tmp_path / "a.txt"
        bommed = tmp_path / "b.txt"
        self._report().save(str(plain))
        self._report().save(str(bommed), bom=True)
        assert plain.read_bytes() == bommed.read_bytes().lstrip(b"\xef\xbb\xbf")

    def test_换行符统一为LF(self, tmp_path):
        """跨平台一致：报告恒用 LF，不随系统变成 CRLF。"""
        for bom in (None, True):
            path = tmp_path / f"r_{bom}.txt"
            self._report().save(str(path), bom=bom)
            assert b"\r\n" not in path.read_bytes(), f"bom={bom} 时出现 CRLF"

    def test_CLI文本报告落盘带BOM(self, tmp_path, capsys):
        """端到端：``--report xxx.txt`` 产出的文件带 BOM。

        这里自建干净目录而非复用 workdir fixture——后者刻意含一个
        必然失败的旧版 .doc，会让退出码为 1，与本用例无关。
        """
        src = tmp_path / "干净目录"
        src.mkdir()
        (src / "a.txt").write_text(f"手机 {FAKE_PHONE_1}\n", encoding="utf-8")
        report_path = tmp_path / "报告.txt"
        code = cli.main([
            "dry-run", str(src),
            "--report", str(report_path),
            "--report-format", "text",
        ])
        capsys.readouterr()
        assert code == 0
        assert report_path.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_CLI_JSON报告落盘不带BOM(self, tmp_path, capsys):
        """端到端：``--report xxx.json`` 产出无 BOM，且可被 json.load 读取。"""
        src = tmp_path / "干净目录2"
        src.mkdir()
        (src / "a.txt").write_text(f"手机 {FAKE_PHONE_1}\n", encoding="utf-8")
        report_path = tmp_path / "报告.json"
        code = cli.main([
            "dry-run", str(src),
            "--report", str(report_path),
            "--report-format", "json",
        ])
        capsys.readouterr()
        assert code == 0
        raw = report_path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        data = json.loads(raw.decode("utf-8"))
        assert "files" in data
