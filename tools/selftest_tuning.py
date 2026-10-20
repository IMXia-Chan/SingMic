"""音符块编辑器的自测:测音高 / 切块 / PSOLA / 渲染 / 重做成品。

★ 这些坑的共同点:**算出来「差不多」,但差的是八度、是整个块、是一个采样**。
  所以这里不只看「听着对」,该逐点比的地方就逐点比。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\selftest_tuning.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tuning  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SR = 48000
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def cents(a: float, b: float) -> float:
    return 1200.0 * np.log2(max(a, 1e-9) / max(b, 1e-9))


def synth_note(f0: float, dur: float, sr: int = SR, vib_cents: float = 6.0,
               vib_hz: float = 5.5) -> np.ndarray:
    """带颤音和谐波的「唱」一个音。"""
    n = int(dur * sr)
    t = np.arange(n) / sr
    dev = vib_cents * np.sin(2 * np.pi * vib_hz * t + 0.7)
    ph = 2 * np.pi * np.cumsum(f0 * 2 ** (dev / 1200.0)) / sr
    y = np.zeros(n)
    for k, a in enumerate((1.0, 0.5, 0.28, 0.14, 0.07), start=1):
        if f0 * k < sr / 2:
            y += a * np.sin(k * ph)
    env = np.ones(n)
    k = int(0.01 * sr)
    env[:k] = np.linspace(0, 1, k)
    env[-k:] = np.linspace(1, 0, k)
    return (0.25 * y / 1.99 * env).astype(np.float64)


def synth_phrase(freqs, dur_each=0.55, gap=0.12, sr=SR) -> np.ndarray:
    parts = [np.zeros(int(0.15 * sr))]
    for f in freqs:
        parts.append(synth_note(f, dur_each, sr))
        parts.append(np.zeros(int(gap * sr)))
    return np.concatenate(parts)


def measure(x, sr=SR, lo=0.0, hi=None) -> float:
    x = np.asarray(x, dtype=np.float64)
    if hi is not None:
        x = x[int(lo * sr):int(hi * sr)]
    if len(x) < 2048:
        return float("nan")
    f0, aper, level = tuning.yin(x, sr)
    ok = np.isfinite(f0) & (aper < 0.4)
    if ok.sum() < 3:
        return float("nan")
    return float(np.median(f0[ok]))


def main() -> int:
    rng = np.random.default_rng(7)

    # ---- 1) YIN 对干净 220Hz ----
    t = np.arange(int(1.0 * SR)) / SR
    sine = 0.4 * np.sin(2 * np.pi * 220.0 * t)
    f0m = measure(sine)
    check("YIN 220Hz 误差 < 1 音分", abs(cents(f0m, 220.0)) < 1.0,
          f"测到 {f0m:.2f}Hz ({cents(f0m, 220.0):+.2f} 音分)")

    # ---- 2) 切块 ----
    #   ★ 用 A 大三和弦的音(220/277/330/440/554/659),
    #     换成 A-C-E-G 就是 A 小七和弦,判调当然会判成小调 —— 测试自己先错。
    targets = [220.0, 277.18, 329.63, 440.0, 554.37, 659.26]
    sig = synth_phrase(targets, dur_each=0.55, gap=0.12)
    res = tuning.segment(sig, SR)
    note_blocks = [b for b in res["blocks"] if b["kind"] == "note"]
    check("合成唱 6 个音 → 切出 6 块", len(note_blocks) == 6,
          f"实际 {len(note_blocks)} 块 / 共 {len(res['blocks'])} 块")

    if len(note_blocks) == 6:
        errs, berrs = [], []
        for k, b in enumerate(note_blocks):
            errs.append(abs(cents(b["f0"], targets[k])))
            exp = 0.15 + k * (0.55 + 0.12) + 0.275   # 每块的中心时刻
            berrs.append(abs((b["t0"] + b["t1"]) / 2 - exp))
        check("每块音高误差 < 20 音分", max(errs) < 20.0, f"最大 {max(errs):.1f}")
        check("每块边界误差 < 60ms", max(berrs) < 0.060, f"最大 {max(berrs)*1000:.0f}ms")
        check("判调 = A 大调", res["key"] == "A" and res["mode"] == "major",
              f"{res['key']} {res['mode']} (把握 {res.get('conf', 0):.2f})")

    # ---- 3) PSOLA 变调 ----
    x = synth_note(220.0, 0.8, vib_cents=0.0)
    base = measure(x)
    for semis, tol in ((3, 5.0), (7, 8.0)):
        y = tuning.psola(x, SR, alpha=1.0, beta=2 ** (semis / 12.0))
        got = measure(y)
        want = 220.0 * 2 ** (semis / 12.0)
        check(f"PSOLA +{semis} 半音 → {semis*100}±{tol:.0f} 音分",
              abs(cents(got, want)) < tol and len(y) == len(x),
              f"实测 {cents(got, want):+.1f} 音分, 长度 {len(y)}/{len(x)}")

    # ---- 4) PSOLA 变速 ----
    y = tuning.psola(x, SR, alpha=1.5, beta=1.0)
    got = measure(y)
    check("PSOLA ×1.5 → 长度 1.5 倍,音高不变",
          len(y) == int(round(len(x) * 1.5)) and abs(cents(got, base)) < 8.0,
          f"{len(y)}/{int(round(len(x)*1.5))} 点, 音高 {cents(got, base):+.1f} 音分")

    # ---- 5) 恒等渲染逐点相同 ----
    voice = np.asarray(sig, dtype=np.float32)
    blk = tuning.segment(voice, SR)["blocks"]
    same = tuning.render_blocks(voice, SR, blk, {})
    diff = float(np.max(np.abs(same.astype(np.float64) - voice.astype(np.float64))))
    check("恒等渲染逐点相同", np.array_equal(same, voice), f"max diff {diff:.2e}")

    # ---- 6) 删除一块:该段留空,别处不变 ----
    nb = [b for b in blk if b["kind"] == "note"]
    if nb:
        victim = nb[1]
        out = tuning.render_blocks(voice, SR, blk, {str(victim["id"]): {"deleted": True}})
        a, b = int(round(victim["t0"] * SR)), int(round(victim["t1"] * SR))
        zero = not np.any(out[a:b])
        after = np.array_equal(out[b:], voice[b:])
        check("删除一块 → 该段全零、后面一个采样不动", zero and after,
              f"空 {b-a} 点, 后面相同={after}")

        # ---- 7) 拖长:吃掉紧邻的下一块 ----
        L = (victim["t1"] - victim["t0"]) + 0.1
        out2 = tuning.render_blocks(voice, SR, blk, {str(victim["id"]): {"len": L}})
        nxt = blk[blk.index(victim) + 1] if blk.index(victim) + 1 < len(blk) else None
        if nxt is not None:
            tail = int(round(nxt["t1"] * SR))
            same_tail = np.array_equal(out2[tail:], voice[tail:])
            grew = int(np.sum(np.abs(out2[a:int((victim["t0"] + L) * SR)]) > 1e-6)) > 0
            check("拖长 100ms → 后一块让位,再后面的位置不变",
                  same_tail and grew, f"尾部相同={same_tail}, 新段有声={grew}")

    # ---- 8) 重做成品:残差法逐点 ----
    backing = 0.2 * rng.standard_normal((len(voice), 2)) / 3.0
    mix_old = np.clip(np.stack([voice, voice], axis=1) + backing, -1, 1).astype(np.float32)
    voice_new = voice.copy()
    voice_new[len(voice) // 3: len(voice) // 2] *= 0.5
    mix_new = tuning.rebuild_mix(mix_old, voice, voice_new)
    want = np.clip(np.stack([voice_new, voice_new], axis=1) + backing, -1, 1)
    close = float(np.max(np.abs(mix_new.astype(np.float64) - want)))
    check("重做成品 = 新干声 + 原伴奏(逐点)", close < 1e-6, f"max diff {close:.2e}")

    # ---- 9) 编辑存档往返 ----
    edits = {str(victim["id"]): {"semis": 2, "cents": 0, "len": round(
        victim["t1"] - victim["t0"] + 0.05, 3)}}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.edits.json"
        p.write_text(json.dumps({"blocks": edits}), encoding="utf-8")
        back = json.loads(p.read_text(encoding="utf-8"))["blocks"]
        r1 = tuning.render_blocks(voice, SR, blk, edits)
        r2 = tuning.render_blocks(voice, SR, blk, back)
    check("编辑存档往返后渲染一致", np.array_equal(r1, r2))

    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n" + "=" * 52)
    print(f"  通过 {ok}/{len(RESULTS)}")
    print("=" * 52)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
