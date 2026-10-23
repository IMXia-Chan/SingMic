# -*- coding: utf-8 -*-
"""把 dist\\SingMic 打成可以上传到 Microsoft Store 的 .msix。

为什么要有这一步:
  商店要的不是 exe,是一个**打包身份**(Package Identity)完整的 MSIX:
  清单里写着谁是发行商、叫什么名字、版本号几位数,商店审核按这份清单验收。
  直接传 exe 商店不收(那叫未打包 Win32 应用,只能走「Win32 应用」流程且体验差)。

这个脚本做四件事:
  1. 读 version.py / identity.<版本>.json —— 清单里的名字、版本、发行商只能有一个来源;
  2. 把 dist\\SingMic + packaging\\assets 铺进 packaging\\layout(每次先清空,不留旧文件),
     并写下 edition.txt —— 程序靠它知道自己该放哪些功能;
  3. 写 AppxManifest.xml;
  4. 调 makeappx.exe 打包。本机没装 Windows SDK 也不用手动装:
     脚本会自动从 NuGet 取 Microsoft.Windows.SDK.BuildTools,解到 packaging\\.sdk\\。

用法:
    python packaging/make_msix.py                # 发布包(全功能)
    python packaging/make_msix.py --layout-only  # 只铺目录+写清单,不打包(用 Add-AppxPackage -Register 试装)
    python packaging/make_msix.py --identity packaging\\identity.dev.json   # 显式指定身份文件

★ 商店里只有**一个产品**「声麦 Studio Pro」,全部功能都开。包里写的 edition.txt 是 `pro`,
  只是把「全功能」这个事实落到盘上;edition.py 认不出来时也默认全功能。

★ 发行商名(Publisher)必须和 Partner Center 里「产品标识」给出的**一模一样**,
  包括 CN= 和大写;对不上商店会在上传时直接拒包。第一次先跑一遍,
  脚本会把模板写出来,照着商店后台填。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LAYOUT = HERE / "layout"
OUT = HERE / "out"
SDK = HERE / ".sdk"
ASSETS = HERE / "assets"
DIST = ROOT / "dist" / "SingMic"

SDK_PKG = ("Microsoft.Windows.SDK.BuildTools", "10.0.22621.756")
SDK_URL = ("https://www.nuget.org/api/v2/package/{0}/{1}".format(*SDK_PKG))

DEFAULT_IDENTITY = {
    "identityName": "REPLACE_WITH_STORE_ID.SingMicStudio",
    "publisher": "CN=REPLACE-WITH-PARTNER-CENTER-PUBLISHER",
    "publisherDisplayName": "IMXia-Chan",
    "displayName": "SingMic Studio",
    "description": "录音、修音、编曲、混音与导出，全部在本机完成的一体化音频工作站。",
}

# 身份文件。包名/发行商必须与 Partner Center 里「产品标识」一字不差。
IDENTITY = HERE / "identity.json"

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package
    xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
    xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
    xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
    IgnorableNamespaces="uap rescap">

  <Identity Name="{identity}" Publisher="{publisher}" Version="{version}" ProcessorArchitecture="x64" />

  <Properties>
    <DisplayName>{display}</DisplayName>
    <PublisherDisplayName>{publisher_display}</PublisherDisplayName>
    <Description>{description}</Description>
    <Logo>Assets\\StoreLogo.png</Logo>
  </Properties>

  <Dependencies>
    <!-- Windows 10 1809 起都支持;WebView2 运行时由商店依赖或系统自带(见 packaging/README.md) -->
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.22621.0" />
  </Dependencies>

  <Resources>
    <Resource Language="zh-CN" />
    <Resource Language="zh-TW" />
    <Resource Language="en-US" />
    <Resource Language="ja-JP" />
    <Resource Language="ko-KR" />
  </Resources>

  <Applications>
    <Application Id="SingMic" Executable="SingMic\\SingMic.exe" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements
          DisplayName="{display}"
          Description="{description}"
          BackgroundColor="#0B0D12"
          Square150x150Logo="Assets\\Square150x150Logo.png"
          Square44x44Logo="Assets\\Square44x44Logo.png">
        <uap:DefaultTile Wide310x150Logo="Assets\\Wide310x150Logo.png" />
        <uap:SplashScreen Image="Assets\\SplashScreen.png" BackgroundColor="#0B0D12" />
      </uap:VisualElements>
    </Application>
  </Applications>

  <Capabilities>
    <!-- 完整信任:这个包就是一个普通桌面程序(自己起本地服务、读写自己的数据目录) -->
    <rescap:Capability Name="runFullTrust" />
  </Capabilities>
</Package>
"""


def version_quad() -> str:
    """读 version.py 的 VERSION,补齐成商店要求的四段(1.0.0 → 1.0.0.0)。"""
    txt = (ROOT / "version.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', txt, re.M)
    if not m:
        raise SystemExit("version.py 里找不到 VERSION")
    parts = [p for p in re.split(r"[.\-+]", m.group(1)) if p.isdigit()][:4]
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts)


