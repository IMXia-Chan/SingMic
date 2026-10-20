"""Studio 的纯 DSP 自测 —— 不碰真录音,全部在临时目录合成。

覆盖:
  1. clip_key 稳定 / 换一个字段就变;
  2. render_piece 恒等逐点相同、+3 半音 = 300 音分、outSec 采样数精确;
  3. estimate_bpm 对 120/128/90 的合成鼓点误差 ≤1 BPM、第一拍偏移 <20ms;
  4. check_pitch 把「偏高 40 音分」标红并给出 -40 的建议、把「偏高 150 音分」标黄且不给建议。

跑法:
    PYTHONIOENCODING=utf-8 D:\\AI\\python.exe D:\\singmic\\tools\\selftest_studio.py
"""
from __future__ import annotations

import atexit
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import studio_mix as sm        # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:              # noqa: BLE001
    pass

OK = 0
BAD = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global OK, BAD
    OK += 1 if ok else 0
    BAD += 0 if ok else 1
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def cents_of(f: float, want: float) -> float:
    return 1200.0 * math.log2(max(f, 1e-6) / want)


def peak_hz(y: np.ndarray, sr: int) -> float:
    seg = y[: min(len(y), 16384)]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    return float(np.fft.rfftfreq(len(seg), 1 / sr)[int(np.argmax(spec))])


def click_track(bpm: float, sec: float, sr: int = 48000, offset: float = 0.0) -> np.ndarray:
    n = int(sr * sec)
    y = np.zeros(n, dtype=np.float32)
    beat = 60.0 / bpm
    t = offset
    rng = np.random.default_rng(7)
    while t < sec - 0.02:
        i = int(t * sr)
        m = min(n - i, int(sr * 0.03))
        env = np.exp(-np.arange(m) / (sr * 0.006))
        burst = (rng.standard_normal(m) * 0.5 + np.sin(
            2 * np.pi * 1800 * np.arange(m) / sr) * 0.5) * env
        y[i:i + m] += burst.astype(np.float32) * 0.5
        t += beat
    return y


def vowel(f0: float, sec: float, sr: int = 48000, cents: float = 0.0,
          vib: float = 0.0) -> np.ndarray:
    """合成一个「啊」:谐波叠加,带轻微起落包络。cents 用来故意唱偏。"""
    f = f0 * 2 ** (cents / 1200.0)
    n = int(sr * sec)
    t = np.arange(n) / sr
    phase = 2 * np.pi * f * t
    if vib:
        phase = phase + (vib / (5.0)) * np.sin(2 * np.pi * 5.0 * t)
    y = np.zeros(n)
    for k in range(1, 9):
        y += (1.0 / k) * np.sin(k * phase)
    env = np.minimum(1.0, t / 0.03) * np.minimum(1.0, (sec - t) / 0.05)
    return (0.22 * y * np.clip(env, 0, 1)).astype(np.float32)


def sentence(notes: list[tuple[float, float, float]], gap: float = 0.12,
             sr: int = 48000) -> np.ndarray:
    """notes = [(f0, cents, 时长)]"""
    parts = [np.zeros(int(sr * 0.15), dtype=np.float32)]
    for f0, c, d in notes:
        parts.append(vowel(f0, d, sr, cents=c))
        parts.append(np.zeros(int(sr * gap), dtype=np.float32))
    return np.concatenate(parts)


def vst_section(tmp: Path, sr: int) -> None:
    """VST3 宿主 / 冻结 / stems 的离线段。

    ★ 没有插件或没有 pedalboard 时**整段跳过**,不误报。
    ★ 这里的音频全是合成的,不碰真录音。
    """
    import studio_vst as sv

    print("\n-- VST3(离线宿主 / 冻结 / stems)--")
    probe = sv.probe()
    if not probe.get("ok"):
        print(f"  (跳过:VST 宿主不可用 —— {probe.get('error')})")
        return
    plugins = [p for p in sv.scan_plugins() if p.get("ok") and p.get("path")]
    if not plugins:
        print("  (跳过:允许的目录里没有可用的 .vst3)")
        return
    p0 = plugins[0]
    dll = p0["path"]

    # 1) bundle → 内部 x86_64-win DLL
    bundle = Path(p0.get("bundle") or "")
    if bundle.is_dir():
        got = sv.resolve_bundle(bundle)
        check("bundle 解析到内部 DLL",
              got is not None and got.is_file() and "contents" in str(got).lower(),
              str(got))
    else:
        check("bundle 解析到内部 DLL", Path(dll).is_file(), dll)

    # 2) 路径白名单
    bad_paths = [str(tmp / "x.vst3"), str(ROOT / "vst" / ".." / "server.py"),
                 str(ROOT / "vst" / "not_a_plugin.txt")]
    rejected = 0
    for bp in bad_paths:
        try:
            sv.allowed_plugin_path(bp)
        except sv.VstError:
            rejected += 1
    check("白名单拒绝目录外/穿越/非 vst3", rejected == len(bad_paths),
          f"{rejected}/{len(bad_paths)}")

    # 合成源
    sm.MEDIA.mkdir(parents=True, exist_ok=True)
    src = sm.MEDIA / "_vst_selftest.wav"
    t = np.arange(sr) / sr
    x = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    sf.write(str(src), x, sr, subtype="FLOAT")
    rel = str(src.relative_to(ROOT)).replace("\\", "/")

    slot = {"id": "v1", "name": p0["name"], "path": dll,
            "bundle": p0.get("bundle") or "", "bypass": False, "params": {}}

    def mkproj(slot_in=None, with_clip: bool = True) -> dict:
        tr = {"id": "tr_1", "name": "主唱", "kind": "voice", "gain": 1.0,
              "pan": 0.0, "mute": False, "solo": False, "clips": []}
        if with_clip:
            tr["clips"] = [{"id": "c1", "sourceId": "s1", "start": 0.0,
                            "srcIn": 0.0, "srcOut": 1.0, "length": 1.0,
                            "semis": 0, "cents": 0, "gain": 1.0,
                            "fadeIn": 0.0, "fadeOut": 0.0, "kind": "note"}]
        if slot_in:
            tr["fx"] = {"vst": [dict(slot_in)]}
        return {"version": 1, "bpm": 120, "lengthSec": 1.0,
                "sources": {"s1": {"path": rel, "size": src.stat().st_size,
                                   "mtime": int(src.stat().st_mtime)}},
                "tracks": [tr]}

    def raises(fn, exc) -> bool:
        try:
            fn()
        except exc:
            return True
        except Exception:                 # noqa: BLE001
            return False
        return False

    with sv.VstHost() as host:
        # 3) worker 往返
        y = host.render(x, sr, [slot], "自测")
        check("worker 往返:长度一致、非空、无 NaN",
              len(y) == len(x) and float(np.max(np.abs(y))) > 1e-4
              and bool(np.isfinite(y).all()),
              f"n={len(y)} peak={float(np.max(np.abs(y))):.3f}")

        # 4) bypass 与干声逐点一致
        yb = host.render(x, sr, [dict(slot, bypass=True)], "自测")
        e_by = float(np.max(np.abs(yb - x)))
        check("bypass 与干声逐点一致(<1e-6)", e_by < 1e-6, f"{e_by:.1e}")

        # 5) 参数真的生效
        info = host.inspect(dll)
        nump = [p for p in (info.get("params") or [])
                if p.get("type") == "number"
                and isinstance(p.get("min"), (int, float))
                and isinstance(p.get("max"), (int, float))
                and p["max"] > p["min"]]
        if nump:
            prm = nump[0]
            base = host.render(x, sr, [slot], "自测")
            lo = host.render(x, sr, [dict(slot, params={prm["key"]: prm["min"]})], "自测")
            hi = host.render(x, sr, [dict(slot, params={prm["key"]: prm["max"]})], "自测")
            d_max = max(float(np.max(np.abs(lo - base))),
                        float(np.max(np.abs(hi - base))))
            check("改参数输出真的变(>1e-3)", d_max > 1e-3,
                  f"{prm['key']} diff {d_max:.1e}")
        else:
            print("  (这个插件没有带范围的数值参数,跳过参数项)")

        # 6) 确定性探针 ── 先说老实话:Airwindows Consolidated 自带抖动噪声,
        #    实测连跑两遍就差 1e-1 量级 ⇒ 它本来就不是确定性的。
        #    这里先量它,再用 nondet_of 把「谁来判、怎么判」单测住。
        a = host.render(x, sr, [slot], "自测")
        b = host.render(x, sr, [slot], "自测")
        e_det = float(np.max(np.abs(a - b))) if len(a) == len(b) else 1.0
        print(f"  · 实测量:这个插件两次渲染差 {e_det:.2e}"
              f"(> {sv.NONDET_EPS:g} 就会在冻结里标 nondet)")
        check("nondet 探针:同一份数组判为确定", sv.nondet_of(a, a) is False)
        check("nondet 探针:差 1e-3 判为非确定",
              sv.nondet_of(a, a + np.float32(1e-3)) is True)
        check("nondet 探针:长度不同判为非确定",
              sv.nondet_of(a, a[:-1]) is True)

        # 7) 没插件的旧工程:给不给 host 都逐点一样
        p_novst = mkproj(None)
        m0, _ = sm.mix_project(p_novst, sr)
        m1, _ = sm.mix_project(p_novst, sr, vst=host)
        check("没有 fx.vst 的工程不受宿主影响(逐点 0 差)",
              float(np.max(np.abs(m0 - m1))) == 0.0)

        # 8) 有 VST 却没宿主 ⇒ 必须抛错,不许静默出干声
        p_vst = mkproj(slot)
        tr_v = p_vst["tracks"][0]
        msg = ""
        try:
            sm.mix_project(p_vst, sr, vst=None)
        except sv.VstUnavailable as e:
            msg = str(e)
        check("有 VST 却没宿主 ⇒ VstUnavailable,带轨名",
              "主唱" in msg and "宿主" in msg, msg[:60])

        # 9) 冻结 == 主混音(单轨 / pan=0 / 无旧 FX)
        #    ★ 用 bypass 的槽位量「管线」:插件确定时两侧必然逐点一致。
        #      不旁通时这个插件自己两次渲染就不同(见第 6 条),
        #      只能比量级 —— 这正是 nondet 标记存在的理由。
        p_by = mkproj(dict(slot, bypass=True))
        post_b = sm.render_track_post(p_by, p_by["tracks"][0], sr, 0.0, None, vst=host)
        mix_b, _ = sm.mix_project(p_by, sr, vst=host)
        g = math.cos(math.pi / 4.0)
        e_fz = float(np.max(np.abs(mix_b[:, 0] - post_b * g)))
        check("冻结轨 == 主混音同一轨(<1e-4,旁通槽位)", e_fz < 1e-4, f"{e_fz:.1e}")

        post_w = sm.render_track_post(p_vst, tr_v, sr, 0.0, None, vst=host)
        mix_w, _ = sm.mix_project(p_vst, sr, vst=host)
        r_post = float(np.sqrt(np.mean(post_w ** 2)))
        r_mix = float(np.sqrt(np.mean((mix_w[:, 0] / g) ** 2)))
        ok_mag = r_mix > 1e-6 and 0.2 < (r_post / r_mix) < 5.0
        print(f"  · 不旁通时:冻结 RMS {r_post:.4f} / 主混音 RMS {r_mix:.4f}"
              f"(这个插件不确定,只比量级)")
        check("不旁通的冻结与主混音在同一量级", ok_mag,
              f"{r_post:.4f} vs {r_mix:.4f}")

        # 10) 分轨:post 过 VST,pre 不过
        st_pre, _ = sm.render_stems(p_vst, sr, "pre")
        st_post, _ = sm.render_stems(p_vst, sr, "post", vst=host)
        ok_st = (len(st_pre) == 1 and len(st_post) == 1 and
                 len(st_pre[0]["audio"]) == len(x) and
                 len(st_post[0]["audio"]) == len(x))
        d_st = (float(np.max(np.abs(st_post[0]["audio"] - st_pre[0]["audio"])))
                if ok_st else 0.0)
        check("stems:post 过 VST、pre 不过", ok_st and d_st > 1e-3,
              f"len {len(st_pre)}/{len(st_post)} diff {d_st:.1e}")

        # 11) 签名:同内容稳定;改参数/改采样率都变
        s1 = sv.track_sig(p_vst, tr_v, sr)
        s2 = sv.track_sig(p_vst, tr_v, sr)
        p_chg = json.loads(json.dumps(p_vst))
        if nump:
            p_chg["tracks"][0]["fx"]["vst"][0]["params"][nump[0]["key"]] = 0.123
        s3 = sv.track_sig(p_chg, p_chg["tracks"][0], sr)
        s4 = sv.track_sig(p_vst, tr_v, 44100)
        check("冻结签名:同内容稳定、改参数/采样率会变",
              s1 == s2 and s1 != s3 and s1 != s4,
              f"{s1[:8]} {s3[:8]} {s4[:8]}")

        # 12) 空轨 + 坏插件路径 ⇒ 也必须报错(不能因为没音频就「成功」)
        p_bad = mkproj(dict(slot, path=str(ROOT / "vst" / "_nope_.vst3")),
                       with_clip=False)
        ok_bad = raises(
            lambda: sm.render_track_post(p_bad, p_bad["tracks"][0], sr,
                                         0.0, None, vst=host), sv.VstError)
        check("空轨上的坏插件也报错(不静默成功)", ok_bad)

        # 13) 采样率:按 44.1k 渲染就是 44.1k 的长度
        post44 = sm.render_track_post(p_vst, tr_v, 44100, 0.0, None, vst=host)
        check("44.1k 冻结长度正确、无 NaN",
              len(post44) == 44100 and bool(np.isfinite(post44).all()),
              f"n={len(post44)}")


