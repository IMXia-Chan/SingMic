#!/usr/bin/env bash
# 分段训练 + 断点续训:把 200 轮拆成若干段,每段结束都能从上次的 G_*.pth 接着练。
#
# 和 train.sh 的区别:
#   train.sh    每次都会删掉中间产物(0_gt_wavs/2a_f0/3_feature768)重新做,
#               适合一次练完;机器中途停了就白丢 6–8 分钟预处理。
#   train_chunks 预处理只做一次(已有特征就跳过),训练按段推进,
#               每段用上一段的权重续训,配合本机备份可以硬扛机器被回收。
#
# 用法:
#   ./train_chunks.sh <dataset目录> <实验名> [总轮数] [每段轮数]
#   例:./train_chunks.sh /root/singmic/dataset myvoice 200 50
#
# 环境变量同 train.sh(RVC_DIR / PY)。
set -euo pipefail

DATASET="${1:?用法: $0 <dataset目录> <实验名> [总轮数] [每段轮数]}"
EXP="${2:?缺实验名}"
TOTAL="${3:-200}"
CHUNK="${4:-50}"
RVC_DIR="${RVC_DIR:-/opt/rvc}"
PY="${PY:-python}"

cd "$RVC_DIR"
EXP_DIR="logs/$EXP"
mkdir -p "$EXP_DIR"
[[ -f "$EXP_DIR/config.json" ]] || cp configs/v2/48k.json "$EXP_DIR/config.json"

for f in assets/pretrained_v2/f0G48k.pth assets/pretrained_v2/f0D48k.pth assets/rmvpe/rmvpe.pt; do
  [[ -f "$RVC_DIR/$f" ]] || { echo "缺预训练权重: $RVC_DIR/$f" >&2; exit 2; }
done
[[ -f "$RVC_DIR/assets/hubert_base/config.json" ]] || {
  echo "缺 HuBERT: $RVC_DIR/assets/hubert_base/(config.json + pytorch_model.bin)" >&2; exit 2; }

run() { echo; echo "\$ $*"; "$@"; }

# ---------- 1 预处理:只在缺特征时做 ----------
if [[ ! -d "$EXP_DIR/3_feature768" || -z "$(ls -A "$EXP_DIR/3_feature768" 2>/dev/null)" ]]; then
  echo "== 预处理(切片 / f0 / HuBERT)=="
  rm -rf "$EXP_DIR"/0_gt_wavs "$EXP_DIR"/1_16k_wavs "$EXP_DIR"/2a_f0 \
         "$EXP_DIR"/2b-f0nsf "$EXP_DIR"/3_feature768
  run "$PY" -m train.preprocess "$DATASET" 48000 4 "$EXP_DIR" False 3.7
  run "$PY" -m train.dataset.extract_f0 cuda 1 0 0 "$EXP_DIR" True
  run "$PY" -m train.dataset.extract_hubert_feature cuda 1 0 0 "$EXP_DIR" v2 True
else
  echo "== 预处理已存在,跳过($(ls -1 "$EXP_DIR/3_feature768" | wc -l) 条特征)=="
fi

# ---------- 2 filelist ----------
"$PY" - "$EXP_DIR" <<'PY'
import os, sys, pathlib
exp = pathlib.Path(sys.argv[1]).resolve()
gt, fe = exp / "0_gt_wavs", exp / "3_feature768"
f0, f0nsf = exp / "2a_f0", exp / "2b-f0nsf"
keys = lambda d: {n.split(".")[0] for n in os.listdir(d)}
names = sorted(keys(gt) & keys(fe) & keys(f0) & keys(f0nsf))
if not names:
    sys.exit("filelist: 四条产物没有交集,特征没提全")
(exp / "filelist.txt").write_text(
    "\n".join("%s/%s.wav|%s/%s.npy|%s/%s.wav.npy|%s/%s.wav.npy|0"
              % (gt, n, fe, n, f0, n, f0nsf, n) for n in names), encoding="utf8")
