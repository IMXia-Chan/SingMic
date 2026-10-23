#!/usr/bin/env bash
# 在云 GPU 上训 RVC v2 音色模型(适配 RVC tag 2.3.260718 —— 老教程的
# `infer/modules/train/...` 路径和 fairseq 那一套在这个版本里已经没有了)。
#
# 用法:
#   ./train.sh <dataset目录> <实验名> [epochs]
# 环境变量:
#   RVC_DIR  默认 /opt/rvc(镜像里的克隆位置)
#   PY       默认 python(镜像里是 /opt/venv/bin/python)
#
# 实际执行顺序(= RVC 网页「一键训练」背后的四步):
#   preprocess → 训练集切片(48k)
#   extract_f0 → RMVPE 取音高
#   extract_hubert_feature → HuBERT 特征
#   train → 训练;train_index → 检索索引
set -euo pipefail

DATASET="${1:-}"
EXP="${2:-}"
EPOCHS="${3:-200}"
RVC_DIR="${RVC_DIR:-/opt/rvc}"
PY="${PY:-python}"

if [[ -z "$DATASET" || -z "$EXP" ]]; then
  echo "用法: $0 <dataset目录> <实验名> [epochs]" >&2
  exit 2
fi
if [[ ! -d "$DATASET" ]]; then
  echo "没有这个目录: $DATASET" >&2
  exit 2
fi
if [[ ! -f "$RVC_DIR/train/train.py" ]]; then
  echo "RVC 没装好: $RVC_DIR/train/train.py 不存在(tag 2.3.260718)" >&2
  exit 2
fi
for f in assets/pretrained_v2/f0G48k.pth assets/pretrained_v2/f0D48k.pth \
         assets/rmvpe/rmvpe.pt; do
  if [[ ! -f "$RVC_DIR/$f" ]]; then
    echo "缺预训练权重: $RVC_DIR/$f(把 /data/rvc_assets 挂到 /opt/rvc/assets)" >&2
    exit 2
  fi
done
# ★ 2.3.260718 起 HuBERT 换成了 Transformers 目录形式(assets/hubert_base/),
#   不再是老的 assets/hubert/hubert_base.pt;缺 config.json 会在特征提取阶段才报错。
if [[ ! -f "$RVC_DIR/assets/hubert_base/config.json" ]]; then
  echo "缺 HuBERT: $RVC_DIR/assets/hubert_base/config.json(+ pytorch_model.bin)" >&2
  exit 2
fi

N=$(find "$DATASET" -maxdepth 1 -name '*.wav' | wc -l | tr -d ' ')
if [[ "$N" -lt 10 ]]; then
  echo "★ 只有 $N 段素材,太少了;先跑 prepare_dataset.py 并多录一些。" >&2
fi
SECS=$("$PY" - "$DATASET" <<'PY'
import sys, pathlib, soundfile as sf
d = pathlib.Path(sys.argv[1]); t = 0.0
for p in d.glob("*.wav"):
    try: t += sf.info(str(p)).duration
    except Exception: pass
print(int(t))
PY
)
echo "素材: $N 段 / ${SECS} 秒 ($(( SECS / 60 )) 分钟)"
if [[ "$SECS" -lt 180 ]]; then
  echo "★ 不足 3 分钟,拒绝训练 —— 训出来不像你。" >&2
  exit 2
fi

cd "$RVC_DIR"
EXP_DIR="logs/$EXP"
mkdir -p "$EXP_DIR"
if [[ ! -f "$EXP_DIR/config.json" ]]; then
  cp configs/v2/48k.json "$EXP_DIR/config.json"
fi

command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.total \
  --format=csv,noheader || echo "★ 没看到 GPU;CPU 训练会慢到没有意义。"

run() { echo; echo "\$ $*"; "$@"; }

# 0) 清掉上一次实验的中间产物(特征/wav 片段),避免新旧样本混在一起
rm -rf "$EXP_DIR"/0_gt_wavs "$EXP_DIR"/1_16k_wavs "$EXP_DIR"/2a_f0 \
       "$EXP_DIR"/2b-f0nsf "$EXP_DIR"/3_feature768

# 1) 切片 + 重采样
# ★ 必须用 `-m`(模块方式)跑: `python train/preprocess.py` 时 sys.path[0] 是
#   train/ 目录,`import train` 会解析成 train/train.py 这个**文件**,和
#   train/ 这个**包**打架,直接 ImportError(circular import)。用 -m 时
#   sys.path[0]=当前目录,解析到命名空间包才对。
run "$PY" -m train.preprocess "$DATASET" 48000 4 "$EXP_DIR" False 3.7

# 2) 取音高(RMVPE —— 比 pm 准,人声颤音/滑音更稳)
run "$PY" -m train.dataset.extract_f0 cuda 1 0 0 "$EXP_DIR" True

# 3) HuBERT 特征
run "$PY" -m train.dataset.extract_hubert_feature cuda 1 0 0 "$EXP_DIR" v2 True

# 3.5) 生成 filelist.txt
# ★ 这一步只有网页界面(webui.py:1272)会做,命令行跑四个脚本时没人写它,
#   而 train.py 第一件事就是读 logs/$EXP/filelist.txt —— 不生成就 FileNotFoundError。
#   格式:gt_wav|feature.npy|f0.npy|f0nsf.npy|speaker_id(单说话人=0)
echo; echo "\$ 生成 filelist.txt"
"$PY" - "$EXP_DIR" <<'PY'
import os, sys, pathlib
exp = pathlib.Path(sys.argv[1]).resolve()
gt, fe = exp / "0_gt_wavs", exp / "3_feature768"
f0, f0nsf = exp / "2a_f0", exp / "2b-f0nsf"
keys = lambda d: {n.split(".")[0] for n in os.listdir(d)}
names = sorted(keys(gt) & keys(fe) & keys(f0) & keys(f0nsf))
if not names:
    sys.exit("filelist: 四条产物没有交集,特征没提全")
lines = ["%s/%s.wav|%s/%s.npy|%s/%s.wav.npy|%s/%s.wav.npy|0"
         % (gt, n, fe, n, f0, n, f0nsf, n) for n in names]
(exp / "filelist.txt").write_text("\n".join(lines), encoding="utf8")
print("filelist: %d 条" % len(lines))
PY

# 4) 训练
run "$PY" -m train.train \
  -e "$EXP" -sr 48k -f0 1 -bs 8 -g 0 -te "$EPOCHS" -se 10 \
  -pg assets/pretrained_v2/f0G48k.pth -pd assets/pretrained_v2/f0D48k.pth \
  -l 1 -c 0 -sw 1 -v v2

# 5) 检索索引(素材少也能建,检索弱一点但没坏处)
run "$PY" -m train.train_index "$EXP" v2 "$EXP_DIR" 4 || \
  echo "★ 索引训练失败(不影响模型本体,serve.py 可以不带 index 跑)"

echo
echo "训练结束。下一步:"
echo "  $PY $(dirname "$0")/export_model.py --exp \"$EXP\" --out ~/model.zip --materials-sec $SECS --epochs $EPOCHS"
echo "然后:上传 model.zip 到对象存储 + 下载回本机 + 给数据盘打快照。"