def syllable_section(tmp: Path, sr: int) -> None:
    """按字/音节切分(studio_syllable)的合成测试 —— 只看「核」的个数与时间轴。"""
    import studio_syllable as sylmod

    # 1) 6 个「啊」,同音高附近、每个带 100ms 缝:应该正好 6 个音节核
    parts = [np.zeros(int(sr * 0.2), dtype=np.float32)]
    for i in range(6):
        parts.append(vowel(220.0 + 20 * i, 0.18, sr))
        parts.append(np.zeros(int(sr * 0.10), dtype=np.float32))
    y = np.concatenate(parts)
    r = sylmod.segment_syllables(y, sr)
    notes = [b for b in r["blocks"] if b["kind"] == "note"]
    check("6 个音节 → 6 个 note 块", len(notes) == 6,
          f"{len(notes)} 个 note / 共 {len(r['blocks'])} 块")

    # 2) 时间轴平铺:首块从 0 起、末块到 dur、块与块首尾相接
    bs = r["blocks"]
    flat = abs(bs[0]["t0"]) < 1e-6 and abs(bs[-1]["t1"] - r["dur"]) < 0.01
    gap = max((abs(bs[i]["t1"] - bs[i + 1]["t0"]) for i in range(len(bs) - 1)),
              default=0.0)
    check("块铺满整条、首尾相接(一个采样都不漏)", flat and gap < 1e-6,
          f"start {bs[0]['t0']} end {bs[-1]['t1']}/{r['dur']} gap {gap:.1e}")

    # 3) 一个 1.6 秒的长音带颤音:是**一个**字,不许按音高抖动切成好几块
    long = np.concatenate([np.zeros(int(sr * 0.2), dtype=np.float32),
                           vowel(330.0, 1.6, sr, vib=0.35)])
    r2 = sylmod.segment_syllables(long, sr)
    n_long = [b for b in r2["blocks"] if b["kind"] == "note"]
    check("1.6s 长音 + 颤音 → 1 个 note 块(不按音高切)",
          len(n_long) == 1, str([b["kind"] for b in r2["blocks"]]))

    # 4) 块上要有 syl 序号与音域 f0lo/f0hi
    nb = notes[0] if notes else {}
    check("块上有 syl 与 f0lo/f0hi",
          nb.get("syl") == 1 and float(nb.get("f0lo", 9e9)) <= float(nb.get("f0hi", -9e9)),
          f"f0={nb.get('f0')} [{nb.get('f0lo')},{nb.get('f0hi')}]")

    # 5) 回归:旧 `tuning.segment()` 的返回字段一个不少(旧工程解析不受影响)
    import tuning
    old = tuning.segment(y, sr)
    need = {"sr", "dur", "key", "mode", "conf", "voiced_s", "blocks"}
    check("旧 segment() 顶层字段没变", need <= set(old), str(set(old)))
    check("旧 segment() 块字段没变",
          all({"id", "t0", "t1", "kind"} <= set(b) for b in old["blocks"]))


def asr_section(tmp: Path, sr: int) -> None:
    """识别结果对齐(studio_asr):无时间戳时的均分 + 中文按块估算对齐。"""
    import studio_asr as sa

    toks, has = sa._clean_tokens(["你", "好", "啊"], [], (0.0, 3.0))
    check("模型不给时间戳:token 在段内均分",
          has is False and len(toks) == 3
          and [round(t["t0"], 2) for t in toks] == [0.0, 1.0, 2.0],
          str([(t["t0"], t["t1"]) for t in toks]))
    check("均分后的 token 首尾相接、不重叠",
          all(abs(toks[i]["t1"] - toks[i + 1]["t0"]) < 1e-6 for i in range(len(toks) - 1)),
          str([(t["t0"], t["t1"]) for t in toks]))

    toks2, has2 = sa._clean_tokens(["a", "b"], [0.5, 0.9], (0.0, 2.0))
    check("有真时间戳就照用模型给的", has2 is True and toks2[0]["t0"] == 0.5
          and toks2[1]["t0"] == 0.9, str(toks2))

    blocks = [
        {"t0": 0.0, "t1": 0.2, "kind": "silence"},
        {"t0": 0.2, "t1": 1.0, "kind": "note"},
        {"t0": 1.0, "t1": 2.0, "kind": "note"},
    ]
    asr = {"lang": "zh", "approx": True,
           "tokens": [{"text": "你", "t0": 0.0, "t1": 0.1},
                      {"text": "好", "t0": 0.0, "t1": 0.1}]}
    ann = sa.annotate(blocks, asr)
    txt = [b.get("text") for b in ann[1:]]
    check("中文估算对齐:字按顺序落到音块上",
          txt == ["你", "好"], str(txt))
    check("估算对齐的块上带 textApprox(界面要标「仅供参考」)",
          all(b.get("textApprox") is True for b in ann[1:]), str(ann[1:]))

    asr_words = {"lang": "en", "approx": False,
                 "words": [{"text": "hello", "t0": 0.25, "t1": 0.9},
                           {"text": "world", "t0": 1.1, "t1": 1.9}]}
    ann2 = sa.annotate(blocks, asr_words)
    check("有真实时间时英文按重叠贴词",
          [b.get("text") for b in ann2[1:]] == ["hello", "world"],
          str([b.get("text") for b in ann2]))

    # ★ 摊时间前先裁掉两头静音:否则末几个词会被摊到静音上、贴不到块
    pad = np.zeros(int(0.2 * sr), dtype=np.float32)
    tone = (0.3 * np.sin(2 * np.pi * 220.0
                         * np.arange(int(0.5 * sr)) / sr)).astype(np.float32)
    span = sa._active_span(np.concatenate([pad, tone, pad]), sr)
    check("摊时间前裁掉两头静音",
          abs(span[0] - 0.2) < 0.03 and abs(span[1] - 0.7) < 0.05, str(span))
    toks3, _ = sa._clean_tokens(["a", "b"], [], span)
    check("token 只摊在有人声的那段里",
          toks3[0]["t0"] >= 0.19 and toks3[-1]["t1"] <= 0.71, str(toks3))


