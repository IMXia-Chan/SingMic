"""把训练结果打包成 `model.zip`(给 Studio 用),并写 manifest.json。

用法(在 RVC 仓库根目录):
    python /path/to/export_model.py --exp 我的声音 --out ~/model.zip

找文件:
  * 模型:`assets/weights/<exp>.pth`,没有就找 `logs/<exp>/G_*.pth` 里最新的;
  * 索引:`logs/<exp>/added_*.index` 里最新的(没有索引也能跑,只是相似度检索弱一点)。

manifest.json 里的 version = 模型文件 sha256 前 16 位。**换模型就会换 version**,
Studio 的补音缓存 key 带着它,所以不会拿旧音频冒充新模型的结果。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


def newest(paths) -> Path | None:
    ps = [p for p in paths if p.is_file()]
    return max(ps, key=lambda p: p.stat().st_mtime) if ps else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True, help="RVC 实验名")
    ap.add_argument("--out", default="model.zip")
    ap.add_argument("--model", help="直接指定 .pth(不给就按 exp 找)")
    ap.add_argument("--index", help="直接指定 .index")
    ap.add_argument("--materials-sec", type=float, default=0.0,
                    help="训练素材总秒数(可从 dataset/meta.json 的 total_sec 抄)")
    ap.add_argument("--epochs", type=int, default=0)
    a = ap.parse_args()

    root = Path.cwd()
    model = Path(a.model) if a.model else None
    if model is None:
        cand = root / "assets" / "weights" / (a.exp + ".pth")
        if cand.is_file():
            model = cand
        else:
            model = newest((root / "logs" / a.exp).glob("G_*.pth"))
    if model is None or not model.is_file():
        print("找不到模型文件 —— 训练完了吗?看 assets/weights 和 logs/%s" % a.exp,
              file=sys.stderr)
        return 2
    index = Path(a.index) if a.index else newest(
        (root / "logs" / a.exp).glob("added_*.index"))
    if index is not None and not index.is_file():
        index = None

    digest = sha256(model)
    manifest = {
        "version": "rvc-" + digest[:16],
        "model": model.name,
        "index": index.name if index else "",
        "sha256": digest,
        "bytes": model.stat().st_size,
        "trained_at": datetime.fromtimestamp(
            model.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "materials_sec": int(round(a.materials_sec)),
        "epochs": int(a.epochs),
        "exp": a.exp,
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(model, "model.pth")
        if index:
            z.write(index, "model.index")
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    print("打包完成:", out)
    print("  version     :", manifest["version"])
    print("  model       :", model, "(%.1f MB)" % (model.stat().st_size / 1e6))
    print("  index       :", index or "(无)")
    print("  materials   :", manifest["materials_sec"], "秒")
    print("\n下一步(三份备份,缺一不可):")
    print("  1) 传到云盘/对象存储(OSS/COS)")
    print("  2) 下载回本机 D:\\singmic\\voice_model\\")
    print("  3) 给数据盘打快照")
    return 0


if __name__ == "__main__":
    sys.exit(main())
