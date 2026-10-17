"""生成打包所需的 assets/（图标 + 版本资源）。

跑法：
    "E:/Program Files/python/python.exe" scripts/gen_app_assets.py

产出：
    assets/app.ico              六档尺寸图标（16/24/32/48/64/128/256）
    assets/version_info.txt     Windows 版本资源（v1.0.0）
    assets/app_icon_preview.png 预览图（256px，便于肉眼确认）

★为什么程序化生成而不是用外部素材：
   - 不引入二进制素材的来源 / 许可问题
   - 可复跑、可改颜色、改版本号
   - 无第三方依赖（纯 zlib + struct 手写 ICO 与 PNG）

**16px 可读性设计**（小尺寸下细节会糊，故按档位分别绘制）：
   16/24px  —— 实心盾牌 + 一条粗横杠（"隐去"），不加描边
   32px     —— 盾牌 + 横杠 + 1px 描边
   48px以上 —— 盾牌 + 横杠 + 描边 + 顶部高光
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets"

APP_NAME = "LocalFileRedactor"
APP_VERSION = "1.0.0"
COMPANY = "LocalFileRedactor Contributors"
DESCRIPTION = "本地文件脱敏工具 - 离线拖拽脱敏 docx/xlsx/pdf/txt"
COPYRIGHT = "Copyright (C) 2026 LocalFileRedactor Contributors"

# ---- 配色（深蓝盾牌，白色横杠）----
DEEP_BLUE = (0x1B, 0x3A, 0x6B)      # 盾牌主色
LIGHT_BLUE = (0x2E, 0x5C, 0xA8)     # 盾牌下半部
WHITE = (0xFF, 0xFF, 0xFF)
GOLD = (0xF2, 0xC1, 0x4C)          # 少量点缀（高光）


def _blank(size: int):
    """建一个 RGBA 全透明画布。"""
    return [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]


def _put(px, x, y, color):
    n = len(px)
    if 0 <= x < n and 0 <= y < n:
        px[y][x] = color


def _blend(px, x, y, color, alpha):
    """把 color 以 alpha 叠到现有像素上（做抗锯齿）。"""
    n = len(px)
    if not (0 <= x < n and 0 <= y < n):
        return
    dr, dg, db, da = px[y][x]
    sr, sg, sb = color
    if da == 0:
        px[y][x] = (sr, sg, sb, int(alpha * 255))
        return
    a = alpha + da / 255 * (1 - alpha)
    if a <= 0:
        px[y][x] = (0, 0, 0, 0)
        return
    r = (sr * alpha + dr * (da / 255) * (1 - alpha)) / a
    g = (sg * alpha + dg * (da / 255) * (1 - alpha)) / a
    b = (sb * alpha + db * (da / 255) * (1 - alpha)) / a
    px[y][x] = (int(r), int(g), int(b), int(a * 255))


def _shield_inside(u: float, v: float) -> bool:
    """盾牌形状的内部判定（u,v ∈ [0,1]，v 向下）。

    几何（全部用平直线段 + 一段弧，16px 下也不会糊）：
      上 46%：矩形（半宽 0.34，居中）
      下 54%：梯形收窄到半宽 0.17，底边圆角

    ★半宽从 0.34 起步而不是 0.38：留出描边空间，
    否则描边会顶到画布边缘被截断。
    """
    if v < 0.0 or v > 1.0:
        return False
    center = 0.5

    if v < 0.46:
        # 上部矩形（微收一点点，顶部也圆，避免生硬）
        half = 0.34
        if v < 0.06:                       # 顶部圆角
            r = 0.10
            cu, cv = center, 0.06 + r
            if ((u - cu) ** 2 + (v - cv) ** 2) ** 0.5 > r:
                return False
            return abs(u - center) <= half
        return abs(u - center) <= half

    if v > 0.94:                           # 底部收尾
        return False

    # 下部梯形
    t = (v - 0.46) / 0.48
    half = 0.34 * (1 - t) + 0.17 * t
    if abs(u - center) > half:
        return False

    # 底边圆角：只在**最底下 8%** 收，判定圆心在底边中线上。
    # ★不能用"两角分别挖圆"—— 那样会在底边中间也挖出凹口
    #   （实测图标底部出现缺口就是这么来的）。
    if v > 0.92:
        r = 0.10
        cy = 0.94 - r
        if ((u - center) ** 2 + (v - cy) ** 2) ** 0.5 > r:
            return False
    return True


def _bar_inside(u: float, v: float) -> bool:
    """中央"隐去横杠"的内部判定（u,v ∈ [0,1]）。"""
    return 0.26 <= v <= 0.40 and 0.28 <= u <= 0.72


def render(size: int, *, detail: str = "auto"):
    """渲染一档尺寸。

    Args:
        size: 边长（像素）。
        detail: ``full`` / ``simple`` / ``auto``。``auto`` 会按尺寸
            自动选：<=24 用 simple（无描边、高光），否则 full。
    """
    if detail == "auto":
        detail = "simple" if size <= 24 else "full"

    px = _blank(size)
    ss = 4# 4x 超采样做抗锯齿
    stroke = max(1, size // 32)

    for y in range(size):
        for x in range(size):
            cov_shield = 0.0
            cov_bar = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    u = (x + (sx + 0.5) / ss) / size
                    v = (y + (sy + 0.5) / ss) / size
                    if not _shield_inside(u, v):
                        continue
                    cov_shield += 1.0
                    if _bar_inside(u, v):
                        cov_bar += 1.0
            total = ss * ss
            if cov_shield <= 0:
                continue
            a_shield = cov_shield / total
            # 盾牌填充：上深下浅，暗示"由表及里"
            v_mid = (y + 0.5) / size
            base = DEEP_BLUE if v_mid < 0.55 else LIGHT_BLUE
            _blend(px, x, y, base, a_shield)

            if cov_bar > 0:
                a_bar = cov_bar / total
                _blend(px, x, y, WHITE, a_bar)

    if detail == "full" and stroke >= 1:
        # 描边：贴着盾牌**外轮廓**外侧 1~2 像素的一圈深色，
        # 让盾牌在浅色背景上也有边界（小尺寸尤其需要）。
        band = max(1, size // 24)
        for y in range(size):
            for x in range(size):
                u = (x + 0.5) / size
                v = (y + 0.5) / size
                if _shield_inside(u, v):
                    continue
                near = any(
                    _shield_inside((x + dx + 0.5) / size,
                                   (y + dy + 0.5) / size)
                    for dy in range(-band, band + 1)
                    for dx in range(-band, band + 1)
                    if dx or dy
                )
                if near:
                    _blend(px, x, y, (0x08, 0x16, 0x2E), 0.92)

        # 顶部高光：只在**盾牌内部**画一条浅色横带
        if size >= 48:
            hy = int(size * 0.18)
            thick = max(1, size // 64)
            for x in range(size):
                u = (x + 0.5) / size
                for d in range(thick):
                    v = (hy + d + 0.5) / size
                    if _shield_inside(u, v):
                        _blend(px, x, hy + d, GOLD, 0.28)

    return px


# ================================================================ PNG 编码
def write_png(path: Path, px) -> None:
    """把 RGBA 像素写成 PNG（纯 zlib + struct，无第三方依赖）。"""
    size = len(px)
    raw = bytearray()
    for row in px:
        raw.append(0)                      # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


# ================================================================ ICO 编码
def write_ico(path: Path, images) -> None:
    """把多档PNG 合成多分辨率 ICO。

    Args:
        path: 输出路径。
        images: ``[(size, png_bytes), ...]``，按任意顺序即可。
    """
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    blobs = b""
    for size, blob in images:
        w = 0 if size >= 256 else size          # 256 在 ICO 里记作 0
        entries += struct.pack("<BBBBHHII",
                               w, w, 0, 0, 1, 32, len(blob), offset)
        blobs += blob
        offset += len(blob)
    path.write_bytes(header + entries + blobs)


# ================================================================ 版本资源
def _build_version_info() -> str:
    """生成 PyInstaller 的 version 文件内容。

    ★关键：PyInstaller 用 ``eval(text)`` 解析该文件
       （见 PyInstaller/utils/win32/versioninfo.py:572），
       所以内容必须是**合法 Python 字面量**，且必须真的构造成
       ``VSVersionInfo`` 实例 —— 不能手写 ``VSVersionInfo(...)`` 那种
       "看起来像"的源码，那样 eval 时名字未定义，
       报 "Failed to deserialize VSVersionInfo from text-based
       representation!"（本轮打包实测踩过）。

    正确做法：直接构造对象再 ``repr()``，输出的一定能被 eval 回来。
    """
    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )
    except ImportError:
        return "# PyInstaller 未安装，无法生成 version_info.txt\n"

    v = tuple(int(x) for x in APP_VERSION.split("."))
    #补齐到 4 段（FixedFileInfo 要求 (major, minor, build, revision)）
    while len(v) < 4:
        v = v + (0,)

    info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=v,
            prodvers=v,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,          # VOS_NT_WINDOWS32
            fileType=0x1,        # VFT_APP
            subtype=0x0,         # VFT2_UNKNOWN
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable(
                    "080404b0",  # 简体中文 (0x0804) + Unicode (1200=0x04B0)
                    [
                        StringStruct("CompanyName", COMPANY),
                        StringStruct("FileDescription", DESCRIPTION),
                        StringStruct("FileVersion", APP_VERSION),
                        StringStruct("InternalName", APP_NAME),
                        StringStruct("LegalCopyright", COPYRIGHT),
                        StringStruct("OriginalFilename", APP_NAME + ".exe"),
                        StringStruct("ProductName", APP_NAME),
                        StringStruct("ProductVersion", APP_VERSION),
                    ],
                )
            ]),
            VarFileInfo([VarStruct("Translation", [2052, 1200])]),
        ],
    )
    # repr() 里带 `versioninfo.` 模块前缀（如
    # ``versioninfo.VSVersionInfo(...)``），而 PyInstaller 是裸 ``eval(text)``，
    # 那个名字**并不存在** → 报 "Failed to deserialize"。必须剥掉前缀。
    text = repr(info)
    text = text.replace("versioninfo.", "")
    return text + "\n"


VERSION_INFO = ""   # 由 _build_version_info() 运行时生成


def main() -> int:
    ASSETS.mkdir(exist_ok=True)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for size in sizes:
        px = render(size)
        png_path = ASSETS / f"_tmp_{size}.png"
        write_png(png_path, px)
        images.append((size, png_path.read_bytes()))
        print(f"  渲染 {size:>3}px")
        png_path.unlink()

    ico_path = ASSETS / "app.ico"
    write_ico(ico_path, images)
    print(f"✅ {ico_path}  ({ico_path.stat().st_size} bytes, {len(sizes)} 档)")

    prev = ASSETS / "app_icon_preview.png"
    write_png(prev, render(256))
    print(f"✅ {prev}  ({prev.stat().st_size} bytes)")

    vi = ASSETS / "version_info.txt"
    content = _build_version_info()
    vi.write_text(content, encoding="utf-8")

    # 自检：PyInstaller 用 eval 解析它，必须能原样解析回来
    try:
        from PyInstaller.utils.win32.versioninfo import (
            load_version_info_from_text_file,
        )
        load_version_info_from_text_file(str(vi))
        print(f"✅ {vi}  (v{APP_VERSION}, eval 自检通过)")
    except ImportError:
        print(f"⚠️  {vi}  (v{APP_VERSION}；PyInstaller 未装，跳过自检)")
    except Exception as exc:
        print(f"❌ {vi}  eval 自检失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())