"""把打包所需的运行时依赖装到 deps/（不入 git）。

跑法：
    "E:/Program Files/python/python.exe" scripts/install_deps.py

为什么单独放 deps/ 而不是装到系统环境：
    - 开源仓库不该包含第三方库副本（体积 + 许可）
    - 版本锁定，避免"我机器上能跑、你机器上跑不了"
    - ``deps/`` 已在 .gitignore 里

⚠️ **jieba 必须在 deps/ 里**：PyInstaller 打包时要用 ``--paths deps``
   才能把它收进 exe；装到别处 ``collect_data('jieba')`` 会收空，
   而打包后的表现是**静默退化到 HMM 模式**（不报错，分词质量暴跌）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPS = REPO / "deps"

#: 打包必需的运行时依赖 —— **全部装进 deps/**
#:
#: ★关键教训（本轮打包实测踩了 10 轮才定位）：
#: 最初这里只装 jieba，其余依赖指望"系统环境已有"。实测系统 Python
#: **根本没装 PySide6** —— 测试之所以全绿，是因为测试是通过
#: ``PYTHONPATH=.verify_libs`` 跑的，那里有 PySide6。
#: 于是打包时Analysis 报 ``missing module named PySide6``，
#: exe 体积只有 60 MB（真包该更大），启动即
#: ``ModuleNotFoundError: No module named 'PySide6'``。
#:
#: **教训：打包依赖必须显式列全，且打包与测试用同一份依赖**，
#: 不能"测试环境有就行" —— 两者是不同的 sys.path。
RUNTIME_PKGS = [
    "PySide6",        # GUI（★ 最大的一个，约 60 MB）
    "jieba",          # 人名识别（可选功能，但打包验证依赖它）
    "python-docx",    # docx handler
    "openpyxl",       # xlsx handler
    "PyMuPDF",        # pdf handler
    "PyYAML",         # 规则/词库配置解析
]

#: 打包验证需要的构建工具（不放进 exe）
BUILD_PKGS = ["pyinstaller"]


def _pip(*args: str) -> int:
    cmd = [sys.executable, "-m", "pip", *args]
    print("  $ " + " ".join(args))
    proc = subprocess.run(cmd)
    return proc.returncode


def main() -> int:
    print("=" * 70)
    print("安装打包运行时依赖 -> deps/")
    print("=" * 70)

    DEPS.mkdir(exist_ok=True)

    print("\n[1/3] 运行时依赖（进exe）")
    rc = _pip("install", "--upgrade", "--target", str(DEPS), *RUNTIME_PKGS)
    if rc != 0:
        print(f"❌ 运行时依赖安装失败（exit {rc}）")
        return rc

    print("\n[2/3] 构建工具（不进exe）")
    rc = _pip("install", "--upgrade", *BUILD_PKGS)
    if rc != 0:
        print(f"⚠️  构建工具安装失败（exit {rc}）—— 打包会失败")

    print("\n[3/3] 校验（逐个确认 deps/ 里真能import）")
    ok = True
    if str(DEPS) not in sys.path:
        sys.path.insert(0, str(DEPS))

    # ★逐个 import —— 只查目录存在是不够的：
    #   "系统能import" 与 "deps/ 能 import" 是两回事，
    #   而打包只会用 deps/（见 .spec 的 pathex）。
    import importlib
    for mod, why in [
        ("PySide6", "GUI（★ 缺了 exe 启动即崩）"),
        ("jieba", "人名识别"),
        ("docx", "docx handler"),
        ("openpyxl", "xlsx handler"),
        ("fitz", "pdf handler"),
        ("yaml", "配置解析"),
    ]:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "?")
            print(f"  ✅ {mod:<10} {ver:<12} {why}")
        except ImportError:
            print(f"  ❌ {mod:<10} {'缺失':<12} {why}")
            ok = False

    try:
        import PyInstaller
        print(f"  ✅ {'PyInstaller':<10} {PyInstaller.__version__}")
    except ImportError:
        print("  ❌ PyInstaller 未安装 —— 无法打包")
        ok = False

    print()
    if ok:
        print("下一步：python scripts/gen_app_assets.py")
        print("      python -m PyInstaller LocalFileRedactor.spec --clean --noconfirm")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())