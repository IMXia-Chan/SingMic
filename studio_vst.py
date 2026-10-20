"""VST3 宿主(父进程侧)。

★ 这是**唯一**和插件打交道的入口,但它自己 **不 import pedalboard** ——
  pedalboard 只能在独立子进程(`studio_vst_worker.py`)里用。
  这样:插件崩了杀不到服务;而且不装 pedalboard 时主程序照常跑。
  (本项目是 AGPL-3.0,与 GPL-3.0 相容,所以这里不是许可隔离,是稳定性隔离。)

★ 这台机上已经验过的事实(别改回去):
  - pedalboard **不能**加载 `.vst3` 目录,必须指到
    `Contents/x86_64-win/<名字>.vst3` 那个真 DLL;
  - `plugin.parameters` 是只读 dict,改参数要 `setattr`;
  - `bypass=True` 与干声逐点一致。

协议:一行一个 JSON,音频走临时 WAV(不塞 base64)。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent
WORKER = ROOT / "studio_vst_worker.py"
TMP = ROOT / "projects" / "tmp"

MAX_PER_TRACK = 8
PLUGIN_EXT = ".vst3"
ARCH_DIRS = ("x86_64-win", "x86-win", "arm64-win")


class VstUnavailable(RuntimeError):
    """工程里有 VST,但没给宿主 —— 宁可报错,绝不静默当成没有插件。"""


class VstError(RuntimeError):
    """插件加载/处理失败(消息会直接给用户看)。"""


# ---------------------------------------------------------------------------
# 目录与路径安全
# ---------------------------------------------------------------------------
def plugin_roots() -> list[Path]:
    """允许放插件的目录。★ 客户端传进来的路径必须落在这些目录下。"""
    roots = [ROOT / "vst"]
    extra = os.environ.get("SINGMIC_VST_DIRS", "")
    for part in extra.split(os.pathsep):
        if part.strip():
            roots.append(Path(part.strip()))
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    common = os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files")
    roots.append(Path(common) / "VST3")
    roots.append(Path(pf) / "Common Files" / "VST3")
    # ★ CommonProgramFiles 和 Program Files\Common Files 常常是同一个目录,去重
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        try:
            k = str(r.resolve()).lower()
        except OSError:
            k = str(r).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def ensure_vst_dir() -> Path:
    d = ROOT / "vst"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _inside(p: Path, roots: list[Path]) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    for r in roots:
        try:
            if rp.is_relative_to(r.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def allowed_plugin_path(p: str | Path) -> Path:
    """把客户端给的路径校验成「允许目录里的 .vst3」。不合格就抛 VstError。"""
    if not p or "\x00" in str(p):
        raise VstError("插件路径是空的")
    path = Path(str(p))
    if not _inside(path, plugin_roots()):
        raise VstError("这个插件不在允许的目录里")
    if not path.exists():
        raise VstError(f"插件不存在:{path}")
    dll = resolve_bundle(path)
    if dll is None:
        raise VstError("这不是一个能用的 .vst3(x86_64-win)")
    if not _inside(dll, plugin_roots()):
        raise VstError("插件路径越界")
    return dll


def resolve_bundle(path: Path) -> Path | None:
    """`.vst3` 目录 → 内部真 DLL;已经是 DLL 就原样返回。

    ★ pedalboard 直接吃 bundle 目录会失败(`unsupported plugin format`),
      必须落到 `Contents/<arch>/<名字>.vst3`。
    """
    try:
        if path.is_file() and path.suffix.lower() == PLUGIN_EXT:
            return path
        if not path.is_dir():
            return None
        contents = path / "Contents"
        for arch in ARCH_DIRS:
            d = contents / arch
            if d.is_dir():
                for f in sorted(d.glob(f"*{PLUGIN_EXT}")):
                    if f.is_file():
                        return f
        # 兜底:某些打包工具目录名不标准
        if contents.is_dir():
            for f in sorted(contents.rglob(f"*{PLUGIN_EXT}")):
                if f.is_file():
                    return f
    except OSError:
        return None
    return None


def plugin_name(path: Path) -> str:
    if path.suffix.lower() == PLUGIN_EXT and path.parent.name in ARCH_DIRS:
        return path.parent.parent.parent.name
    return path.stem


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def scan_plugins() -> list[dict]:
    """扫允许目录,返回可直接加进轨道的插件列表。

    ★ 遇到 `.vst3` 目录就**不再往里走**,否则会把内部 DLL 当成第二个插件。
    """
    roots = plugin_roots()
    ensure_vst_dir()
    out: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir():
                        if e.suffix.lower() == PLUGIN_EXT:
                            item = _scan_one(e)
                            if item and item["path"] not in seen:
                                seen.add(item["path"])
                                out.append(item)
                        else:
                            stack.append(e)
                    elif e.suffix.lower() == PLUGIN_EXT:
                        item = _scan_one(e)
                        if item and item["path"] not in seen:
                            seen.add(item["path"])
                            out.append(item)
                except OSError:
                    continue
    out.sort(key=lambda x: x["name"].lower())
    return out


def _scan_one(p: Path) -> dict | None:
    dll = resolve_bundle(p)
    if dll is None:
        return {"name": plugin_name(p), "bundle": str(p), "path": "",
                "ok": False, "error": "找不到 x86_64-win 的内部 DLL"}
    return {"name": plugin_name(p), "bundle": str(p), "path": str(dll),
            "ok": True, "error": ""}


# ---------------------------------------------------------------------------
# 槽位清洗(和 studio_fx.sanitize_chain 同一套态度:能修的修,不能用的报错)
# ---------------------------------------------------------------------------
def sanitize_slots(slots) -> list[dict]:
    if not isinstance(slots, list):
        raise VstError("VST 链格式不对")
    if len(slots) > MAX_PER_TRACK:
        raise VstError(f"每轨最多 {MAX_PER_TRACK} 个 VST 插件")
    out: list[dict] = []
    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            raise VstError(f"第 {i + 1} 个 VST 槽位格式不对")
        dll = allowed_plugin_path(s.get("path") or "")
        params = s.get("params")
        clean: dict = {}
        if isinstance(params, dict):
            for k, v in params.items():
                if not isinstance(k, str) or not k:
                    continue
                if isinstance(v, bool):
                    clean[k] = float(v)
                elif isinstance(v, (int, float)):
                    clean[k] = float(v)
                elif isinstance(v, str):
                    clean[k] = v
        out.append({"id": str(s.get("id") or f"vst_{i + 1}"),
                    "name": str(s.get("name") or plugin_name(dll)),
                    "path": str(dll), "bundle": str(s.get("bundle") or ""),
                    "bypass": bool(s.get("bypass")),
                    "params": clean})
    return out


NONDET_EPS = 1e-6


def nondet_of(y1, y2) -> bool:
    """两次渲染的结果是否「不一样」。

    ★ 有的插件本来就不确定(内部随机 / free-run LFO / 抖动噪声),比如
      Airwindows Consolidated 自带 dither —— 实测连跑两遍就差 1e-1 量级。
      冻结只能算预览,这件事必须让用户看见,不能装作每次都一样。
    """
    try:
        a = np.asarray(y1, dtype=np.float64).reshape(-1)
        b = np.asarray(y2, dtype=np.float64).reshape(-1)
    except Exception:                      # noqa: BLE001
        return True
    if len(a) != len(b):
        return True
    if not len(a):
        return False
    return bool(float(np.max(np.abs(a - b))) > NONDET_EPS)


def project_has_vst(project: dict | None) -> bool:
    for tr in (project or {}).get("tracks") or []:
        fx = tr.get("fx")
        if isinstance(fx, dict) and isinstance(fx.get("vst"), list) and fx["vst"]:
            return True
    return False


# ---------------------------------------------------------------------------
# 冻结签名
# ---------------------------------------------------------------------------
def track_sig(project: dict, track: dict, sr: int = 48000) -> str:
    """这一轨「冻结结果」的指纹:内容、增益、内置链、VST 参数、源文件身份。

    ★ 和导出用的是同一批输入 ⇒ 参数没变就一定命中同一条缓存;
      任何一个会改变声音的字段变了,签名就变。
    """
    import hashlib

    keys = ("id", "kind", "clips", "notes", "sample", "attack", "release", "gain")
    tr_view = {k: track.get(k) for k in keys}
    used = set()
    for c in track.get("clips") or []:
        if c.get("sourceId"):
            used.add(c["sourceId"])
    smp = track.get("sample") or {}
    if smp.get("sourceId"):
        used.add(smp["sourceId"])
    srcs = {}
    for sid in used:
        s = (project.get("sources") or {}).get(sid) or {}
        srcs[sid] = [s.get("path"), s.get("size"), s.get("mtime")]
    fx = track.get("fx") or {}
    payload = {
        "v": "post-vst-v1",
        "sr": int(sr),
        "track": tr_view,
        "chain": fx.get("chain") if isinstance(fx, dict) else None,
        "vst": fx.get("vst") if isinstance(fx, dict) else None,
        "sources": srcs,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------------------
# worker 进程
# ---------------------------------------------------------------------------
class _Worker:
    """常驻子进程。一行 JSON 进,一行 JSON 出。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.err: deque[str] = deque(maxlen=60)

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        TMP.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        self.q = queue.Queue()
        self.err.clear()
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(WORKER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=str(ROOT), env=env)
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_out(self) -> None:
        try:
            for line in self.proc.stdout:      # type: ignore[union-attr]
                self.q.put(line)
        finally:
            self.q.put(None)

    def _read_err(self) -> None:
        try:
            for line in self.proc.stderr:      # type: ignore[union-attr]
                self.err.append(line.rstrip())
        except Exception:                      # noqa: BLE001
            pass

    def call(self, req: dict, timeout: float) -> dict:
        self.start()
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception as e:                 # noqa: BLE001
            self.kill()
            raise VstError(f"VST 进程没在跑:{e}") from e
        try:
            line = self.q.get(timeout=max(1.0, timeout))
        except queue.Empty:
            self.kill()
            raise VstError(f"VST 插件超时(等了 {timeout:.0f} 秒)")
        if line is None:
            tail = " | ".join(list(self.err)[-6:])
            self.kill()
            raise VstError("VST 进程退出了" + (f":{tail}" if tail else ""))
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            self.kill()
            raise VstError(f"VST 进程回了一行看不懂的东西:{line[:200]}") from e
        if not data.get("ok"):
            raise VstError(str(data.get("error") or "插件处理失败"))
        return data

    def kill(self) -> None:
        p = self.proc
        self.proc = None
        if p is None:
            return
        try:
            p.kill()
        except Exception:                      # noqa: BLE001
            pass
        try:
            p.wait(timeout=3)
        except Exception:                      # noqa: BLE001
            pass