def load_identity(path: Path) -> dict:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_IDENTITY, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        raise SystemExit(
            f"没有 {path.name}，已经写出模板：{path}\n"
            "  去 Partner Center →「产品标识」抄两样填进去：\n"
            "    identityName  = 包名（形如 1234567890ABCDE.SingMicStudio）\n"
            "    publisher     = 发行商（形如 CN=8F3C…，连 CN= 一起抄）\n"
            "  填完再跑一次。")
    d = json.loads(path.read_text(encoding="utf-8"))
    for k in ("identityName", "publisher"):
        if "REPLACE" in str(d.get(k, "REPLACE")):
            raise SystemExit(
                f"{path.name} 里的 {k} 还是占位符。商店会拒包：发行商必须和后台一字不差。")
    for k, v in DEFAULT_IDENTITY.items():
        d.setdefault(k, v)
    return d


def ensure_makeappx() -> Path:
    """取出 makeappx 整个 x64 目录，不只是那个 exe。

    ★ 只抠 makeappx.exe 出来会直接起不来：WinError 14001「并行配置不正确」——
      它依赖同目录的 appxpackaging.dll / opcservices.dll / ServicingCommon.dll 等。
    """
    exe = SDK / "makeappx.exe"
    if exe.exists():
        return exe
    SDK.mkdir(parents=True, exist_ok=True)
    pkg = SDK / "sdkbuildtools.nupkg"
    print(f"取 makeappx（{SDK_PKG[0]} {SDK_PKG[1]}，约 22MB）…")
    urllib.request.urlretrieve(SDK_URL, pkg)
    n = 0
    with zipfile.ZipFile(pkg) as z:
        # 包内目录名是 SDK 自己的版本(10.0.22621.0),不是 NuGet 的版本号 —— 只能现找
        hit = [x for x in z.namelist() if x.lower().endswith("/x64/makeappx.exe")]
        if not hit:
            raise SystemExit("这个 nupkg 里找不到 x64/makeappx.exe，换一个版本号再试")
        prefix = hit[0][: -len("makeappx.exe")]
        for name in z.namelist():
            ln = name.lower()
            if not ln.startswith(prefix) or name.endswith("/"):
                continue
            rel = name[len(prefix):]
            dst = SDK / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    pkg.unlink(missing_ok=True)
    if not exe.exists():
        raise SystemExit("这个 nupkg 里没有 x64/makeappx.exe，换一个版本号再试")
    print(f"   解开 {n} 个文件到 {SDK}")
    return exe


def build_layout(manifest: str) -> None:
    if not DIST.exists():
        raise SystemExit(f"没有 {DIST} —— 先跑 python tools/build_app.py 打出 exe")
    if not ASSETS.exists():
        raise SystemExit(f"没有 {ASSETS} —— 先跑 python packaging/make_icons.py")
    if LAYOUT.exists():
        shutil.rmtree(LAYOUT)
    LAYOUT.mkdir(parents=True)
    shutil.copytree(DIST, LAYOUT / "SingMic")
    # ★ 发布版全功能。写 `pro` 是把这件事落成文件,方便日后核对包里到底是什么。
    (LAYOUT / "SingMic" / "edition.txt").write_text("pro\n", encoding="utf-8")
    shutil.copytree(ASSETS, LAYOUT / "Assets")
    (LAYOUT / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    # 商店审核会看这个:「许可证条款」页面上写的协议与仓库一致
    for f in ("LICENSE", "NOTICE"):
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, LAYOUT / f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", default=None,
                    help="显式指定身份文件（默认 packaging\\identity.json）")
    ap.add_argument("--layout-only", action="store_true")
    a = ap.parse_args()

    ident_path = Path(a.identity) if a.identity else IDENTITY
    ident = load_identity(ident_path)
    ver = version_quad()
    manifest = MANIFEST.format(
        identity=ident["identityName"], publisher=ident["publisher"], version=ver,
        display=ident["displayName"], publisher_display=ident["publisherDisplayName"],
        description=ident["description"])
    build_layout(manifest)
    print(f"清单写好：{LAYOUT / 'AppxManifest.xml'}  版本 {ver}  全功能")
    if a.layout_only:
        print("只铺目录。试装：Add-AppxPackage -Register "
              f"\"{LAYOUT / 'AppxManifest.xml'}\"（需开发者模式）")
        return 0

    exe = ensure_makeappx()
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"{ident['identityName'].split('.')[-1]}_{ver}_x64.msix"
    out = OUT / name
    out.unlink(missing_ok=True)
    cmd = [str(exe), "pack", "/d", str(LAYOUT), "/p", str(out), "/o", "/v"]
    print(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc
    print(f"\n完成：{out}  ({out.stat().st_size / 1048576:.1f} MB)")
    print("上传：Partner Center → 你的应用 → 提交 → 程序包。不需要自己签名，商店会重签。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