def voice_section(tmp: Path, sr: int) -> None:
    """本地补音 + 云端降级(studio_voice)。"""
    import studio_voice as sv

    x = vowel(220.0, 1.0, sr)

    # 1) 延长 3×
    y, note = sv.local_extend(x, sr, 3 * sr)
    check("本地补音 延长 3× 采样数精确", len(y) == 3 * sr,
          f"{len(y)} vs {3 * sr}")
    check("补音结果没有 NaN/Inf", bool(np.isfinite(y).all()))
    pk = float(np.max(np.abs(y))) if len(y) else 0.0
    check("补音不削顶(峰值 ≤0.99)", pk <= 0.99, f"peak {pk:.3f}")
    check("首尾 5ms 淡入淡出",
          abs(float(y[0])) < 1e-4 and abs(float(y[-1])) < 1e-4,
          f"{y[0]:.2e} / {y[-1]:.2e}")
    from tuning import yin
    f0, _ap, _lv = yin(y[int(0.3 * sr):int(2.7 * sr)], sr)
    med = float(np.nanmedian(f0))
    c = 1200 * math.log2(med / 220.0) if med == med and med > 0 else 9999.0
    check("延长后音高不变(±15¢)", abs(c) <= 15, f"{med:.1f}Hz ({c:+.1f}¢)")
    check("3× 延长不算妥协(note 为空)", not note, note)

    # 2) 缩短 0.5×
    y2, note2 = sv.local_extend(x, sr, sr // 2)
    check("本地补音 缩短 0.5× 采样数精确", len(y2) == sr // 2,
          f"{len(y2)} vs {sr // 2}")
    f0b, _ap, _lv = yin(y2[int(0.05 * sr):int(0.45 * sr)], sr)
    medb = float(np.nanmedian(f0b))
    cb = 1200 * math.log2(medb / 220.0) if medb == medb and medb > 0 else 9999.0
    check("缩短后音高不变(±15¢)", abs(cb) <= 15, f"{medb:.1f}Hz ({cb:+.1f}¢)")
    check("0.5× 缩短不算妥协(note 为空)", not note2, note2)

    # 3) 缩到 0.1× → 撞 30% 下限,必须明说,长度仍要精确
    y3, note3 = sv.local_extend(x, sr, sr // 10)
    check("缩过头夹到 30% 并说明",
          len(y3) == sr // 10 and "30%" in note3, f"{len(y3)} 说:{note3}")

    # 4) extend():落盘、相对路径没有子目录(媒体接口取不到子目录)、缓存
    src = tmp / "rec.wav"
    sf.write(str(src), x, sr, subtype="PCM_16")
    media = tmp / "projects" / "media"
    r = sv.extend(src, "projects/rec.wav", 0.1, 0.9, 2.0, media, force_local=True)
    check("extend 本地:engine=local、文件真存在",
          bool(r.get("ok")) and r.get("engine") == "local"
          and (media / Path(str(r.get("path", ""))).name).is_file(), str(r)[:200])
    check("生成文件在 projects/media 下、不带子目录",
          str(r.get("path", "")).startswith("projects/media/ext_")
          and "/" not in str(r.get("path", ""))[len("projects/media/"):],
          str(r.get("path")))
    r2 = sv.extend(src, "projects/rec.wav", 0.1, 0.9, 2.0, media, force_local=True)
    check("同一请求第二次命中缓存",
          r2.get("cached") is True and r2.get("path") == r.get("path"),
          str(r2)[:160])

    # 5) 云端:可达但 /extend 失败 → 必须回落本地,且 engine 不能冒充 cloud
    _probe, _cloud = sv.probe_health, sv._cloud_extend
    try:
        sv.probe_health = lambda force=False: {
            "reachable": True, "modelVersion": "rvc-test", "detail": ""}
        sv._cloud_extend = lambda x, sr, t: {"ok": False, "reason": "mock 超时"}
        r3 = sv.extend(src, "projects/rec.wav", 0.1, 0.9, 1.2, media)
        check("云端失败 → 回落本地并在 note 里说明",
              bool(r3.get("ok")) and r3.get("engine") == "local"
              and "云端不可用" in str(r3.get("note") or ""), str(r3)[:220])
        r4 = sv.extend(src, "projects/rec.wav", 0.1, 0.9, 1.2, media)
        check("云端失败后再次请求仍标 local(不命中云端缓存)",
              r4.get("engine") == "local", str(r4)[:160])

        # 6) 云端成功:标 cloud,时长精确
        def _ok_cloud(x, sr, t):
            n = int(round(t * sr))
            y = np.tile(np.asarray(x, dtype=np.float64), 3)[:n]
            return {"ok": True, "audio": y, "modelVersion": "rvc-test"}
        sv._cloud_extend = _ok_cloud
        r5 = sv.extend(src, "projects/rec.wav", 0.1, 0.9, 1.5, media)
        p5 = media / Path(str(r5.get("path", ""))).name
        n5 = sf.info(str(p5)).frames if p5.is_file() else -1
        check("云端成功:engine=cloud、时长精确",
              r5.get("engine") == "cloud" and abs(n5 - int(1.5 * sr)) <= 2,
              f"engine={r5.get('engine')} frames={n5}")
    finally:
        sv.probe_health, sv._cloud_extend = _probe, _cloud


def nl_section(tmp: Path, sr: int) -> None:
    """自然语言调参:上下文清洗 + 服务端校验(studio_nl)。"""
    import studio_nl as nl

    proj = {"name": "秘密工程.singmic", "tracks": [
        {"id": "tr_1", "name": "我的真名.wav", "kind": "voice", "gain": 1.0, "pan": 0.0,
         "fx": {"reverb": {"mix": 0.1, "size": 1.5}},
         "clips": [
             {"id": "cl_1", "start": 0.0, "length": 2.0, "semis": 0, "cents": 5,
              "gain": 1.0, "kind": "note", "pitchCurve": [[0, 0], [1, 1]]},
             {"id": "cl_2", "start": 2.0, "length": 1.0, "semis": 1, "cents": 0,
              "gain": 0.8, "kind": "note"},
         ]},
        {"id": "tr_2", "name": "伴奏_D盘路径.wav", "kind": "music", "gain": 0.7,
         "pan": 0.0, "clips": []},
    ]}

    ctx = nl.build_context(proj, {"type": "project"}, send_names=False)
    blob = json.dumps(ctx, ensure_ascii=False)
    check("上下文不含真实轨道名、工程名",
          "我的真名" not in blob and "秘密工程" not in blob and "伴奏_D盘" not in blob)
    check("上下文不含路径、盘符、.wav",
          "\\" not in blob and "D:" not in blob and ".wav" not in blob)
    check("上下文里是匿名角色名(主唱/伴奏)", "主唱" in blob and "伴奏" in blob)
    named = nl.build_context(proj, {"type": "project"}, send_names=True)
    check("勾了「发送轨道名」才带真名",
          "我的真名" in json.dumps(named, ensure_ascii=False))

    ct = nl.build_context(proj, {"type": "track", "trackId": "tr_2"})
    check("scope=轨 只带这一条轨",
          [t["id"] for t in ct["tracks"]] == ["tr_2"] and not ct["clips"],
          str([t["id"] for t in ct["tracks"]]))
    cc = nl.build_context(proj, {"type": "clips", "clipIds": ["cl_2"]})
    check("scope=块 只带选中的块与它所属的轨",
          [c["id"] for c in cc["clips"]] == ["cl_2"]
          and [t["id"] for t in cc["tracks"]] == ["tr_1"],
          str([c["id"] for c in cc["clips"]]))

    raw = {"reply": "好", "ops": [
        {"kind": "gain", "trackId": "tr_1", "to": 5.0},
        {"kind": "pan", "trackId": "tr_2", "to": -9},
        {"kind": "gain", "trackId": "tr_999", "to": 0.5},
        {"kind": "bogus", "trackId": "tr_1", "to": 1},
        {"kind": "clipgain", "clipId": "cl_999", "to": 1},
        {"kind": "pitch", "clipId": "cl_1", "to": {"semis": 99, "cents": 0}},
        {"kind": "reverb", "trackId": "tr_1", "to": {"mix": 2, "size": 0.01}},
        {"kind": "chain", "trackId": "tr_1", "slots": [
            {"type": "compressor", "params": {"threshold": -30, "ratio": 3}}]},
        {"kind": "chain", "trackId": "tr_1", "slots": [{"type": "no_such_fx"}]},
    ]}
    acts, dropped, notes = nl.validate(raw, ctx, proj)
    by = {}
    for a in acts:
        by.setdefault(a["kind"], []).append(a)
    check("gain 5.0 被夹到 1.5", by["gain"][0]["to"] == 1.5, str(by["gain"][0]))
    check("pan -9 被夹到 -1", by["pan"][0]["to"] == -1.0, str(by["pan"][0]))
    check("pitch 99 半音被夹到 24", by["pitch"][0]["to"]["semis"] == 24,
          str(by["pitch"][0]["to"]))
    check("reverb mix/size 被夹进范围",
          by["reverb"][0]["to"] == {"mix": 1.0, "size": 0.2},
          str(by["reverb"][0]["to"]))
    check("chain 复用 sanitize_slot,有效槽位保留并标 auto",
          len(by.get("chain", [])) == 1 and by["chain"][0]["slots"][0]["auto"] is True,
          str(by.get("chain"))[:200])
    check("未知 id / 未知 kind / 空槽位链被丢弃并计数",
          dropped == 4, f"dropped={dropped}")

    check("解析带 ``` 围栏的 JSON", nl._parse('```json\n{"reply":"x","ops":[]}\n```')
          == {"reply": "x", "ops": []})
    check("解析夹在废话里的 JSON",
          nl._parse('好的:{"reply":"x","ops":[]} 完毕') == {"reply": "x", "ops": []})
    check("非 JSON 返回 None", nl._parse("我不知道") is None)

    # plan():校验后的动作返回给界面,服务端绝不改工程
    _ask = nl._ask
    try:
        nl._ask = lambda text, c: ({"reply": "加一点混响", "ops": [
            {"kind": "reverb", "trackId": "tr_1", "to": {"mix": 0.25}}]}, "", 0.0)
        before = json.dumps(proj, sort_keys=True, ensure_ascii=False)
        pr = nl.plan("人声太干,加一点混响", proj, {"type": "project"})
        check("plan 返回校验后的动作",
              bool(pr.get("ok")) and len(pr.get("actions") or []) == 1,
              str(pr)[:200])
        check("plan 不改工程本身",
              json.dumps(proj, sort_keys=True, ensure_ascii=False) == before)
    finally:
        nl._ask = _ask


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="st-self-"))
    # ★ 跑完(含中途报错)把临时目录带走 —— 以前每跑一次留一份,攒出好几个 G
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    sr = 48000

    # ---- 1 clip_key ----
    k1 = sm.clip_key("recordings/a_voice.wav", 100, 200, 0, 48000, 300, 48000)
    k2 = sm.clip_key("recordings/a_voice.wav", 100, 200, 0, 48000, 300, 48000)
    k3 = sm.clip_key("recordings/a_voice.wav", 100, 200, 0, 48000, 301, 48000)
    check("clip_key 稳定且不含 block id", k1 == k2 and len(k1) == 20, k1)
    check("clip_key 对音分敏感", k1 != k3)

    # ---- 2 render_piece ----
    x = vowel(220.0, 1.0, sr)
    same = sm.render_piece(x, sr, 0, len(x), 0.0, len(x))
    check("恒等渲染逐点相同", float(np.max(np.abs(same - x))) == 0.0,
          f"max diff {float(np.max(np.abs(same - x))):.2e}")
    up = sm.render_piece(x, sr, 0, len(x), 300.0, None)
    f_up = peak_hz(up, sr)
    c = cents_of(f_up, 220.0)
    check("+3 半音 ≈ +300 音分", abs(c - 300) <= 12, f"{f_up:.1f}Hz ({c:+.1f}¢)")
    want_n = int(round(1.5 * sr))
    st = sm.render_piece(x, sr, 0, len(x), 0.0, want_n)
    check("outSec 采样数精确", len(st) == want_n, f"{len(st)} vs {want_n}")

    # 和声用到的全域 ±7:★ 必须用 YIN 量,不能用 FFT 峰值 —— 低八度那边峰值会挑到二次谐波
    import tuning
    worst = 0.0
    for s in range(-7, 8):
        y = sm.render_piece(x, sr, 0, len(x), s * 100.0, None)
        f0, _ap, _lv = tuning.yin(y[int(0.2 * sr): int(0.9 * sr)], sr)
        f = float(np.nanmedian(f0))
        e = abs(1200.0 * math.log2(f / (220.0 * 2 ** (s / 12)))) if f == f else 9999.0
        worst = max(worst, e)
    check("±7 半音内音高误差 <10 音分", worst < 10.0, f"最差 {worst:.2f}¢")

    # ---- 3 BPM ----
    for bpm, off in ((120.0, 0.0), (128.0, 0.37), (90.0, 0.11)):
        p = tmp / f"click_{int(bpm)}.wav"
        sf.write(str(p), click_track(bpm, 20.0, sr, off), sr, subtype="PCM_16")
        r = sm.estimate_bpm(p)
        ok = r.get("bpm", 0) > 0 and abs(r["bpm"] - bpm) <= 1.0
        # 偏移取模到一个拍内再比
        d = abs(((r.get("offset", 0) - off) % (60.0 / bpm)))
        d = min(d, 60.0 / bpm - d)
        check(f"{int(bpm)} BPM 测速误差 ≤1", ok, f"{r.get('bpm')} (偏移差 {d*1000:.0f}ms)")
        check(f"{int(bpm)} BPM 第一拍偏移 <20ms", d < 0.020, f"{d*1000:.0f}ms")

    # ---- 4 check_pitch ----
    # C4=261.63;前 4 个正常,第 5 个偏高 40 音分,第 6 个偏高 150 音分
    seq = [(261.63, 0, 0.45), (293.66, 0, 0.45), (329.63, 0, 0.45),
           (261.63, 0, 0.45), (329.63, 40, 0.45), (293.66, 150, 0.45)]
    p = tmp / "sentence.wav"
    sf.write(str(p), sentence(seq), sr, subtype="PCM_16")
    r = sm.check_pitch(p)
    notes = [b for b in r["blocks"] if b["f0"]]
    red = [b for b in notes if b["status"] == "red"]
    yellow = [b for b in notes if b["status"] == "yellow"]
    check("检测到 ≥5 个音", len(notes) >= 5, f"{len(notes)} 个 · 调 {r['root']}{r['mode']}")
    check("偏高 40 音分被标红", len(red) >= 1,
          str([(b["note"], b["cents"], b["suggest"]) for b in red[:3]]))
    if red:
        s = red[0]["suggest"]
        shift = s["semis"] * 100 + s["cents"]
        check("红点建议 ≈ -40 音分", abs(shift + 40) <= 15, f"{shift:+.0f}¢")
        check("红点建议能落回调内", abs(red[0]["cents"] + shift) <= 12,
              f"{red[0]['cents']:+.1f} → {red[0]['cents']+shift:+.1f}")
    check("偏太多(≥100 音分)标黄且不给建议", all(b["suggest"] is None for b in yellow),
          f"黄 {len(yellow)} 个")

    # ---- 5 mix_project ----
    src44 = tmp / "src44.wav"
    sr44 = 44100
    tt = np.arange(int(sr44 * 1.0)) / sr44
    y44 = (0.5 * np.sin(2 * np.pi * 440 * tt)).astype(np.float32)
    sf.write(str(src44), y44, sr44, subtype="PCM_16")
    proj = {
        "tracks": [{
            "id": "tr1", "gain": 1.0, "pan": 0.0, "mute": False, "solo": False,
            "clips": [{"id": "c1", "sourceId": "s1", "start": 0.5, "srcIn": 0.0,
                       "srcOut": 1.0, "length": 1.0, "gain": 1.0,
                       "fadeIn": 0.1, "fadeOut": 0.1, "semis": 0, "cents": 0}],
        }],
        "sources": {"s1": {"path": str(src44)}},
    }
    out, rep = sm.mix_project(proj)
    want_n = int(round(1.5 * 48000))
    check("导出时长误差 <10ms", abs(rep["sec"] - 1.5) < 0.010 and len(out) == want_n,
          f"{rep['sec']}s / {len(out)} 采样")
    # 逐点核对淡入公式:前 0.1s 线性 0→1,后 0.1s 1→0
    seg = out[int(0.5 * 48000): int(1.5 * 48000), 0]
    from scipy.signal import resample_poly
    src48 = resample_poly(y44, 160, 147).astype(np.float32)     # 48k/44.1k
    # ★ 单声道居中时要乘等功率系数 cos(π/4),否则差 0.707 倍
    ref = (src48[: len(seg)] * math.cos(math.pi / 4)).astype(np.float32)
    k = int(round(0.1 * 48000))
    ref[:k] *= np.linspace(0, 1, k).astype(np.float32)
    ref[-k:] *= np.linspace(1, 0, k).astype(np.float32)
    err = float(np.max(np.abs(seg - ref)))
    check("淡入淡出逐点符合公式(1e-4)", err < 1e-4, f"max diff {err:.2e}")
    check("44.1k 源重采样进 48k 工程", len(out) == want_n and abs(float(out.mean())) < 0.02,
          f"直流偏移 {float(out.mean()):.4f}")

    proj["tracks"][0]["mute"] = True
    out_m, _ = sm.mix_project(proj)
    check("mute 的轨真的静音", float(np.max(np.abs(out_m))) == 0.0)
    proj["tracks"][0]["mute"] = False

    proj["tracks"].append({"id": "tr2", "gain": 1.0, "pan": 0.0, "mute": False,
                           "solo": False, "clips": []})
    proj["tracks"][0]["solo"] = True
    out_s, rep_s = sm.mix_project(proj)
    check("solo 时其他轨不进混音", rep_s["clips"] == 1, f"clips={rep_s['clips']}")
    proj["tracks"][0]["solo"] = False

    # 归一化
    proj["tracks"][0]["clips"][0]["gain"] = 4.0
    out_n2, rep_n = sm.mix_project(proj, normalize=True)
    check("归一化后峰值 ≈ -1 dBFS", abs(rep_n["peak_db"] - (-1.0)) < 0.2,
          f"{rep_n['peak_db']} dBFS · peak {rep_n['peak']}")
    check("归一化后不削顶", float(np.max(np.abs(out_n2))) <= 1.0)

    # ---- 6 和弦检测 ----
    def chord(notes: list[int], sec: float, sr: int = 48000) -> np.ndarray:
        n = int(sr * sec)
        t = np.arange(n) / sr
        y = np.zeros(n)
        for m in notes:
            f = 440.0 * 2 ** ((m - 69) / 12)
            for k in (1, 2, 3):
                y += (0.12 / k) * np.sin(2 * np.pi * f * k * t + k)
        env = np.minimum(1, t / 0.05) * np.minimum(1, (sec - t) / 0.08)
        return (y * np.clip(env, 0, 1)).astype(np.float32)

    prog = [("C", [60, 64, 67]), ("G", [55, 59, 62]),
            ("Am", [57, 60, 64]), ("F", [53, 57, 60])]
    p = tmp / "cgaf.wav"
    sf.write(str(p), np.concatenate([chord(n, 1.6, sr) for _, n in prog]), sr, subtype="PCM_16")
    r = sm.detect_chords(p)
    got = [c["name"] for c in r["chords"]]
    want = [n for n, _ in prog]
    hit = sum(1 for i, g in enumerate(got) if i < len(want) and g == want[i])
    check("C–G–Am–F 测出 ≥3/4", hit >= 3, f"{got}")
    # 时间边界:每段起点误差 <0.25s
    if len(got) >= 4:
        off = [abs(r["chords"][i]["t0"] - 1.6 * i) for i in range(4)]
        check("和弦边界误差 <0.25s", max(off) < 0.25, f"{max(off):.2f}s")

    # ---- 7 块内音高曲线 ----
    import tuning
    far = tmp / "far.wav"
    sf.write(str(far), x, sr, subtype="FLOAT")
    base = sm.clip_key("projects/media/far.wav", 1, 2, 0, len(x), 0, len(x))
    sig_a = sm.curve_sig([(0.0, 50.0), (1.0, 50.0)])
    sig_b = sm.curve_sig([(0.0, 50.0), (1.0, 60.0)])
    k_a = sm.clip_key("projects/media/far.wav", 1, 2, 0, len(x), 0, len(x), sig_a)
    k_b = sm.clip_key("projects/media/far.wav", 1, 2, 0, len(x), 0, len(x), sig_b)
    check("曲线不改 → key 不变", k_a == sm.clip_key("projects/media/far.wav", 1, 2, 0, len(x), 0, len(x), sig_a))
    check("曲线改一点 → key 就变", k_a != k_b)
    check("空曲线 → key 与旧版逐字节一致", base == sm.clip_key(
        "projects/media/far.wav", 1, 2, 0, len(x), 0, len(x), ""))

    c50 = sm.render_curve_piece(x, sr, 0, len(x), [(0.0, 50.0), (1.0, 50.0)], None)
    f0_50, _a, _l = tuning.yin(c50[int(0.2 * sr):int(0.9 * sr)], sr)
    meas = 1200.0 * math.log2(float(np.nanmedian(f0_50)) / 220.0)
    check("+50 音分的常数曲线 → 实测 50±5", abs(meas - 50) <= 5, f"{meas:+.1f}¢")

    for c in (200.0, -200.0):
        yy = sm.render_curve_piece(x, sr, 0, len(x), [(0.0, c), (1.0, c)], None)
        ok = (not np.isnan(yy).any()) and len(yy) == len(x) and float(np.max(np.abs(yy))) > 0
        check(f"{c:+.0f} 音分曲线不崩、不出 NaN", ok,
              f"len {len(yy)} · peak {float(np.max(np.abs(yy))):.3f}")

    # ★ 边界回归:最后一个基音标记 round() 正好顶到 n ⇒ 老代码 f0s[marks] 越界
    trig = None
    for _n in range(4000, 12000, 7):
        for _f in (137.0, 149.3, 161.7, 173.1, 181.9):
            _c, _hit = 0.0, False
            while _c < _n:
                if int(round(_c)) >= _n:
                    _hit = True
                    break
                _c += max(sr / _f, 2.0)
            if _hit:
                trig = (_n, _f)
                break
        if trig:
            break
    if trig:
        _n, _f = trig
        _tt = np.arange(_n) / sr
        _xs = (0.3 * np.sin(2 * np.pi * 220.0 * _tt)).astype(np.float64)
        try:
            _ys = tuning.psola_curve(_xs, sr, 1.0,
                                     [(0.0, 50.0), (_n / sr, 50.0)],
                                     f0=np.full(4, _f))
            _ok = len(_ys) > 0 and not np.isnan(_ys).any()
            _msg = f"n={_n} · f0={_f}"
        except Exception as _e:              # noqa: BLE001
            _ok, _msg = False, f"n={_n} · f0={_f} · {_e}"
        check("基音标记顶到末采样也不越界(曲线渲染边界)", _ok, _msg)
    else:
        print("  - 基音标记边界:没找到触发组合(跳过)", flush=True)

    # 常数曲线必须和直接变调逐点同一份(两条路走同一个内核)
    same_psola = sm.render_piece(x, sr, 0, len(x), 50.0, None)
    diff = float(np.max(np.abs(c50 - same_psola)))
    check("常数曲线 == 直接变调(逐点)", diff == 0.0, f"max diff {diff:.2e}")

    # ---- 8 效果器 + 母带 ----
    # (a) Python 的 IR 必须和 engine.js 的 makeIR 是同一串随机数(否则实时和导出两台声音)
    js = (
        "const sr=48000,sec=0.4,decay=0.18;"
        "const n=Math.round(sec*sr),k=Math.round(decay*sr);"
        "for(let ch=0;ch<2;ch++){let s=(12345+ch*999)|0,en=0;"
        "const d=new Float64Array(n);"
        "for(let i=0;i<n;i++){s=(Math.imul(s,1103515245)+12345)|0;"
        "const u=((s>>>0)/4294967296)*2-1;const v=u*Math.exp(-i/k);d[i]=v;en+=v*v;}"
        "const g=1/Math.sqrt(en);const out=[];for(let i=0;i<n;i++)out.push(d[i]*g);"
        "console.log(JSON.stringify(out.slice(0,16)));}"
    )
    try:
        import subprocess
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
        lines = [ln for ln in r.stdout.strip().splitlines() if ln.startswith("[")]
        js_ir = np.array(json.loads(lines[0]) + json.loads(lines[1]), dtype=np.float64)
        py_ir = sm._make_ir(48000, 0.4, 0.18).astype(np.float64)
        ref = np.concatenate([py_ir[0, :16], py_ir[1, :16]])
        err = float(np.max(np.abs(js_ir - ref)))
        check("IR 与 engine.makeIR 逐点一致(1e-6)", err < 1e-6, f"max diff {err:.2e}")
    except FileNotFoundError:
        print("  - 没有 node,跳过 IR 跨语言对照", flush=True)

    # (b) 关掉 FX 必须和阶段 5 逐点一样(含一个空 fx 对象)
    x1 = chord([60, 64, 67], 2.0, sr)
    src = tmp / "fxsrc.wav"
    sf.write(str(src), x1, sr, subtype="PCM_16")
    fxproj = {
        "tracks": [{"id": "t1", "gain": 0.8, "pan": 0.2, "clips": [
            {"id": "c1", "sourceId": "s1", "start": 0.0, "srcIn": 0.0, "srcOut": 2.0,
             "length": 2.0, "gain": 1.0, "fadeIn": 0.02, "fadeOut": 0.05,
             "semis": 0, "cents": 0}]}],
        "sources": {"s1": {"path": str(src)}},
    }
    o0, _ = sm.mix_project(fxproj)
    fxproj["tracks"][0]["fx"] = {}
    o1, _ = sm.mix_project(fxproj)
    fxproj["tracks"][0]["fx"] = {"reverb": {"mix": 0, "size": 1.6},
                                 "delay": {"mix": 0, "time": 0.25, "fb": 0.3}}
    o2, _ = sm.mix_project(fxproj)
    d01 = float(np.max(np.abs(o0 - o1)))
    d02 = float(np.max(np.abs(o0 - o2)))
    check("关掉 FX → 与阶段 5 逐点一致", d01 == 0.0 and d02 == 0.0,
          f"diff {d01:.1e} / {d02:.1e}")

    # (c) 混响公式:干*(1-mix) + conv(源, IR)*mix;并且尾巴真的拖过输入长度
    fxproj["tracks"][0]["fx"] = {"reverb": {"mix": 0.4, "size": 0.6}}
    o3, _ = sm.mix_project(fxproj)
    from scipy.signal import fftconvolve
    gl = math.cos((0.2 + 1) * math.pi / 4)
    mono = o0[:, 0] / gl                                          # 还原单声道干声
    ir = sm._make_ir(sr, 0.6, 0.6 * 0.45).astype(np.float64).mean(axis=0)
    # 注意 fftconvolve 在整轨上做,和 _apply_fx 同一做法
    want_mono = mono * 0.6 + fftconvolve(mono, ir)[: len(mono)] * 0.4
    want = want_mono[:, None] * np.array(
        [gl, math.sin((0.2 + 1) * math.pi / 4)])
    err = float(np.max(np.abs(o3 - want)))
    check("混响公式 干×(1-mix)+conv×mix 逐点成立(1e-3)", err < 1e-3,
          f"max diff {err:.2e}")

    # 尾音:源只有 2 秒混响会拖到 2 秒之后?工程长度跟着 clip 走,所以这里直接量 _apply_fx
    tail = sm._apply_fx(np.concatenate([x1, np.zeros(sr, dtype=np.float32)]),
                        sr, {"reverb": {"mix": 0.5, "size": 1.0}})
    late = float(np.max(np.abs(tail[int(2.2 * sr):])))
    check("混响有真正的尾音(2.2s 之后仍 >0)", late > 1e-4, f"peak {late:.4f}")

    # (d) 延迟:fb=0 时就是原样延后 d 秒、乘 mix
    dly = sm._delay_wet(x1.astype(np.float64), sr, 0.25, 0.0, 1.0)
    d = int(round(0.25 * sr))
    err = float(np.max(np.abs(dly[d:] - x1[:-d])))
    check("延迟 fb=0 时 = 原信号延后 0.25s", err < 1e-6, f"max diff {err:.2e}")

    # (e) 母带:1kHz 正弦 → −14 LUFS ±1,peak ≤ 1 且不削平
    tt = np.arange(int(sr * 10)) / sr
    sine = (0.1 * np.sin(2 * np.pi * 1000 * tt)).astype(np.float32)
    mastered, rep_m = sm.master_to(np.stack([sine, sine], axis=1), sr, -14.0)
    lufs = sm.integrated_lufs(mastered, sr)
    pk = float(np.max(np.abs(mastered)))
    # 「削平」= 连续好几个采样都顶在同一个最大值;正弦即使满幅也只有 1 个连续点
    at = np.abs(np.abs(mastered) - pk) < 1e-6
    run = best = 0
    for v in at[:, 0]:
        run = run + 1 if v else 0
        best = max(best, run)
    check("导出响度 −14 ±1 LUFS", abs(lufs + 14.0) <= 1.0,
          f"{rep_m['lufs_before']} → {lufs:.2f} (报告 {rep_m['lufs_after']})")
    check("限幅后 peak ≤ 1 且不削平", pk <= 1.0 and pk < 10 ** (-0.25 / 20)
          and best <= 2, f"peak {20*math.log10(pk):+.2f}dBFS · 最长贴顶连续 {best} 点")

    # 已经被削平的方波再放大,也不许自己变方(限幅器只压不塑形)
    hot, _ = sm.master_to(np.stack([sine * 40, sine * 40], axis=1), sr, -9.0)
    pk2 = float(np.max(np.abs(hot)))
    check("超响素材 → 限幅到 ceiling 以内", pk2 < 10 ** (-0.25 / 20),
          f"peak {20*math.log10(pk2):+.2f}dBFS")

    # (f) mix_project 的 loudness 参数真的接上了
    _o4, rep4 = sm.mix_project(fxproj, loudness=-14.0)
    check("mix_project(loudness=-14) 报告实测响度", "loudness" in rep4
          and abs(rep4["loudness"]["lufs_after"] + 14.0) <= 1.0,
          str(rep4.get("loudness")))

    # ---- 9 插入效果链(内置插件)----
    import studio_fx as fx
    import time as _time

    ts = np.arange(int(sr * 1.5)) / sr

    def sine(amp, hz):
        return (amp * np.sin(2 * np.pi * hz * ts)).astype(np.float32)

    def rms_db(y, skip=0.3):
        seg = y[int(skip * sr):]
        return 20 * math.log10(float(np.sqrt(np.mean(np.square(seg, dtype=np.float64)))) + 1e-12)

    # (a) 空链 = 一个采样都不动(旧工程兼容的硬保证)
    xsig = sine(0.3, 220.0)
    same = fx.apply_chain(xsig, sr, [])
    check("空链 export 逐点不变", float(np.max(np.abs(same - xsig))) == 0.0)
    check("空链(全未知类型)也逐点不变",
          float(np.max(np.abs(fx.apply_chain(xsig, sr, [{"type": "nope"}]) - xsig))) == 0.0)

    # (b) 低通 1kHz:200Hz 基本不动,8kHz 掉下去
    y200 = fx.apply_chain(sine(0.3, 200.0), sr, [{"id": "f", "type": "filter",
                         "params": {"mode": "lowpass", "cutoff": 1000, "q": 0.707}}])
    y8k = fx.apply_chain(sine(0.3, 8000.0), sr, [{"id": "f", "type": "filter",
                        "params": {"mode": "lowpass", "cutoff": 1000, "q": 0.707}}])
    d200 = rms_db(y200) - rms_db(sine(0.3, 200.0))
    d8k = rms_db(y8k) - rms_db(sine(0.3, 8000.0))
    check("低通 1k:200Hz 衰减 <1dB", abs(d200) < 1.0, f"{d200:+.2f}dB")
    check("低通 1k:8kHz 衰减 >20dB", d8k < -20.0, f"{d8k:+.1f}dB")

    # (c) EQ 高架 +6dB
    eq = [{"id": "e", "type": "eq3", "params": {"lowFreq": 120, "lowGain": 0,
          "midFreq": 1000, "midQ": 1, "midGain": 0, "highFreq": 6000, "highGain": 6}}]
    # ★ 高架在 6kHz 是**过渡点**,8kHz 只到 +4.7dB;量渐近端(16k)才是 6dB
    d_eq8k = rms_db(fx.apply_chain(sine(0.3, 16000.0), sr, eq)) - rms_db(sine(0.3, 16000.0))
    d_eq200 = rms_db(fx.apply_chain(sine(0.3, 200.0), sr, eq)) - rms_db(sine(0.3, 200.0))
    check("EQ 高架 +6dB:16k(渐近端)提升 6±1", abs(d_eq8k - 6.0) <= 1.0, f"{d_eq8k:+.2f}dB")
    check("EQ 高架 +6dB:200Hz 变化 <1", abs(d_eq200) < 1.0, f"{d_eq200:+.2f}dB")

    # (d) 失真:THD 至少升一个数量级
    def thd(y):
        seg = y[int(0.3 * sr):int(0.3 * sr) + 4096 * 8]
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        k = int(np.argmax(sp))
        f0 = sp[k]
        h = 0.0
        for m in range(2, 9):
            j = k * m
            if j < len(sp):
                h += sp[j] ** 2
        return math.sqrt(h) / max(f0, 1e-12)
    dry = sine(0.3, 220.0)
    d1 = fx.apply_chain(dry, sr, [{"id": "d", "type": "distortion",
                                   "params": {"drive": 1, "tone": 18000, "mix": 1}}])
    d10 = fx.apply_chain(dry, sr, [{"id": "d", "type": "distortion",
                                    "params": {"drive": 10, "tone": 18000, "mix": 1}}])
    t1, t10 = thd(d1), thd(d10)
    check("失真 drive=10:THD 升 ≥10 倍", t10 > 10 * max(t1, 1e-6) and t10 > 0.1,
          f"THD {t1:.4f} → {t10:.4f}")
    check("失真输出无 NaN、峰值有界",
          (not np.isnan(d10).any()) and float(np.max(np.abs(d10))) < 4.0,
          f"peak {float(np.max(np.abs(d10))):.2f}")

    # (e) 压缩 −18dB / 4:1:输入 −6dBFS → 峰值 ≈ −15dBFS
    comp = [{"id": "c", "type": "compressor", "params": {
        "threshold": -18, "ratio": 4, "attack": 5, "release": 100,
        "knee": 0, "makeup": 0}}]
    yc = fx.apply_chain(sine(0.5, 220.0), sr, comp)
    pk_c = 20 * math.log10(float(np.max(np.abs(yc[int(0.6 * sr):]))) + 1e-12)
    check("压缩 −18/4:−6dBFS → −15dBFS ±1", abs(pk_c + 15.0) <= 1.0, f"{pk_c:+.2f}dBFS")

    # (f) 限幅 ceiling=−1dB(★ 无前瞻,只量稳态)
    yl = fx.apply_chain(sine(1.0, 220.0), sr,
                        [{"id": "l", "type": "limiter",
                          "params": {"ceiling": -1, "release": 50}}])
    pk_l = 20 * math.log10(float(np.max(np.abs(yl[int(0.5 * sr):]))) + 1e-12)
    check("限幅 ceiling=−1:稳态峰值 ≤ −0.95dBFS", pk_l <= -0.95 + 1e-3, f"{pk_l:+.2f}dBFS")

    # (g) 合唱:输出与干声不同,且最佳延迟随 LFO 变化
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(sr) * 0.2).astype(np.float32)
    ych = fx.apply_chain(noise, sr, [{"id": "h", "type": "chorus", "params": {
        "rate": 0.5, "depth": 6, "base": 12, "mix": 1}}])
    check("合唱:输出与干声不同", float(np.max(np.abs(ych - noise))) > 1e-3)
    lags = []
    for t0 in (0.15, 0.55, 0.85):
        a = int(t0 * sr)
        seg = ych[a:a + 2048].astype(np.float64)
        best, bl = -2.0, 0
        for lag in range(int(0.006 * sr), int(0.020 * sr)):
            ref = noise[a - lag:a - lag + 2048].astype(np.float64)
            c = float(np.dot(seg, ref) / (np.linalg.norm(seg) * np.linalg.norm(ref) + 1e-12))
            if c > best:
                best, bl = c, lag
        lags.append(bl)
    check("合唱:最佳延迟随 LFO 变化(方差 > 0)",
          float(np.var(lags)) > 1.0, f"lags {lags}")

    # (h) 镶边:有反馈时出现梳状凹陷
    yf0 = fx.apply_chain(noise, sr, [{"id": "g", "type": "flanger",
                          "params": {"rate": 0.3, "depth": 2, "base": 1.5,
                                     "feedback": 0, "mix": 1}}])
    yf5 = fx.apply_chain(noise, sr, [{"id": "g", "type": "flanger",
                          "params": {"rate": 0.3, "depth": 2, "base": 1.5,
                                     "feedback": 0.5, "mix": 1}}])
    sp0 = 20 * np.log10(np.abs(np.fft.rfft(yf0[4096:4096 + 8192])) + 1e-9)
    sp5 = 20 * np.log10(np.abs(np.fft.rfft(yf5[4096:4096 + 8192])) + 1e-9)
    dsp = float(np.max(np.abs(sp0 - sp5)))
    check("镶边 feedback>0 频谱变化 >3dB", dsp > 3.0, f"{dsp:.1f}dB")

    # (i) 颤音 depth=1:包络最大/最小 > 10
    yt = fx.apply_chain(sine(0.5, 220.0), sr,
                        [{"id": "t", "type": "tremolo",
                          "params": {"rate": 4, "depth": 1}}])
    win = int(0.005 * sr)
    envs = [float(np.max(np.abs(yt[i:i + win]))) for i in range(0, len(yt) - win, win)]
    ratio = max(envs) / max(min(envs), 1e-6)
    check("颤音 depth=1:包络最大/最小 >10", ratio > 10.0, f"{ratio:.1f}×")

    # (j) 比特压缩 bits=4:落在 1/8 格点上,保持窗内相等
    yb = fx.apply_chain(sine(0.4, 110.0), sr,
                        [{"id": "b", "type": "bitcrush",
                          "params": {"bits": 4, "crushHz": 6000, "mix": 1}}])
    grid = float(np.max(np.abs(yb.astype(np.float64) * 8 - np.round(yb.astype(np.float64) * 8))))
    hold = max(1, int(round(sr / 6000)))
    held_ok = all(abs(float(yb[i]) - float(yb[i + 1])) < 1e-7
                  for i in range(0, min(len(yb) - 1, 4000), hold))
    check("比特压缩 bits=4:落在 1/8 格点", grid < 1e-6, f"max 偏差 {grid:.2e}")
    check("比特压缩:保持窗内相邻采样相等", held_ok, f"hold={hold}")

    # (k) 链顺序真的生效
    chain_a = [{"id": "1", "type": "distortion", "params": {"drive": 12, "tone": 6000, "mix": 1}},
               {"id": "2", "type": "filter", "params": {"mode": "lowpass", "cutoff": 800, "q": 0.707}}]
    chain_b = [{"id": "2", "type": "filter", "params": {"mode": "lowpass", "cutoff": 800, "q": 0.707}},
               {"id": "1", "type": "distortion", "params": {"drive": 12, "tone": 6000, "mix": 1}}]
    ya = fx.apply_chain(dry, sr, chain_a)
    yb2 = fx.apply_chain(dry, sr, chain_b)
    dd = float(np.max(np.abs(ya - yb2)))
    check("链顺序:失真→滤波 ≠ 滤波→失真", dd > 1e-3, f"max diff {dd:.3f}")

    # (l) sanitize:越界夹住、未知跳过、链长封顶、重复 id 修掉
    bad = [{"id": "x", "type": "filter", "params": {"cutoff": 999999, "q": -5, "mode": "wat"}},
           {"id": "x", "type": "compressor", "params": {"threshold": -999}},
           {"id": "y", "type": "unknown_thing"},
           "not a dict"]
    bad += [{"id": f"z{i}", "type": "tremolo", "params": {}} for i in range(30)]
    sc = fx.sanitize_chain(bad)
    cut = sc[0]["params"]["cutoff"] if sc else 0
    qv = sc[0]["params"]["q"] if sc else 0
    ok_san = (len(sc) == fx.MAX_CHAIN
              and cut == 20000 and qv == 0.1
              and sc[0]["params"]["mode"] == "lowpass"
              and sc[1]["id"] != sc[0]["id"]
              and all(s["type"] != "unknown_thing" for s in sc))
    check("sanitize:夹参数/跳未知/封顶/去重 id", ok_san,
          f"len {len(sc)} cutoff {cut} q {qv} ids {sc[0]['id']},{sc[1]['id']}")

    # (m) 分轨:pre 各轨按声像/增益相加 == 关闭链与 FX 的主混音
    st_src = tmp / "stem_src.wav"
    sf.write(str(st_src), chord([60, 64, 67], 1.2, sr), sr, subtype="PCM_16")
    st_proj = {
        "lengthSec": 1.2,
        "tracks": [
            {"id": "t1", "name": "主唱", "gain": 0.8, "pan": -0.35,
             "clips": [{"id": "c1", "sourceId": "s1", "start": 0.0, "srcIn": 0.0,
                        "srcOut": 1.2, "length": 1.2, "gain": 1.0,
                        "fadeIn": 0.02, "fadeOut": 0.05, "semis": 0, "cents": 0}]},
            {"id": "t2", "name": "伴奏", "gain": 0.5, "pan": 0.4,
             "clips": [{"id": "c2", "sourceId": "s1", "start": 0.2, "srcIn": 0.0,
                        "srcOut": 1.0, "length": 1.0, "gain": 1.0,
                        "fadeIn": 0.0, "fadeOut": 0.0, "semis": 0, "cents": 0}]},
        ],
        "sources": {"s1": {"path": str(st_src)}},
    }
    main_out, _ = sm.mix_project(st_proj)
    stems, miss = sm.render_stems(st_proj, stage="pre")
    rebuilt = np.zeros_like(main_out, dtype=np.float64)
    for st in stems:
        tr = next(t for t in st_proj["tracks"] if t["id"] == st["id"])
        th = (max(-1.0, min(1.0, tr.get("pan", 0))) + 1.0) * math.pi / 4.0
        a = st["audio"].astype(np.float64)
        rebuilt[:, 0] += a * math.cos(th)
        rebuilt[:, 1] += a * math.sin(th)
    err_st = float(np.max(np.abs(rebuilt - main_out)))
    check("stems:轨数 = 轨道数、无缺失", len(stems) == 2 and not miss,
          f"{len(stems)} 轨 · missing {miss}")
    check("stems:每轨长度 = 工程长度",
          all(len(s["audio"]) == len(main_out) for s in stems),
          str([len(s["audio"]) for s in stems]))
    check("stems(pre)按声像相加 == 主混音(<1e-4)", err_st < 1e-4, f"max diff {err_st:.2e}")

    # post 链必须和 pre 不同(有插件时)
    st_proj["tracks"][0]["fx"] = {"chain": [{"id": "c", "type": "distortion",
                                             "params": {"drive": 8, "tone": 6000, "mix": 1}}]}
    pre2, _ = sm.render_stems(st_proj, stage="pre")
    post2, _ = sm.render_stems(st_proj, stage="post")
    dpre = float(np.max(np.abs(pre2[0]["audio"] - post2[0]["audio"])))
    check("stems:post 链与 pre 不同", dpre > 1e-3, f"max diff {dpre:.3f}")

    # (n) 性能:3 分钟 + 3 个效果器
    long_n = int(180 * sr)
    long_x = (0.2 * np.sin(2 * np.pi * 220 * np.arange(long_n) / sr)).astype(np.float32)
    perf_chain = [
        {"id": "p1", "type": "distortion", "params": {"drive": 4, "tone": 8000, "mix": 0.6}},
        {"id": "p2", "type": "compressor", "params": {"threshold": -18, "ratio": 3,
         "attack": 12, "release": 140, "knee": 6, "makeup": 0}},
        {"id": "p3", "type": "chorus", "params": {"rate": 0.5, "depth": 6,
         "base": 12, "mix": 0.4}},
    ]
    t_perf = _time.perf_counter()
    _ = fx.apply_chain(long_x, sr, perf_chain)
    cost = _time.perf_counter() - t_perf
    check("3 分钟 × 3 个效果器 < 3 秒", cost < 3.0, f"{cost:.2f}s")

    # (o) ★ 浏览器 worklet ↔ numpy:同一段信号、同一条链,必须逐点一致
    #     (只看 RMS/峰值不够 —— 相位或延迟错位也可能凑出一样的 RMS)
    import subprocess
    try:
        pn = 8192
        pseed = 7
        ss = pseed
        pin = np.empty(pn, dtype=np.float32)
        for i in range(pn):
            ss = (ss * 1664525 + 1013904223) & 0xFFFFFFFF
            pin[i] = np.float32(((ss / 4294967296.0) * 2 - 1) * 0.3)
        pchain = [
            {"id": "1", "type": "filter",
             "params": {"mode": "lowpass", "cutoff": 1000, "q": 0.707}},
            {"id": "2", "type": "compressor",
             "params": {"threshold": -18, "ratio": 3, "attack": 12,
                        "release": 140, "knee": 6, "makeup": 0}},
            {"id": "3", "type": "bitcrush",
             "params": {"bits": 6, "crushHz": 6000, "mix": 1}},
        ]
        rr = subprocess.run(
            ["node", "tools/fx_parity.js",
             json.dumps({"sr": sr, "n": pn, "seed": pseed, "chain": pchain})],
            cwd=str(ROOT), capture_output=True, text=True, timeout=90)
        if rr.returncode != 0:
            check("worklet ↔ numpy 逐点一致", False, (rr.stderr or "")[-200:])
        else:
            jd = json.loads(rr.stdout)
            py = fx.apply_chain(pin, sr, pchain).astype(np.float64)
            e_head = float(np.max(np.abs(py[:64] - np.array(jd["samples"]))))
            e_tail = float(np.max(np.abs(py[-64:] - np.array(jd["tail"]))))
            e_rms = abs(float(np.sqrt(np.mean(py ** 2))) - float(jd["rms"]))
            ok_par = max(e_head, e_tail) < 1e-6 and e_rms < 1e-6
            check("worklet ↔ numpy 逐点一致(<1e-6)", ok_par,
                  f"head {e_head:.1e} · tail {e_tail:.1e} · rms {e_rms:.1e}")
    except FileNotFoundError:
        print("  (跳过 worklet↔numpy 对照:本机没有 node)")

    # ---- 10 一键调音(本机测量 + 写死的规则,不是模型)----
    #   这里只验「方案对不对」;应用/撤销是前端的事,归冒烟测。
    import studio_auto as sa

    def tone(amp, hz, sec):
        t = np.arange(int(sec * sr)) / sr
        return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)

    def pluck(amp, hz, sec, per_sec=4.0, duty=0.25):
        """带门控的音:峰值高、平均响度低 —— 专门用来撞峰值保护。"""
        t = np.arange(int(sec * sr)) / sr
        gate = (np.mod(t * per_sec, 1.0) < duty).astype(np.float32)
        return (amp * np.sin(2 * np.pi * hz * t) * gate).astype(np.float32)

    va = tmp / "auto_vocal.wav"
    ba = tmp / "auto_backing.wav"
    sf.write(str(va), sum((0.06 / k) * tone(1.0, 220.0 * k, 4.0)
                          for k in range(1, 6)).astype(np.float32),
             sr, subtype="FLOAT")
    sf.write(str(ba), tone(0.40, 110.0, 4.0), sr, subtype="FLOAT")

    def _aclip(cid, sid, kind="note"):
        return {"id": cid, "sourceId": sid, "start": 0.0, "srcIn": 0.0,
                "srcOut": 4.0, "length": 4.0, "gain": 1.0, "fadeIn": 0.0,
                "fadeOut": 0.0, "semis": 0, "cents": 0, "kind": kind}

    def _aproj(backs, vocal_path=None, fx=None):
        """backs = [(路径, 轨增益), …];人声固定一条,轨名为「主唱」。"""
        sources = {"sv": {"path": str(vocal_path or va)}}
        tracks = [{"id": "tv", "name": "主唱", "kind": "voice", "gain": 1.0,
                   "pan": 0.0, "clips": [_aclip("c_v", "sv")]}]
        for i, (pth, g) in enumerate(backs):
            sid = f"sb{i}"
            sources[sid] = {"path": str(pth)}
            tracks.append({"id": f"tb{i}", "name": f"伴奏{i}", "kind": "audio",
                           "gain": g, "pan": 0.0,
                           "clips": [_aclip(f"c_b{i}", sid, "audio")]})
        if fx:
            tracks[0]["fx"] = fx
        return {"lengthSec": 4.0, "sources": sources, "tracks": tracks}

    plan = sa.auto_mix(_aproj([(ba, 1.0)]), "all", False)
    ms_ = {m["trackId"]: m for m in plan["measures"]}
    gv = next((a for a in plan["actions"] if a["kind"] == "gain"
               and a["trackId"] == "tv"), None)
    gb = next((a for a in plan["actions"] if a["kind"] == "gain"
               and a["trackId"] == "tb0"), None)
    check("一键调音:测出人声/伴奏两条 + 角色判对",
          len(plan["measures"]) == 2 and ms_.get("tv", {}).get("role") == "vocal"
          and ms_.get("tb0", {}).get("role") == "backing",
          str({k: v.get("role") for k, v in ms_.items()}))
    check("一键调音:人声被往上提,且不超过 +9 dB",
          bool(gv) and 0.0 < float(gv["db"]) <= 9.0,
          f"{gv and gv['db']:+.1f}dB · {gv and gv['why']}")
    check("一键调音:单轨伴奏只往下压(目标 −20 LUFS)",
          bool(gb) and float(gb["db"]) <= 0.0,
          f"{gb and gb['db']:+.1f}dB · {gb and gb['why']}")

    # 多轨伴奏:各自的内部平衡不动,只调人声
    plan2 = sa.auto_mix(_aproj([(ba, 1.0), (ba, 0.7)]), "all", False)
    bg2 = [a for a in plan2["actions"] if a["kind"] == "gain"
           and str(a["trackId"]).startswith("tb")]
    check("一键调音:多轨伴奏不各自调增益",
          not bg2 and any("内部平衡没动" in n for n in plan2["notes"]),
          f"伴奏动作 {len(bg2)} 个 · {plan2['notes'][:1]}")

    # 峰值保护:推上去会削顶 ⇒ 只提到 −1 dB,并写在 why 里
    vp = tmp / "auto_vocal_peak.wav"
    sf.write(str(vp), pluck(0.60, 220.0, 4.0), sr, subtype="FLOAT")
    plan3 = sa.auto_mix(_aproj([(ba, 1.0)], vocal_path=vp), "vol", False)
    g3 = next((a for a in plan3["actions"] if a["kind"] == "gain"
               and a["trackId"] == "tv"), None)
    peak3 = next((m for m in plan3["measures"] if m["trackId"] == "tv"), {})
    expect3 = -1.0 - float(peak3.get("peak_db") or 0)
    check("一键调音:峰值保护只提到 −1 dB",
          bool(g3) and float(g3["db"]) <= expect3 + 0.2 and "削顶" in str(g3["why"]),
          f"峰值 {peak3.get('peak_db')} dB → 提 {g3 and g3['db']:+.1f} dB")

    # 已经有 VST 混响 / 已经加了混响 ⇒ 不再加一条旧混响(避免双混响)
    fx_vst = {"vst": [{"id": "v1", "name": "DragonflyHallReverb", "path": "nope"}]}
    plan4 = sa.auto_mix(_aproj([(ba, 1.0)], fx=fx_vst), "fx", False)
    check("一键调音:已有 VST 混响就不加旧混响",
          not [a for a in plan4["actions"] if a["kind"] == "reverb"],
          str([a["kind"] for a in plan4["actions"]]))
    plan5 = sa.auto_mix(_aproj([(ba, 1.0)],
                               fx={"reverb": {"mix": 0.2, "size": 1.2}}), "fx", False)
    check("一键调音:已有混响就不再动它",
          not [a for a in plan5["actions"] if a["kind"] == "reverb"],
          str([a["kind"] for a in plan5["actions"]]))
    # 自动效果槽必须带 auto:true(前端据此「重跑替换、不碰手动槽」)
    ch = [a for a in plan["actions"] if a["kind"] == "chain"]
    check("一键调音:自动效果槽都打 auto:true",
          all(s.get("auto") is True for a in ch for s in (a.get("slots") or [])),
          f"{sum(len(a.get('slots') or []) for a in ch)} 个槽")

    # check_pitch:阈值必须进缓存 key,30 与 45 互不命中
    p40 = tmp / "sentence40.wav"
    sf.write(str(p40), sentence([(261.63, 40, 0.45), (293.66, 40, 0.45),
                                 (329.63, 40, 0.45), (261.63, 40, 0.45)]),
             sr, subtype="PCM_16")
    c30 = sm.check_pitch(p40, 30.0, 100.0)
    c45 = sm.check_pitch(p40, 45.0, 100.0)
    check("check_pitch:30 与 45 的缓存 key 不同、互不命中",
          c30.get("key") != c45.get("key") and c45.get("cached") is False,
          f"{c30.get('key')} vs {c45.get('key')} cached={c45.get('cached')}")
    check("check_pitch:偏 40 音分在 30 下红、45 下不红",
          int(c30.get("red") or 0) >= 1 and int(c45.get("red") or 0) == 0,
          f"红 {c30.get('red')} → {c45.get('red')}")
    c30b = sm.check_pitch(p40, 30.0, 100.0)
    check("check_pitch:同阈值再来一次命中缓存", c30b.get("cached") is True)

    # ---- 11 VST 参数显示:_param_view 不许把 min/max 丢成 0–1 ----
    #   ★ 这里用**假的**参数对象,所以不需要装任何插件;没有 pedalboard 就整段跳过。
    try:
        import studio_vst_worker as vw
    except Exception as _e:                 # noqa: BLE001
        print(f"  (跳过 _param_view:没有 pedalboard —— {_e})")
        vw = None
    if vw is not None:
        class _FP:
            pass

        class _FPlug:
            pass

        fp = _FP()
        fp.name = "frequency_hz"
        fp.range = (20.0, 20000.0, None)     # ★ 步长是 None,以前会连 min/max 一起丢
        fp.raw_value = 0.0037               # 0–1 的原始值
        fp.units = "Hz"
        fpl = _FPlug()
        fpl.frequency_hz = 80.0
        v = vw._param_view(fp, fpl, "frequency_hz")
        check("_param_view:step=None 时 min/max 还在、default 是自然值",
              v["min"] == 20.0 and v["max"] == 20000.0 and v["step"] is None
              and abs(float(v["default"]) - 80.0) < 1e-6 and v["type"] == "number",
              str(v))

        fs = _FP()
        fs.name = "hp_freq_hz"
        fs.range = (0.0, 1.0, None)
        fs.raw_value = 0.0
        fs.units = None
        fpl2 = _FPlug()
        fpl2.hp_freq_hz = "Off"
        v2 = vw._param_view(fs, fpl2, "hp_freq_hz")
        check("_param_view:字符串参数原样读回(Off),不假装成数字",
              v2["type"] == "string" and v2["default"] == "Off", str(v2))

        fb = _FP()
        fb.name = "bypass"
        fb.range = (0.0, 1.0, None)
        fb.raw_value = 0.0
        fpl3 = _FPlug()
        fpl3.bypass = True
        v3 = vw._param_view(fb, fpl3, "bypass")
        check("_param_view:布尔参数是 bool(复选框),不是 0/1 数字框",
              v3["type"] == "bool" and v3["default"] is True, str(v3))

    # ---- 12 按字/音节切分 ----
    syllable_section(tmp, sr)

    # ---- 13 识别对齐(没有时间戳时的估算) ----
    asr_section(tmp, sr)

    # ---- 14 补音(本地 + 云端降级) ----
    voice_section(tmp, sr)

    # ---- 15 自然语言调参(上下文清洗 + 校验) ----
    nl_section(tmp, sr)

    vst_section(tmp, sr)

    print("\n" + "=" * 52)
    print(f"  通过 {OK}/{OK + BAD}")
    print("=" * 52)
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