class VstHost:
    """一次导出 / 一次冻结用一个实例;多条轨复用同一个 worker。"""

    def __init__(self, timeout: float | None = None) -> None:
        TMP.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix="vst-", dir=str(TMP)))
        self.worker = _Worker()
        self.timeout = timeout
        self._seq = 0

    def __enter__(self) -> "VstHost":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.worker.kill()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _paths(self) -> tuple[Path, Path]:
        self._seq += 1
        return (self.dir / f"in_{self._seq}.wav",
                self.dir / f"out_{self._seq}.wav")

    def inspect(self, path: str | Path, params: dict | None = None) -> dict:
        dll = allowed_plugin_path(path)
        req = {"op": "inspect", "path": str(dll)}
        if params:
            req["params"] = params
        data = self.worker.call(req, 30.0)
        return {"name": data.get("name") or plugin_name(dll),
                "path": str(dll),
                "params": data.get("params") or [],
                "applied": data.get("applied") or {},
                "latency": data.get("latency"),
                "sec": data.get("sec")}

    def render(self, x: np.ndarray, sr: int, slots: list, label: str = "") -> np.ndarray:
        chain = sanitize_slots(slots)
        active = [s for s in chain if not s["bypass"]]
        if not active:
            return np.asarray(x, dtype=np.float32)
        src = np.asarray(x, dtype=np.float32).reshape(-1)
        a, b = self._paths()
        sf.write(str(a), src, int(sr), format="WAV", subtype="FLOAT")
        dur = len(src) / max(1, int(sr))
        timeout = self.timeout or max(40.0, 12.0 * dur + 20.0)
        req = {"op": "process", "in": str(a), "out": str(b),
               "sr": int(sr),
               "slots": [{"path": s["path"], "params": s["params"]}
                         for s in active]}
        try:
            self.worker.call(req, timeout)
        except VstError as e:
            where = f"轨道「{label}」" if label else ""
            raise VstError(f"{where}的 VST 失败:{e}") from e
        if not b.is_file():
            raise VstError(f"{label or '轨道'}的 VST 没有输出结果")
        y, _ = sf.read(str(b), dtype="float32", always_2d=False)
        y = np.asarray(y, dtype=np.float32).reshape(-1)
        if len(y) != len(src):
            if len(y) < len(src):
                y = np.pad(y, (0, len(src) - len(y)))
            else:
                y = y[:len(src)]
        return np.nan_to_num(y, nan=0.0, posinf=4.0, neginf=-4.0)


def probe() -> dict:
    """宿主可用性:能不能 import pedalboard(worker 一起来就知道)。"""
    w = _Worker()
    try:
        data = w.call({"op": "ping"}, 20.0)
        return {"ok": True, "pedalboard": str(data.get("pedalboard") or "")}
    except VstError as e:
        return {"ok": False, "error": str(e)}
    finally:
        w.kill()


# ---------------------------------------------------------------------------
# 试听专用的常驻宿主(崩了自动重启,只影响试听)
# ---------------------------------------------------------------------------
_aud_host: VstHost | None = None
_aud_lock = threading.RLock()


def audition_render(x: np.ndarray, sr: int, slots: list, label: str = "") -> np.ndarray:
    global _aud_host
    with _aud_lock:
        if _aud_host is None:
            _aud_host = VstHost()
        try:
            return _aud_host.render(x, sr, slots, label)
        except Exception:
            try:
                _aud_host.close()
            except Exception:                  # noqa: BLE001
                pass
            _aud_host = None
            raise


def shutdown() -> None:
    global _aud_host
    with _aud_lock:
        if _aud_host is not None:
            try:
                _aud_host.close()
            except Exception:                  # noqa: BLE001
                pass
            _aud_host = None
