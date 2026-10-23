# -*- coding: utf-8 -*-
"""把 packaging/icon.svg 渲染成两张透明底母版图。

为什么要先渲染成 PNG 再让 make_icons.py 去缩:
  make_icons.py 只会画圆和矩形,画不了贝塞尔。图标本身是矢量画出来的(icon.svg),
  而 MSIX 要十几个尺寸、exe 还要 .ico,所以流程拆成两段 ——
  这里负责「矢量 → 两张大图」,make_icons.py 负责「大图 → 每个尺寸」。

两张母版:
  assets/_icon-master.png   整块圆角图(方图、开始菜单、exe 图标)
  assets/_icon-glyph.png    只有蛇、透明底(宽磁贴要自己铺底)

渲染用系统自带的 Edge 无头模式,不引入 cairosvg/Inkscape 这类额外依赖。
Edge 找不到就明确报错,不静默留一张旧图。

用法:
    python packaging/render_icon.py            # 默认 1024
    python packaging/render_icon.py --size 2048
"""
from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SVG = HERE / "icon.svg"
OUT = HERE / "assets"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 隐藏底片的写法:#tile 整组不画,只剩蛇,背景走 Edge 的透明参数
HIDE_TILE = "#tile{display:none}"


def _edge() -> str:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("找不到 Microsoft Edge,无法渲染 icon.svg。\n"
                     "请确认 Edge 已安装,或用任意工具把 icon.svg 导出成同名 PNG 放回 "
                     "packaging/assets/。")


def _html(svg_text: str, hide_tile: bool) -> str:
    # ★ 尺寸交给 CSS(100vw/100vh),不用 SVG 自己的 width/height ——
    #   否则窗口大小和图片大小对不上,截出来会带一圈白边或裁掉一角。
    svg_text = re.sub(r'(<svg[^>]*?)\swidth="[^"]*"', r"\1", svg_text, count=1)
    svg_text = re.sub(r'(<svg[^>]*?)\sheight="[^"]*"', r"\1", svg_text, count=1)
    style = HIDE_TILE if hide_tile else ""
    return ("<!doctype html><html><head><meta charset='utf-8'><style>"
            "html,body{margin:0;padding:0;background:transparent;overflow:hidden}"
            "svg{display:block;width:100vw;height:100vh}" + style +
            "</style></head><body>" + svg_text + "</body></html>")


def render(svg_text: str, hide_tile: bool, size: int, dest: Path) -> None:
    """用无头 Edge 截一张 size×size 的透明底 PNG。"""
    with tempfile.TemporaryDirectory(prefix="singmic-icon-") as td:
        page = Path(td) / "icon.html"
        page.write_text(_html(svg_text, hide_tile), encoding="utf-8")
        cmd = [
            _edge(),
            "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--default-background-color=00000000",     # 透明,不是白
            f"--window-size={size},{size}",
            f"--screenshot={dest}",
            page.as_uri(),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if not dest.exists():
            raise SystemExit("渲染失败:" + r.stderr.decode("utf-8", "replace")[-800:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=1024)
    a = ap.parse_args()

    svg_text = SVG.read_text(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    for hide, name in ((False, "_icon-master.png"), (True, "_icon-glyph.png")):
        dest = OUT / name
        render(svg_text, hide, a.size, dest)
        print("  ", dest.name, f"{a.size}x{a.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