print("filelist: %d 条" % len(names))
PY

# ---------- 3 找最新检查点 ----------
latest() {   # latest G 或 D:按文件名里的数字取最大
  local pat="$1" best="" bestn=-1 n
  for f in "$EXP_DIR"/$pat; do
    [[ -f "$f" ]] || continue
    n=$(basename "$f" | grep -oE '[0-9]+' | head -1)
    n=${n:-0}
    (( n > bestn )) && { bestn=$n; best="$f"; }
  done
  echo "$best"
}

CUR_G=$(latest 'G_*.pth'); CUR_D=$(latest 'D_*.pth')
# ★ RVC 的周期存档固定叫 G_2333333.pth / D_2333333.pth,文件名里的数字**不是轮数**。
#   所以从外部拿来的检查点要用 START_EPOCH 明确告诉脚本「这是第几轮的」,
#   脚本会把它复制成 G_<轮数>.pth —— train.py 正是靠文件名认起始轮。
START_EPOCH="${START_EPOCH:-}"
if [[ -n "$START_EPOCH" ]]; then
  [[ -n "${RESUME_G:-}" && -n "${RESUME_D:-}" ]] || {
    echo "给了 START_EPOCH 就必须同时给 RESUME_G 和 RESUME_D(路径)" >&2; exit 2; }
  cp -f "$RESUME_G" "$EXP_DIR/G_$START_EPOCH.pth"
  cp -f "$RESUME_D" "$EXP_DIR/D_$START_EPOCH.pth"
  DONE="$START_EPOCH"
  PG="$EXP_DIR/G_$START_EPOCH.pth"; PD="$EXP_DIR/D_$START_EPOCH.pth"
  echo "== 用外部检查点续训:第 $DONE 轮 =="
elif [[ -n "$CUR_G" && -n "$CUR_D" ]]; then
  DONE=$(basename "$CUR_G" | grep -oE '[0-9]+' | head -1); DONE=${DONE:-0}
  # 名字里的数字大于总轮数 ⇒ 那是 2333333 这种占位名,只能按 0 处理,别当成进度
  (( DONE > TOTAL )) && DONE=0
  PG="$CUR_G"; PD="$CUR_D"; echo "== 从第 $DONE 轮续训 =="
else
  DONE=0; PG=assets/pretrained_v2/f0G48k.pth; PD=assets/pretrained_v2/f0D48k.pth
  echo "== 从零开始(用官方预训练权重)=="
fi

# ---------- 4 分段训练 ----------
while (( DONE < TOTAL )); do
  NEXT=$(( DONE + CHUNK )); (( NEXT > TOTAL )) && NEXT=$TOTAL
  echo
  echo "==== 第 $((DONE+1)) → $NEXT 轮 ===="
  run "$PY" -m train.train \
    -e "$EXP" -sr 48k -f0 1 -bs 8 -g 0 -te "$NEXT" -se 10 \
    -pg "$PG" -pd "$PD" -l 1 -c 0 -sw 1 -v v2
  DONE=$NEXT
  CUR_G=$(latest 'G_*.pth'); CUR_D=$(latest 'D_*.pth')
  [[ -n "$CUR_G" && -n "$CUR_D" ]] && { PG="$CUR_G"; PD="$CUR_D"; }
  echo "---- 第 $DONE 轮完成,检查点: $(basename "$PG") // $(basename "$PD") ----"
done

# ---------- 5 检索索引 ----------
run "$PY" -m train.train_index "$EXP" v2 "$EXP_DIR" 4 || \
  echo "★ 索引训练失败(不影响模型本体,serve.py 可以不带 index 跑)"

echo
echo "训练结束。检查点:"
ls -1 "$EXP_DIR"/G_*.pth "$EXP_DIR"/D_*.pth "$EXP_DIR"/added_*.index 2>/dev/null | tail -6
