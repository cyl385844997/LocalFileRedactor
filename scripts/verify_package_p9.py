"""打包产物验证（v1.0.0）—— 4 项核心能力，全部必须在**打包后**验证。

跑法：
    "E:/Program Files/python/python.exe" scripts/verify_package_p9.py

★为什么要"在打包产物内"验证：
    源码环境全绿**完全推断不出**打包问题。实测踩过的坑：
      - jieba 的 dict.txt 没进包 → **不报错**，静默退化成 HMM 模式
      - rules/ 没进包 → 找不到规则集
    这两类问题只有真跑 exe 才会暴露。

4 项验证：
    1. 纯净目录可运行   —— 复制到无Python 的目录，offscreen 起GUI
    2. rules/ 路径生效  —— 手机号 / 身份证必须被替换
    3. jieba 词典生效   —— 勾选人名识别后"张三"必须变[人名]
    4. 范围泛化生效     —— 配"公司"后缀，"重庆某某有限公司" → [公司]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXE = REPO / "dist" / "LocalFileRedactor.exe"

PHONE = "13812345678"
ID_CARD = "110101199001011237"       # 校验位合法（已实测能过 id_card validator）

#: 打进 exe 的探针代码 —— 它在**纯净目录**里跑，只能用 exe 内的东西
#:
#: ★为什么用环境变量而不是命令行参数：
#:   exe 是 **windowed**（console=False），只跑 redactor.gui.main:main()，
#:   **不会**执行 argv 里的脚本路径。唯一可靠的注入通道是环境变量
#:   REDACTOR_PROBE_SCRIPT（见 gui/main.py::_run_probe_if_requested）。
PROBE = r'''
import sys, json, os, tempfile, shutil
from pathlib import Path

# ★探针是**独立文件**（runpy 执行），外层的 PHONE / ID_CARD 常量
#   在它的作用域里**不存在** —— 必须自己定义。
PHONE = "13812345678"
ID_CARD = "110101199001011237"

result = {}

# exe 是 windowed 的（无控制台），失败时不会弹窗，只能写文件回报
report = Path(os.environ["REDACTOR_PROBE_REPORT"])

try:
    result["meipass"] = getattr(sys, "_MEIPASS", None)
    result["frozen"] = bool(getattr(sys, "frozen", False))

    # ---- 验证 2：rules/ 路径 ----
    from redactor.core.rules import load_rule_dir
    from redactor.cli import _default_rules_dir
    rules_dir = _default_rules_dir()
    result["rules_dir"] = str(rules_dir)
    result["rules_dir_exists"] = rules_dir.is_dir()
    rules = load_rule_dir(rules_dir)
    result["rule_count"] = len(rules)
    result["rule_names"] = [r.name for r in rules]

    # ---- 验证 3：jieba 词典 ----
    try:
        import jieba
        result["jieba_version"] = jieba.__version__
        jieba.initialize()               # ★强制走"读dict.txt"路径
        result["jieba_dict_loaded"] = True
    except Exception as exc:
        result["jieba_dict_loaded"] = False
        result["jieba_error"] = f"{type(exc).__name__}: {exc}"

    # ---- 端到端：真造一个 docx，跑完整 GUI 流程 ----
    from PySide6 import QtWidgets
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from docx import Document
    from redactor.core.scopes import ScopeEntry
    from redactor.gui import MainWindow

    tmp = Path(tempfile.mkdtemp(prefix="p9_probe_"))
    src = tmp / "in" / "contract.docx"
    src.parent.mkdir(parents=True, exist_ok=True)
    d = Document()
    d.add_paragraph(f"甲方代表张三，电话{PHONE}，身份证{ID_CARD}")
    d.add_paragraph("乙方为重庆创锐电气技术有限公司")
    d.save(str(src))

    w = MainWindow()
    w.show()
    app.processEvents()

    # 验证 2 + 3 + 4 一起跑
    w.drop_zone().add_files([str(src)])
    w.apply_scope_entries([
        ScopeEntry(name="单位",
                   suffixes=("有限公司", "公司", "集团", "大学", "项目"))
    ])
    w.names_action().setChecked(True)          # 开启人名识别
    app.processEvents()
    result["names_enabled"] = w.controller().names_enabled()

    out_root = tmp / "out"
    started = w.start_processing(output_root=out_root)

    import time
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and w.state() != "done":
        app.processEvents()
        time.sleep(0.05)
    result["state"] = w.state()
    w.close()

    out_file = out_root / "contract_redacted.docx"
    result["output_exists"] = out_file.exists()
    if out_file.exists():
        text = "\n".join(p.text for p in Document(str(out_file)).paragraphs)
        result["output_text"] = text
        result["phone_ok"] = PHONE not in text and "[手机号]" in text
        result["id_ok"] = ID_CARD not in text and "[身份证号]" in text
        result["name_ok"] = "张三" not in text and "[人名]" in text
        result["scope_ok"] = ("重庆创锐电气技术有限公司" not in text
                              and "[有限公司]" in text)
    shutil.rmtree(tmp, ignore_errors=True)

except Exception as exc:
    import traceback
    result["error"] = f"{type(exc).__name__}: {exc}"
    result["traceback"] = traceback.format_exc()[-2000:]

report.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                   encoding="utf-8")
sys.exit(0)
'''


def main() -> int:
    print("=" * 74)
    print("打包产物验证（v1.0.0）")
    print("=" * 74)

    if not EXE.exists():
        print(f"❌ 未找到产物：{EXE}")
        print("   先跑：python -m PyInstaller LocalFileRedactor.spec --clean")
        return 1

    size_mb = EXE.stat().st_size / 1024 / 1024
    print(f"\n[产物] {EXE}")
    print(f"       {size_mb:.1f} MB")

    # ================================================================
    # 验证 1：纯净目录（无 Python、无任何 .py）
    # ================================================================
    print("\n--- 验证 1：纯净目录可运行 ---")
    pure = Path(tempfile.mkdtemp(prefix="p9_pure_"))
    exe_copy = pure / EXE.name
    shutil.copy2(EXE, exe_copy)

    # 确认目录里确实没有 .py / Python 环境
    strays = [p.name for p in pure.iterdir() if p.suffix in (".py", ".pyd", ".dll")]
    print(f"  纯净目录: {pure}")
    print(f"  目录内容: {[p.name for p in pure.iterdir()]}")
    print(f"  杂散 .py/.pyd/.dll: {strays or '无'}")

    # 把探针写成 .py 交给 exe 的 python 解释器跑 —— 但 windowed exe 不接参数，
    # 所以改用环境变量 + 一次性 py 文件放在**另一个**临时目录。
    probe_dir = Path(tempfile.mkdtemp(prefix="p9_probe_src_"))
    probe_file = probe_dir / "probe_inside_exe.py"
    probe_file.write_text(PROBE, encoding="utf-8")
    report_path = probe_dir / "result.json"

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["REDACTOR_PROBE_SCRIPT"] = str(probe_file)
    env["REDACTOR_PROBE_REPORT"] = str(report_path)

    # ★配置文件必须放**系统 TEMP**，不是 exe 旁边。
    #   PyInstaller onefile 启动时把 exe 解包到 %TEMP%\_MEIxxxxxx\，
    #   sys.executable 指向**解包副本** —— 放在原 exe 旁边它根本看不到。
    #   同时也是因为打包后的 windowed exe 拿不到 subprocess 传入的环境变量。
    import json as _json
    import tempfile as _tf
    cfg_path = Path(_tf.gettempdir()) / "_redactor_probe.json"
    cfg_path.write_text(
        _json.dumps({"script": str(probe_file),
                     "report": str(report_path)}),
        encoding="utf-8",
    )
    print(f"  探针配置: {cfg_path}")
    # ★无论成败都要清掉 —— 残留会污染后续正常启动（exe 读到它就会
    #   去跑探针而不显示窗口）。见 gui/main.py 里的同名说明。
    _CFG_PATH_TO_CLEAN = cfg_path

    print("\n  [在纯净目录里跑 exe 内的探针]")
    try:
        proc = subprocess.run(
            [str(exe_copy)],
            cwd=str(pure), env=env, capture_output=True,
            timeout=45,
        )
        print(f"  exe 退出码 = {proc.returncode}")
    except subprocess.TimeoutExpired:
        # ★windowed exe 若探针钩子没触发，会直接进 GUI 事件循环永不退出。
        #   用"到达标记"区分卡点 —— 没有这个就只能猜，白白浪费打包轮次：
        #     标记文件不存在 → 环境变量没传进 exe，或钩子没编进去
        #     标记是 hook-entered → 进了钩子，是探针脚本内部挂住了
        print("  ❌ exe 超时（45s）未退出")
        marker = None
        if report_path.exists():
            try:
                marker = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                marker = {"raw": report_path.read_text(encoding="utf-8")[:500]}
        if marker is None:
            print("     卡点：报告文件不存在 → 钩子**根本没被触发**")
            print("     可能：环境变量没传进 exe（windowed exe 正常继承，")
            print("           但若被清理器/沙箱过滤则收不到）")
            print("           或 exe 里的 main.py 是旧版本（没重新打包）")
        elif marker.get("stage") == "hook-entered":
            print("     卡点：钩子已进入，探针脚本内部挂住（GUI 事件循环未返回）")
        else:
            print(f"     卡点：钩子内部异常 → {marker}")
        shutil.rmtree(pure, ignore_errors=True)
        shutil.rmtree(probe_dir, ignore_errors=True)
        return 1

    import json
    if not report_path.exists():
        print(f"❌ 探针没写出报告文件 —— exe 内可能崩了")
        out = proc.stdout or b""
        err = proc.stderr or b""
        for raw, label in ((out, "stdout"), (err, "stderr")):
            for enc in ("utf-8", "gbk", "cp936", "latin-1"):
                try:
                    txt = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                txt = repr(raw)
            if txt.strip():
                print(f"  {label}: {txt[-1500:]}")
        shutil.rmtree(pure, ignore_errors=True)
        shutil.rmtree(probe_dir, ignore_errors=True)
        return 1

    # 清理残留配置（双保险：exe 侧已删，这里再删一次）
    try:
        _CFG_PATH_TO_CLEAN.unlink(missing_ok=True)
    except Exception:
        pass

    result = json.loads(report_path.read_text(encoding="utf-8"))
    print("\n  --- exe 内实测结果 ---")
    for k, v in result.items():
        if k in ("traceback", "output_text"):
            continue
        print(f"    {k} = {v!r}")
    if result.get("output_text"):
        print("    --- 脱敏后正文 ---")
        for line in result["output_text"].splitlines():
            print(f"      {line}")

    shutil.rmtree(pure, ignore_errors=True)
    shutil.rmtree(probe_dir, ignore_errors=True)

    # ================================================================
    # 判定
    # ================================================================
    print("\n" + "=" * 74)
    print("判定")
    print("=" * 74)

    checks = [
        ("验证 1 纯净目录可运行",
         bool(result.get("frozen")) and "error" not in result),
        # ★阈值 5 不是 6：rules/builtin.yaml 里 IPv6地址 默认 enabled: false，
        #   load_rule_dir 只返回**启用**的规则，实测就是 5 条
        #   （手机号/身份证号/银行卡号/邮箱/IP地址）。
        #   我最初写 >=6 导致"验证 2a"假红 —— 又一次"假设 vs 实测"的错。
        ("验证 2a rules/ 路径生效",
         result.get("rules_dir_exists") is True
         and result.get("rule_count", 0) >= 5),
        ("验证 2b 基础正则生效（手机号）",
         result.get("phone_ok") is True),
        ("验证 2c 基础正则生效（身份证）",
         result.get("id_ok") is True),
        ("验证 3 jieba 词典生效（人名）",
         result.get("jieba_dict_loaded") is True
         and result.get("names_enabled") is True
         and result.get("name_ok") is True),
        ("验证 4 范围泛化生效",
         result.get("scope_ok") is True),
    ]

    all_ok = True
    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")
        all_ok = all_ok and ok

    if "error" in result:
        print(f"\n  exe 内异常：{result['error']}")
        if result.get("traceback"):
            print(f"  {result['traceback']}")

    print("\n" + "=" * 74)
    print(f"✅ 全部 {len(checks)} 项通过" if all_ok else "❌ 有项目未通过")
    print("=" * 74)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())