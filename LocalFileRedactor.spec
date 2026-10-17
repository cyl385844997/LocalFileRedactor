# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（v1.0.0 单文件 exe）。

★ 三处关键（缺了就出问题，详见各处注释）：
   1. datas 里带 rules/       —— 否则打包后找不到规则集
   2. collect_data('jieba')   —— 否则 jieba **静默退化**到 HMM 模式
   3. console=False           —— 双击不弹黑框

打包命令：
    python -m pip install --target=deps jieba
    python scripts/gen_app_assets.py
    python -m PyInstaller LocalFileRedactor.spec --clean --noconfirm

产物：
    dist/LocalFileRedactor.exe
"""

import sys
from pathlib import Path

# PyInstaller 注入的变量：SPECPATH 是本 .spec 所在目录
REPO = Path(SPECPATH).resolve()
LIBS = REPO / "deps"          # 运行时依赖（jieba），由 deps/install_deps.py 安装
SRC = REPO / "src"
ASSETS = REPO / "assets"

block_cipher = None

# ---------------------------------------------------------------- 数据文件
datas = [
    # ★1) 内置规则集 —— 打包后由 redactor.cli._default_rules_dir() 经
    #      sys._MEIPASS 定位（该函数已内置 _MEIPASS 分支，见 cli.py:59-61）
    (str(REPO / "rules"), "rules"),
    (str(REPO / "README.md"), "."),
    (str(REPO / "LICENSE"), "."),
]

# ★2) jieba 的词典 dict.txt / 后缀表 char.txt 是**运行时按文件路径读取**的，
#    不是 import 期加载的 Python 常量。PyInstaller 只分析 import 语句，
#    **不会**自动收非 .py 数据文件。
#    缺了它，打包后 jieba 不报错，而是**静默退化**到 HMM 模式 ——
#    分词质量暴跌但程序照常跑，用户只会觉得"脱敏不准"。
#
# ★为什么手写 glob 而不用 collect_data('jieba') —— 两个坑都实测踩过：
#      ① `from PyInstaller.utils.hooks import collect_data` 在 6.22.3 上
#         **ImportError**（它在 collect_data 子模块，不在 hooks 顶层）；
#      ② `collect_all('jieba')` 在 jieba 装在 deps/（不在 sys.path 默认
#         位置）时输出 "not a package" 并**收 0 个文件** —— 静默失败，
#         打包能完成、exe 能启动，只是分词全废。
#    手写 glob 虽土，但**行为确定**；打完包由 verify_package_p9.py 断言
#    `jieba.initialize()` 真的成功。
def _jieba_datas():
    """收集 jieba 的**词性标注所需**数据文件。

    ★为什么显式排除 ``analyse/``（TF-IDF 关键词抽取）：

    实测踩过——收进去之后 onefile exe **起不来**，bootloader 报：
        [PYI-ERROR] Failed to extract jieba\analyse\idf.txt\idf.txt
        [PYI-ERROR] failed to extract files!
    打包阶段**不报错（EXIT=0）**，只有运行时才炸，且 windowed exe 没有
    stderr 看不到 —— 只能靠打一份``debug=True`` 的诊断版才能看见。
    （成因是 ``analyse/idf.txt`` 这个**文件名以 .txt 结尾**，
    被当成了"需要创建的目录"，路径撞车。）

    而本项目**只用人名识别**（jieba.posseg），实测它的依赖是：
        jieba/__init__.py        -> dict.txt
        jieba/posseg/            -> finalseg/{prob_emit,prob_start,prob_trans}.py
    **完全不 import jieba.analyse** —— 所以排除它零损失，
    还顺带减小 exe 体积、避开上述运行时崩溃。

    ★★ 最关键的写法陷阱（实测浪费了 6 轮打包才定位）：
    PyInstaller 的 ``datas`` 每项是 ``(src, dest_dir)`` —— 第二项是
    **目标目录**，文件会被放成 ``dest_dir / basename(src)``。
    我最初传了**含文件名**的完整相对路径：
        (deps/jieba/dict.txt, "jieba/dict.txt")     ✗
    PyInstaller 于是把 ``dict.txt`` 当成**目录**，目标变成：
        jieba/dict.txt/dict.txt                     ← 目录当文件名
    打包阶段 **EXIT=0 完全不报错**，但 onefile 运行时 bootloader 解压失败：
        [PYI-ERROR] Failed to extract jieba\dict.txt\dict.txt
    exe 根本起不到 Python（表现：一直挂着不退出 / 没有窗口）。
    **windowed exe 没有 stderr**，只能靠另打一份 debug 版才看得见。

    正确写法是传**父目录**：
        (deps/jieba/dict.txt, "jieba")                     ✓
        (deps/jieba/finalseg/prob_emit.py, "jieba/finalseg") ✓
    """
    root = LIBS / "jieba"
    if not root.is_dir():
        sys.stderr.write(
            "[warn] deps/jieba 不存在 —— 人名识别将不可用。"
            "先跑：python scripts/install_deps.py\n"
        )
        return []

    # analyse/（关键词抽取）整体排除，见上面的说明
    SKIP_DIRS = {"analyse", "lac_small", "__pycache__"}

    found = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        #★★ dest 必须是**父目录** —— 见上面的写法陷阱
        dest = Path("jieba") / rel.parent
        found.append((str(p), str(dest)))

    sys.stderr.write(
        f"[info] jieba 数据文件 {len(found)} 个已收进包"
        f"（dict.txt + finalseg；已排除 analyse/lac_small）\n"
    )
    for src, dest in found[:3]:
        sys.stderr.write(f"[info]   {src}  ->  {dest}/\n")
    return found


datas += _jieba_datas()

# ---------------------------------------------------------------- 隐藏导入
hiddenimports = [
    # jieba 用 importlib 动态加载，静态分析抓不到
    "jieba",
    "jieba.posseg",
    "jieba._compat",
    # ★runpy 被 PyInstaller 默认排除，但它被产物自检钩子用到
    #（gui/main.py::_run_probe_if_requested）。漏了它 → 探针静默不跑，
    #  exe 直接进 GUI 事件循环 → 验证脚本等到超时也拿不到报告。
    #  正常双击时这个钩子不激活，但 import 语句在函数体内，
    #  静态分析抓不到，所以必须显式声明。
    "runpy",
    # 三个载体 handler（部分走注册表动态查找）
    "redactor.handlers.docx_handler",
    "redactor.handlers.xlsx_handler",
    "redactor.handlers.pdf_handler",
    "redactor.handlers.txt_handler",
    "redactor.gui.dialogs.scope_dialog",
]

# 三个载体库的数据文件（python-docx 的 default.docx 模板、openpyxl 的
# schema XML、PyMuPDF 的 .dat 资源）。这些包都装在**系统** site-packages，
# PyInstaller 自带的 hook 已覆盖绝大部分；这里再兜一层非 .py 资源，
# 避免漏收后出现"打开 xlsx 报 schema 找不到"这类**运行时才暴露**的问题。
def _extra_datas():
    import importlib
    import importlib.util
    out = []
    for pkg in ("docx", "openpyxl", "fitz"):
        try:
            spec = importlib.util.find_spec(pkg)
            if spec is None or not spec.submodule_search_locations:
                continue
            base = Path(list(spec.submodule_search_locations)[0])
            for p in base.rglob("*"):
                if p.suffix.lower() in (".xml", ".docx", ".xlsx", ".dat",
                                        ".txt", ".json", ".tmpl"):
                    rel = p.relative_to(base)
                    # ★dest 是**父目录**（见 _jieba_datas 的写法陷阱）
                    out.append((str(p), str(rel.parent)))
        except Exception:
            continue
    sys.stderr.write(f"[info]载体库数据文件 {len(out)} 个\n")
    return out


datas += _extra_datas()

a = Analysis(
    [str(SRC / "redactor" / "gui" / "main.py")],
    pathex=[str(SRC), str(LIBS)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排掉用不到的大依赖：能显著减小体积，也避免 PyInstaller
    # 因为这些包里的缺失可选依赖而报警
    excludes=[
        "tkinter", "matplotlib", "numpy", "scipy", "pandas",
        "IPython", "jupyter", "notebook", "pytest", "_pytest",
        "setuptools._distutils",
        # ★jieba 的关键词抽取子包：本项目只用 posseg（人名），
        #   且analyse/idf.txt 这个"文件名以 .txt 结尾"的数据文件会让
        #   onefile bootloader 解压崩溃（见 _jieba_datas 的说明）
        "jieba.analyse", "jieba.lac_small",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LocalFileRedactor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # ★ UPX 会压缩 exe 内的 .pyd，中文 Windows 上极易被杀软误报或直接崩。
    #    本项目 exe 体积不敏感，**关掉 UPX**。
    upx=False,
    runtime_tmpdir=None,
    # ★3) 无控制台窗口 —— 双击只出 GUI，不弹黑框
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ASSETS / "app.ico"),
    version=str(ASSETS / "version_info.txt"),
)
