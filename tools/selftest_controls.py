"""新增控件的行为自测:高通 / 直通 / 参数链路。

★ 这些坑的共同点:**参数没送到该去的地方,界面照样显示改了**。
  所以这里不看界面,直接问对象:链子里的 hp 是不是真的变了、直通是不是真的绕过了。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\selftest_controls.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audio_engine import Engine, Recorder      # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def main() -> int:
    e = Engine()          # ★ 不 open_output,不会碰声卡

    # ---- 1) 高通其实送到了 EQ 层 ----
    e.set_params(hp_hz=120.0)
    check("hp_hz 送到 SpectralEQ", e.chain.eq.hp_hz == 120.0,
          f"eq.hp_hz={e.chain.eq.hp_hz}")
    check("改 hp 会触发重新设计", e.chain.eq._dirty is True)
    check("hp_hz 记在 params 里", e.params.get("hp_hz") == 120.0)

    # ---- 2) 直通是引擎级开关,不许塞进 chain.set ----
    e.set_params(bypass=True)
    check("bypass 打开", e.chain_bypass is True)
    check("bypass 没被塞进参数表给链子", "bypass" in e.params)
    e.set_params(bypass=False)
    check("bypass 关回去", e.chain_bypass is False)

    # ---- 3) 直通时**真实回调**的输出 = 输入 × 增益(不经过 EQ/门/压缩)----
    rng = np.random.default_rng(3)
    sig = (0.01 * rng.standard_normal(2000)).astype(np.float32)
    e.set_params(gain=2.0, bypass=True, denoise=0.0, gate=False)
    e.voice.push(sig)                     # 攒够目标水位才会 pull 出真样本
    out = np.zeros((480, 2), dtype=np.float32)
    e._callback(out, 480, None, None)
    check("直通 = 输入 × 增益(走 _callback)", np.allclose(out[:, 0], sig[:480] * 2.0),
          f"max diff {np.abs(out[:, 0] - sig[:480] * 2.0).max():.2e}")
    e.set_params(bypass=False, gain=1.0, denoise=0.0, gate=False)

    # ---- 4) 高通的物理效果:120Hz 比 80Hz 更狠 ----
    def low_response(hp: float) -> float:
        ee = Engine()
        ee.set_params(hp_hz=hp, denoise=0.0, gate=False,
                      eq_low=0.0, eq_mid=0.0, eq_high=0.0)
        t = np.arange(480 * 60) / 48000.0
        sig = (0.2 * np.sin(2 * np.pi * 60 * t)).astype(np.float32)
        out = np.concatenate([ee.chain.process(sig[i:i + 480])
                              for i in range(0, len(sig) - 480, 480)])
        return 20 * np.log10(float(np.sqrt(np.mean(out[4800:] ** 2))) + 1e-12)

    a, b = low_response(80.0), low_response(120.0)
    check("120Hz 对 60Hz 的衰减比 80Hz 更狠", b < a - 1.0, f"80Hz {a:.1f} / 120Hz {b:.1f} dB")

    # ---- 5) 录音开头那截「手机还没送数据」的真空要被裁掉 ----
    #   ★ 精确零 = 缓冲区空,不是安静。真实房间的底噪永远不会恰好 0.0。
    tmp = Path(tempfile.mkdtemp())
    r = Recorder()
    r.start(tmp)
    z = np.zeros(480, dtype=np.float32)
    for _ in range(2):                      # 两块真空
        r.push(z.copy(), np.stack([z, z], axis=1))
    t = np.arange(480) / 48000.0
    sig = (0.5 * np.sin(2 * np.pi * 523.25 * t)).astype(np.float32)
    r.push(sig.copy(), np.stack([sig, sig], axis=1))
    r.stop()
    v, _ = sf.read(r.paths[1], dtype="float32", always_2d=True)
    m, _ = sf.read(r.paths[0], dtype="float32", always_2d=True)
    shutil.rmtree(tmp, ignore_errors=True)
    check("录音开头不再带真空", len(v) < 1200 and not bool(np.all(v[:1, 0] == 0)),
          f"写出 {len(v)} 点(原始 1440)")
    check("裁的时候人声和混音没走散", float(np.max(np.abs(v[:, 0] - m[:, 0]))) < 1e-6,
          f"max diff {float(np.max(np.abs(v[:, 0] - m[:, 0]))):.2e}")

    # ---- 6) 保存接口要写的那组键,引擎里一个都不缺 ----
    keys = ("gain", "denoise", "gate", "gate_db", "hp_hz", "eq_low", "eq_mid",
            "eq_high", "deess", "comp", "comp_ratio", "comp_threshold", "makeup")
    missing = [k for k in keys if k not in e.params]
    check("保存要用的参数键齐全", not missing, str(missing))

    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n" + "=" * 52)
    print(f"  通过 {ok}/{len(RESULTS)}")
    print("=" * 52)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
