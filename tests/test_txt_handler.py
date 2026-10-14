"""txt handler 与安全底线测试（永不覆盖原文件）。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from fixtures.make_samples import FAKE_BANK_CARD_BAD, FAKE_EMAIL, FAKE_ID_INVALID, FAKE_PHONE_1
from redactor.core.engine import RedactionEngine
from redactor.core.rules import load_rules
from redactor.handlers import get_handler
from redactor.handlers.base import HandlerError
from redactor.handlers.txt_handler import TxtHandler

BUILTIN_RULES = Path(__file__).resolve().parents[1] / "rules" / "builtin.yaml"


@pytest.fixture()
def engine():
    return RedactionEngine(load_rules(BUILTIN_RULES))


@pytest.fixture()
def sample(tmp_path):
    path = tmp_path / "demo.txt"
    path.write_text(f"手机 {FAKE_PHONE_1}\n邮箱 {FAKE_EMAIL}\n", encoding="utf-8")
    return path


class TestTxtProcess:
    """基本处理。"""

    def test_输出文件生成且原文不出现(self, sample, tmp_path, engine):
        output = tmp_path / "out" / "demo_redacted.txt"
        result = TxtHandler().process(sample, output, engine)

        assert output.is_file()
        content = output.read_text(encoding="utf-8")
        assert FAKE_PHONE_1 not in content
        assert FAKE_EMAIL not in content
        assert "[手机号]" in content and "[邮箱]" in content
        assert result.total_hits == 2

    def test_源文件字节级不变(self, sample, engine):
        """核心安全断言：源文件内容必须逐字节一致。"""
        before = sample.read_bytes()
        before_mtime = sample.stat().st_mtime_ns

        TxtHandler().process(sample, sample.parent / "out.txt", engine)

        assert sample.read_bytes() == before
        assert sample.stat().st_mtime_ns == before_mtime

    def test_dry_run不写任何文件(self, sample, tmp_path, engine):
        output = tmp_path / "out" / "demo_redacted.txt"
        result = TxtHandler().process(sample, output, engine, dry_run=True)

        assert not output.exists()
        assert not output.parent.exists()
        assert result.output is None
        # 但统计信息依然完整，供人工评估
        assert result.total_hits == 2

    def test_gbk编码自动识别(self, tmp_path, engine):
        source = tmp_path / "gbk.txt"
        source.write_bytes(f"手机 {FAKE_PHONE_1}\n".encode("gb18030"))
        output = tmp_path / "out.txt"

        TxtHandler().process(source, output, engine)
        assert "[手机号]" in output.read_text(encoding="utf-8")

    def test_换行结构保留(self, tmp_path, engine):
        source = tmp_path / "multi.txt"
        source.write_text(f"行1 {FAKE_PHONE_1}\n行2\n行3 {FAKE_EMAIL}\n", encoding="utf-8")
        output = tmp_path / "out.txt"

        TxtHandler().process(source, output, engine)
        assert output.read_text(encoding="utf-8") == "行1 [手机号]\n行2\n行3 [邮箱]\n"

    def test_位置报告到行号(self, tmp_path, engine):
        source = tmp_path / "loc.txt"
        source.write_text(f"第一行\n第二行 {FAKE_PHONE_1}\n", encoding="utf-8")
        output = tmp_path / "out.txt"

        result = TxtHandler().process(source, output, engine)
        stats = {s.rule_name: s for s in result.hits}
        assert stats["手机号"].locations == ("第 2 行",)

    def test_校验位错误不计入命中(self, tmp_path, engine):
        source = tmp_path / "bad.txt"
        source.write_text(f"证件 {FAKE_ID_INVALID}\n卡号 {FAKE_BANK_CARD_BAD}\n", encoding="utf-8")
        output = tmp_path / "out.txt"

        result = TxtHandler().process(source, output, engine)
        assert result.total_hits == 0
        # 文件仍被复制为脱敏副本，不报错
        assert output.is_file()


class TestSecurityBottomLine:
    """安全底线：永不覆盖原文件。"""

    def test_输出与源同路径被拒(self, sample, engine):
        with pytest.raises(HandlerError) as excinfo:
            TxtHandler().process(sample, sample, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_SAME

    def test_同路径大小写变体也被拒(self, sample, engine):
        """Windows 上 A.txt 与 a.txt 是同一文件，必须识别为冲突。"""
        tricky = sample.parent / sample.name.upper()
        if os.name != "nt":
            pytest.skip("仅 Windows 需要该断言")
        with pytest.raises(HandlerError) as excinfo:
            TxtHandler().process(sample, tricky, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_SAME

    def test_输出已存在默认拒绝(self, sample, tmp_path, engine):
        output = tmp_path / "exists.txt"
        output.write_text("原有内容", encoding="utf-8")

        with pytest.raises(HandlerError) as excinfo:
            TxtHandler().process(sample, output, engine)
        assert excinfo.value.code == HandlerError.OUTPUT_EXISTS
        # 原有内容未被破坏
        assert output.read_text(encoding="utf-8") == "原有内容"

    def test_显式允许时才覆盖输出(self, sample, tmp_path, engine):
        output = tmp_path / "exists.txt"
        output.write_text("原有内容", encoding="utf-8")

        TxtHandler().process(sample, output, engine, overwrite_output=True)
        assert "[手机号]" in output.read_text(encoding="utf-8")

    def test_dry_run也做冲突检查(self, sample, engine):
        """dry-run 必须与正式执行使用同一套安全检查，否则会给出虚假的安全感。"""
        with pytest.raises(HandlerError):
            TxtHandler().process(sample, sample, engine, dry_run=True)


class TestFailureHandling:
    """异常文件必须抛出可分类的错误（补充约束 A）。"""

    def test_空文件(self, tmp_path, engine):
        source = tmp_path / "empty.txt"
        source.write_text("", encoding="utf-8")
        with pytest.raises(HandlerError) as excinfo:
            TxtHandler().process(source, tmp_path / "o.txt", engine)
        assert excinfo.value.code == HandlerError.EMPTY

    def test_二进制文件被识别为不支持(self, tmp_path, engine):
        source = tmp_path / "fake.txt"
        source.write_bytes(b"\x00\x01\x02binary")
        with pytest.raises(HandlerError) as excinfo:
            TxtHandler().process(source, tmp_path / "o.txt", engine)
        assert excinfo.value.code == HandlerError.UNSUPPORTED

    def test_文件不存在(self, tmp_path, engine):
        with pytest.raises(HandlerError) as excinfo:
            TxtHandler().process(tmp_path / "nope.txt", tmp_path / "o.txt", engine)
        assert excinfo.value.code == HandlerError.IO_ERROR

    @pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows 上的真实行为差异")
    def test_windows只读属性不阻止读取(self, tmp_path, engine):
        """记录一个平台事实，避免以后误以为 chmod 能造出读权限错误。

        Windows 的只读属性（S_IREAD）只阻止**写入**，不阻止读取；
        POSIX 上则相反（读权限位去掉才阻止读）。
        这就是下面那条用 mock 模拟 PermissionError 的原因。
        """
        source = tmp_path / "ro.txt"
        source.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        os.chmod(source, stat.S_IREAD)
        try:
            assert source.read_bytes()  # 读得到
            with pytest.raises(PermissionError):
                with open(source, "ab"):
                    pass  # 写不了
        finally:
            os.chmod(source, stat.S_IWRITE)

    def test_无权限读取被归类为permission(self, tmp_path, engine, monkeypatch):
        """模拟 read_bytes 抛出 PermissionError，验证错误被正确归类。"""
        source = tmp_path / "locked.txt"
        source.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")

        original = Path.read_bytes

        def deny(self):
            if self.name == "locked.txt":
                raise PermissionError(13, "Permission denied")
            return original(self)

        monkeypatch.setattr(Path, "read_bytes", deny)

        with pytest.raises(HandlerError) as excinfo:
            TxtHandler().process(source, tmp_path / "o.txt", engine)
        assert excinfo.value.code == HandlerError.PERMISSION

    def test_无权限写入被归类为permission(self, tmp_path, engine, monkeypatch):
        """写入阶段被拒同样应归类为 permission_denied，而非未知错误。"""
        source = tmp_path / "src.txt"
        source.write_text(f"手机 {FAKE_PHONE_1}", encoding="utf-8")
        output = tmp_path / "out.txt"

        original = Path.write_text

        def deny(self, *args, **kwargs):
            if self.name == "out.txt":
                raise PermissionError(13, "Permission denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", deny)

        with pytest.raises(HandlerError) as excinfo:
            TxtHandler().process(source, output, engine)
        assert excinfo.value.code == HandlerError.PERMISSION


class TestRegistry:
    """handler 分派。"""

    def test_按扩展名分派(self):
        assert isinstance(get_handler(Path("a.txt")), TxtHandler)
        assert isinstance(get_handler(Path("a.md")), TxtHandler)

    def test_大写扩展名也识别(self):
        assert isinstance(get_handler(Path("A.TXT")), TxtHandler)

    def test_旧doc给友好提示(self):
        with pytest.raises(HandlerError) as excinfo:
            get_handler(Path("a.doc"))
        assert ".docx" in excinfo.value.message

    def test_未知类型报不支持(self):
        with pytest.raises(HandlerError) as excinfo:
            get_handler(Path("a.exe"))
        assert excinfo.value.code == HandlerError.UNSUPPORTED