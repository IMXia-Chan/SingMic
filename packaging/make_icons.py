# -*- coding: utf-8 -*-
"""生成应用图标与 MSIX 需要的整套资源图。

为什么要脚本:MSIX 要求同一个图标给出十几个尺寸(Square44 还有一整套 targetsize),
手工作图每次改版都要重来一遍。这里只认两张母版,其余全部由它们缩出来,
保证每个尺寸的像素都是同一张图(不会出现某个尺寸是旧版)。

图从哪来:
  icon.svg 是唯一的设计源(盘身昂首的蓝色蝰蛇)。先用 render_icon.py 把它渲染成
  assets/_icon-master.png(整块圆角图)和 assets/_icon-glyph.png(只有蛇、透明底),
  再来跑这个脚本。**不要手改 assets 里任何一张 PNG** —— 下次生成会覆盖。

用法:
    python packaging/render_icon.py                # 先出母版(改过 icon.svg 才需要)
    python packaging/make_icons.py                 # 写进 packaging/assets/
    python packaging/make_icons.py --preview       # 额外出一张总览图,方便肉眼检查
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE / "assets"
MASTER = OUT / "_icon-master.png"      # 整块圆角图
GLYPH = OUT / "_icon-glyph.png"        # 只有蛇,透明底

BG_TOP = (21, 80, 127)                 # 和 icon.svg 的底同一个渐变,宽磁贴才对得上
BG_BOT = (6, 19, 33)

_master_src: Image.Image | None = None
_glyph_src: Image.Image | None = None


def _load(path: Path, what: str) -> Image.Image:
    if not path.exists():
        raise SystemExit(
            f"缺少母版 {path.name}。\n"
            "先跑一次: python packaging/render_icon.py\n"
            f"({what}由 icon.svg 渲染而来,不再由本脚本直接画。)")
    return Image.open(path)


def _master_image() -> Image.Image:
    global _master_src
    if _master_src is None:
        _master_src = _load(MASTER, "整块图标").convert("RGBA")
    return _master_src


def _glyph_image() -> Image.Image:
    global _glyph_src
    if _glyph_src is None:
        _glyph_src = _load(GLYPH, "蛇形").convert("RGBA")
    return _glyph_src


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))   # type: ignore[return-value]


def _round_tile(w: int, h: int, radius_frac: float = 0.20) -> Image.Image:
    """圆角深蓝底。渐变按高度走,宽磁贴和方图一个调子。"""
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    grad = Image.new("RGBA", (w, h))
    gd = ImageDraw.Draw(grad)
    for y in range(h):
        gd.line([(0, y), (w, y)], fill=_lerp(BG_TOP, BG_BOT, y / max(1, h - 1)) + (255,))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w - 1, h - 1], radius=int(min(w, h) * radius_frac), fill=255)
    im.paste(grad, (0, 0), mask)
    return im


def master(size: int = 1024) -> Image.Image:
    """整块图缩到 size×size。每个尺寸都从同一张母版缩,不叠缩。"""
    return _master_image().resize((max(1, size), max(1, size)), Image.LANCZOS)


def glyph(s: int) -> Image.Image:
    return _glyph_image().resize((max(1, s), max(1, s)), Image.LANCZOS)


def wide_master(w: int = 310, h: int = 150) -> Image.Image:
    """宽磁贴:蛇放左边,右边留白给系统叠标题字。"""
    im = _round_tile(w, h, 0.16)
    gh = round(h * 0.78)
    g = glyph(gh)
    im.alpha_composite(g, (round(h * 0.24), (h - gh) // 2))
    return im


def write(im: Image.Image, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    im.save(OUT / name)
    print("  ", name, im.size)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="额外写一张总览图")
    a = ap.parse_args()

    base = master(1024)
    # 缩放比例:MSIX 认这五档
    SCALES = (100, 125, 150, 200, 400)
    sets = {
        "Square150x150Logo": 150,
        "Square44x44Logo": 44,
        "StoreLogo": 50,
    }
    for name, px in sets.items():
        for sc in SCALES:
            n = max(1, round(px * sc / 100))
            suffix = "" if sc == 100 else f".scale-{sc}"
            # ★ 每个尺寸单独从母版缩,不是拿上一档再缩 —— 连着缩会把边缘糊掉
            write(master(n), f"{name}{suffix}.png")

    # Square44 还要一整套 targetsize —— 任务栏/开始菜单按「图片的像素高」挑,缺了会退回 100% 那张再硬缩
    for t in (16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 256):
        ic = master(t)
        write(ic, f"Square44x44Logo.targetsize-{t}.png")
        write(ic, f"Square44x44Logo.targetsize-{t}_altform-unplated.png")

    wide = wide_master()
    for sc in SCALES:
        n_w, n_h = round(310 * sc / 100), round(150 * sc / 100)
        suffix = "" if sc == 100 else f".scale-{sc}"
        write(wide.resize((n_w, n_h), Image.LANCZOS), f"Wide310x150Logo{suffix}.png")

    splash = master(620)
    for sc in SCALES:
        n = round(620 * sc / 100)
        suffix = "" if sc == 100 else f".scale-{sc}"
        write(splash.resize((n, n), Image.LANCZOS), f"SplashScreen{suffix}.png")

    # exe / 安装包的图标
    base.save(HERE / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                       (128, 128), (256, 256)])
    print("   app.ico")

    if a.preview:
        sheet = Image.new("RGBA", (1044, 1674), (40, 44, 52, 255))
        sheet.alpha_composite(base, (10, 10))
        sheet.alpha_composite(wide.resize((1024, 496), Image.LANCZOS), (10, 1044))
        # 下面这条小图标排:真实像素大小放一遍,看 16px 下还认不认得出来
        x = 16
        for t in (16, 24, 32, 48, 64, 96, 128):
            sheet.alpha_composite(master(t), (x, 1560))
            x += t + 16
        write(sheet, "_preview.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
