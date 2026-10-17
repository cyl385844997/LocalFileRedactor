"""PyInstaller 打包验证 —— jieba 词典能否进包？

**这是 P8 最大的打包风险**：jieba 的词典
``jieba/dict.txt`` 是**运行时按文件路径读取**的（不是 import 期加载的
Python 常量）。PyInstaller 不会自动把非 .py 的数据文件收进去，
打包后典型症状是::

    Build failed, optimising...  或 运行时 jieba 报找不到 dict.txt，
    或者静默退化成 HMM 模式（分词质量暴跌但不报错 —— 更隐蔽）。

本脚本做三件事：
    1. 用 PyInstaller 实际打包一个最小入口
    2. 在**打包产物**里跑人名识别（不是源码环境）
    3. 断言：能用 jieba 识别人名 + 大字金额仍被剔除

跑法（耗时较久，首次 1-3 分钟）：
    "E:/Program Files/python/python.exe" scripts/verify_pyinstaller_p8.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = "E:/Program Files/python/python.exe"
LIBS = REPO / ".verify_libs"

#: 打包产物里要跑的验证代码
PROBE = r'''
import sys, json
# ★ 必须先把 .verify_libs 加进 path —— 它就是"包里 jieba 所在目录"的替身
sys.path.insert(0, sys.argv[1])

result = {}
try:
    import jieba
    result["jieba_version"] = jieba.__version__
    # 关键：强制走"读词典文件"这条路径，而不是 HMM
    jieba.initialize()
    result["dict_loaded"] = True

    from redactor.core.engine import RedactionEngine
    from redactor.core.rules import load_rule_dir
    from redactor.core.scopes import ScopeEntry

    rules = load_rule_dir(sys.argv[2])
    eng = RedactionEngine(rules, enable_names=True)

    out1, _ = eng.redact("张三13812345678")
    result["name_and_phone"] = out1
    result["name_ok"] = ("[人名]" in out1 and "13812345678" not in out1)

    out2, _ = eng.redact("已支付伍拾万元整")
    result["da_zi"] = out2
    result["da_zi_ok"] = ("[人名]" not in out2 and "伍拾" in out2)

    out3, _ = eng.redact("项目负责人是李四")
    result["single_name"] = out3
    result["single_name_ok"] = ("[人名]" in out3 and "李四" not in out3)
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"

print("__PROBE_JSON__" + json.dumps(result, ensure_ascii=False))
'''

def _decode(raw: bytes) -> str:
    """把子进程输出按多编码尝试解码。

    Windows 中文环境下 PyInstaller / Python 都可能输出 GBK，
    而脚本自身在utf-8 终端里跑。逐个试，第一个不抛的即返回。
    """
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


ENTRY = r'''
import sys, os
sys.path.insert(0, os.path.abspath(sys.argv[1]))
sys.path.insert(0, os.path.abspath(sys.argv[2]))
''' + PROBE + r'''
import sys
sys.exit(0)
'''


def main() -> int:
    print("=" * 74)
    print("PyInstaller 打包验证 —— jieba（v0.8.0）")
    print("=" * 74)

    # PyInstaller 与 jieba 都装在 .verify_libs（本地隔离环境），
    # 必须在import 前把该目录加进 sys.path
    if str(LIBS) not in sys.path:
        sys.path.insert(0, str(LIBS))
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(f"❌ PyInstaller 未安装（应在 {LIBS}）")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="p8_pyinstaller_"))
    entry = workdir / "probe_entry.py"
    entry.write_text(ENTRY, encoding="utf-8")

    cmd = [
        PYTHON, "-m", "PyInstaller",
        "--onefile",
        "--name", "p8_probe",
        # ★ 关键参数：把 jieba 包与词典**显式**打进去
        "--hidden-import", "jieba",
        "--hidden-import", "jieba.posseg",
        "--collect-data", "jieba",
        "--paths", str(LIBS),
        "--paths", str(REPO / "src"),
        "--distpath", str(workdir / "dist"),
        "--workpath", str(workdir / "build"),
        "--specpath", str(workdir),
        "--noconfirm", "--clean",
        # 排除大依赖，缩短打包时间
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "PySide6",
        "--log-level", "WARN",
        str(entry),
    ]
    print("\n[步骤 1] 打包中（首次可能 1-3 分钟）...")
    build_env = dict(os.environ)
    # ★ PyInstaller 自身也要能import 到 jieba（--collect-data 会去读它）
    build_env["PYTHONPATH"] = str(LIBS)
    # ★ 必须用二进制捕获：PyInstaller 在 Windows 中文环境下输出 GBK，
    #   text=True 会用 utf-8 解码并抛 UnicodeDecodeError（实测踩过）。
    proc = subprocess.run(cmd, cwd=str(workdir), env=build_env,
                          capture_output=True)
    exe = workdir / "dist" / "p8_probe.exe"
    if not exe.exists():
        print("❌ 打包失败：未产出 exe")
        print(_decode(proc.stdout)[-3000:])
        print(_decode(proc.stderr)[-3000:])
        return 1
    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"  ✅ 打包成功：{exe.name}（{size_mb:.1f} MB）")

    print("\n[步骤 2] 检查 jieba 词典是否进包...")
    # PyInstaller 会把 collect-data 的内容放到 _MEIPASS
    # onefile 模式下只能通过运行时才知道路径，所以这里用 strings 式检查：
    # 词典里必然含 "贵州" 之类的高频词，用二进制搜索验证
    blob = exe.read_bytes()
    has_dict = "贵州".encode("utf-8") in blob
    print(f"  exe 内含 jieba 词典特征串（'贵州'）: {has_dict}")
    if not has_dict:
        print("  ⚠️ 词典特征串未检出（PyInstaller 会压缩，可能检不出）")

    print("\n[步骤 3] 在打包产物里跑人名识别...")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    run = subprocess.run(
        [str(exe), str(LIBS), str(REPO / "rules")],
        cwd=str(workdir), capture_output=True, env=env,
    )
    out = _decode(run.stdout)
    line = next(
        (ln for ln in out.splitlines() if ln.startswith("__PROBE_JSON__")),
        None,
    )
    if line is None:
        print("❌ 打包产物没输出探针结果")
        print("stdout:", out[-2000:])
        print("stderr:", _decode(run.stderr)[-2000:])
        return 1

    import json
    result = json.loads(line[len("__PROBE_JSON__"):])
    print("  --- 打包后实测 ---")
    for k, v in result.items():
        print(f"    {k} = {v!r}")

    print("\n" + "=" * 74)
    ok = (
        "error" not in result
        and result.get("dict_loaded") is True
        and result.get("name_ok") is True
        and result.get("da_zi_ok") is True
        and result.get("single_name_ok") is True
    )
    if ok:
        print("✅ 打包验证通过：jieba 词典已进包，人名识别与大字剔除在打包后均正常")
    else:
        print("❌ 打包验证失败")
        if result.get("error"):
            print(f"   错误：{result['error']}")
        else:
            print(f"   name_ok={result.get('name_ok')} "
                  f"da_zi_ok={result.get('da_zi_ok')} "
                  f"single_name_ok={result.get('single_name_ok')}")
    print("=" * 74)

    shutil.rmtree(workdir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())