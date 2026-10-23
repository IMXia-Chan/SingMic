# 云端补音:训练 + 常驻推理(阿里云 / 腾讯云 GPU)

这份文档是给**你**在云控制台上照着点的。账号、实名、付款、开实例、跑训练都由你动手;
脚本我已经写好,你只要把命令贴上去。

> 先看清楚:**Studio 不需要云端也能补音**(本地颗粒拼接)。云端这一套是让声音更像你。

---

## 0. 先决条件与预算

| 项 | 建议 |
|---|---|
| 实例 | 按量付费 GPU:**T4 16G** 或 **A10 24G**,Ubuntu 22.04 |
| 系统盘 | 50G 够(只放系统) |
| **数据盘** | **100G 云盘,挂到 `/data`,勾选「不随实例释放」** ← 最关键的一步 |
| 训练时长 | 现有 5–6 分钟素材约 1–2 小时;10 分钟以上约 2–4 小时 |
| 训练费用 | 几十块钱量级;训练完**关机**(节省停机模式) |
| 常驻推理 | 先开**CPU 小实例**(2 核 4G)月成本几十到一两百;单段几秒到十几秒。实测慢再换 GPU |
| 预算告警 | 控制台里设「余额低于 X 元短信提醒」,别等账单 |

**不要用抢占式 / 竞价实例** —— 会被回收,训练到一半没了。

---

## 1. 开实例(控制台,你点)

1. 地域选离你近的(如华东 1/华北 2)。
2. 镜像:Ubuntu 22.04 LTS。
3. 系统盘 50G 高效云盘。
4. **数据盘 100G,勾选「不随实例释放」**(阿里云叫这个;腾讯云在「销毁策略」里选「保留云硬盘」)。
5. 安全组:**只开 22**(你自己的 IP)和 **8000**。别开全端口。
   8000 最好也只放你家宽带出口 IP;IP 变了再改,比裸奔强。
6. 记下公网 IP。用 SSH 登录:

```bash
ssh root@<公网IP>
```

### 挂数据盘

```bash
lsblk                      # 找到那块 100G,通常 /dev/vdb
mkfs.ext4 /dev/vdb         # ★ 只在**新盘**上执行;已有数据的盘绝对别 mkfs
mkdir -p /data && mount /dev/vdb /data
echo '/dev/vdb /data ext4 defaults,nofail 0 2' >> /etc/fstab
df -h /data
```

之后**所有**素材、模型、RVC 仓库都放 `/data`。

---

## 2. 装环境

```bash
apt-get update
apt-get install -y python3.10 python3-pip ffmpeg git libsndfile1 unzip

# PyTorch(和驱动匹配;T4/A10 用 cu121 稳)
pip3 install torch==2.1.2 torchaudio==2.1.2 \
  --index-url https://download.pytorch.org/whl/cu121

# RVC(训练 + 推理都靠它)
cd /data
git clone https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI rvc
cd rvc
git log -1 --oneline        # 记下这个版本号,出问题好回退
pip3 install -r requirements.txt
```

★ RVC 的依赖随版本变。如果 `pip install -r requirements.txt` 报错,按它 README 的说明装,
**别用 `--no-deps` 硬跳过**。

```bash
nvidia-smi                  # 能看到 GPU、驱动版本正常
python3 -c "import torch;print(torch.cuda.is_available())"   # 要 True
```

---

## 3. 传素材 → 整理

在**本机**上把录音传上去(或者你直接把文件拖进云厂商的网页终端):

```bash
scp -r <本机项目目录>/recordings root@<公网IP>:/data/raw
```

在云上:

```bash
cd /data
python3 /opt/singmic/prepare_dataset.py --in /data/raw --out /data/dataset
```

- 立体声文件会自动尝试 demucs 分人声;没装 demucs 会**在报告里标出来**,不会静默混进去。
- 输出末尾会告诉你总秒数:
  - < 3 分钟:脚本拒绝训练;
  - 3–8 分钟:只能训基础版;
  - ≥ 8 分钟:比较稳。
- `dataset/meta.json` 里的 `total_sec` 后面打包要填。

素材要求:**安静环境、只有你一个人、别带伴奏**。带伴奏的整曲请先 `separate_vocals.py`
或让 `prepare_dataset.py --demucs on` 分一遍。

---

## 4. 训练

```bash
cd /data/rvc
RVC_DIR=/data/rvc bash /opt/singmic/train.sh /data/dataset myvoice 200
```

- 实验名用英文/数字/下划线,`myvoice` 这种。
- 200 epoch 是起点。听感不够再 300,别一次拉 500(过拟合 + 烧钱)。
- 训练日志里看 loss 是否在降;`logs/myvoice/` 下会出 `G_*.pth`、`D_*.pth`、`added_*.index`。
- 如果 `train.sh` 里某个参数名被 RVC 新版改了:按提示用 WebUI 的训练页(填实验名、
  选 `/data/dataset`、点「一键训练」),效果完全一样。

训练完**先别关机**,马上打包 + 备份。

---

## 5. 打包 + 三份备份(缺一不可)

```bash
cd /data/rvc
python3 /opt/singmic/export_model.py --exp myvoice --out /data/model.zip \
  --materials-sec <meta.json 里的 total_sec> --epochs 200
```

它会把 `model.pth` + `model.index` + `manifest.json` 打进 `/data/model.zip`,
并打印 `version`(模型 sha256 前 16 位)。

然后:

1. **上传到对象存储**(OSS/COS):控制台里建 bucket(私有读),上传 `model.zip`。
2. **下载回本机**:`scp root@<IP>:/data/model.zip <本机项目目录>/voice_model/`
3. **给数据盘打快照**:控制台 → 云盘 → 创建快照(训练完这一次必须打)。

`model.zip` 和 `.singmic/cloud.json` 都**不要进 git**。

---

## 6. 起推理服务

```bash
# 目录与用户
mkdir -p /opt/singmic/assets
cp /data/model.zip /opt/singmic/assets/model.zip
cd /opt/singmic/assets && unzip -o model.zip && ls -l

# token(自己生成一串,别用弱口令)
head -c 24 /dev/urandom | base64
```

建配置(只有 root 能读):

```bash
mkdir -p /etc/singmic
cat > /etc/singmic/voice.env <<'EOF'
SINGMIC_TOKEN=把上面那串粘进来
SINGMIC_MODEL=/opt/singmic/assets/model.pth
SINGMIC_INDEX=/opt/singmic/assets/model.index
SINGMIC_MANIFEST=/opt/singmic/assets/manifest.json
SINGMIC_DEVICE=cuda:0
EOF
chmod 600 /etc/singmic/voice.env
```

装服务:

```bash
cp /opt/singmic/serve.py /opt/singmic/requirements.txt /opt/singmic/
pip3 install -r /opt/singmic/requirements.txt          # fastapi/uvicorn/librosa…
useradd -r -s /usr/sbin/nologin singmic || true
chown -R singmic:singmic /opt/singmic
cp /opt/singmic/systemd/singmic-voice.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now singmic-voice
systemctl status --no-pager singmic-voice
curl -s -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/health
```

`/health` 应该回 `"ok": true` 和 `model_version`。**不通就看日志**:

```bash
journalctl -u singmic-voice -n 100 --no-pager
```

常见原因:模型路径不对、RVC 依赖没装全、`PYTHONPATH` 没指到 `/data/rvc`。

> 如果训练和推理在同一台机器,`serve.py` 需要能 `import infer.modules.vc.modules`,
> 也就是让 `PYTHONPATH` 指到 RVC 仓库根目录(`Environment=PYTHONPATH=/data/rvc`)。
> 如果你想训完就把 GPU 机器关机、另开一台 CPU 小机器常驻,记得把 RVC 仓库和模型一起
> 放 `/data`(数据盘挂到新机器就能直接用)。

---

## 7. Studio 里填一次

Studio 顶部「🎙 我的声音」→ 填:

- 地址:`http://<公网IP>:8000`(有域名/HTTPS 更好;现在没有就先用 IP)
- token:上面那串
- 点「测试连接」→ 应显示「已连接 · 模型 rvc-xxxx · 素材 386 秒」

之后选中一个块 →「补音…」→ 拖长度 → 试听 → 应用。
云端不可达时会自动退回本地补音,面板上会写原因。

---

## 8. 关机、释放与恢复(数据到底会不会丢)

| 操作 | 计算费 | 云盘 | 数据 | 备注 |
|---|---|---|---|---|
| 关机(节省停机 / 关机不收费) | 停 | 保留(继续计费) | **在** | 恢复后开机即可,公网 IP 可能变 |
| 释放实例(**保留数据盘**) | 停 | 保留 | **在** | 新实例挂上同一块盘就能恢复 |
| 释放实例(**连数据盘一起删**) | 停 | 删 | **没了** | 除非有快照 / OSS |
| 包年包月到期不续费 | — | 宽限期后释放 | 可能没 | 记得续费或转按量 |

所以:

- **平时关机不丢数据**,只是补音暂时不可用(Studio 会退回本地)。
- **释放前确认数据盘是「保留」**;不确定就先打快照。
- 最坏情况恢复顺序:新实例 → 挂原数据盘(`/data`) → 起服务;盘也没了就
  从快照建盘;快照也没有就 OSS 下 `model.zip` + 本机 `D:\singmic\voice_model\`。
- 腾讯云销毁实例默认进回收站 15 天;如果选了「立即释放」就真没了。

---

## 9. 费用与安全

- 训练完**立刻关机**;推理实例不用时也关机(Studio 会显示「云端未连接,已用本地补音」)。
- 控制台设预算告警;每周看一眼账单。
- 安全组只开 22/8000;8000 尽量只放自己家 IP。
- token 泄露 = 别人能拿你的 GPU 跑东西。怀疑泄露就换 `voice.env` 里的 token
  并 `systemctl restart singmic-voice`,Studio 里同步改。
- 服务**不保存**用户音频:请求里用临时文件,处理完即删。
- 只把选中片段(≤15 秒)发到这台服务器;Studio 不发工程、不发文件名、不发整首歌。

---

## 10. 排错速查

| 现象 | 先看 |
|---|---|
| `/health` 连不上 | 安全组、服务是否在跑、`journalctl` |
| `/health` 回 `ok:false` | `reason` 字段;多半是模型路径或 RVC 导入失败 |
| Studio 显示「云端不可用」 | 先在服务器上 `curl` 一次 `/health`;再看 Studio 面板的原因 |
| 补音结果不像你 | 素材是否干净、是否只有你一个人;epoch 是否够;A/B 对比本地 |
| 结果有颗粒/断续 | 拉伸倍数太大(>4×);换更长的源片段再补 |
| 显存不够 | 减小 batch(`train.sh` 里 `-bs 8` → `-bs 4`),或换 24G 卡 |
| 训练中断 | 实例被回收(用了抢占式?)、数据盘满、显存 OOM |

---

## 11. 这些脚本的诚实边界

- `serve.py` / `prepare_dataset.py` / `export_model.py` / `separate_vocals.py`
  **都已在真实 RVC + T4 上跑通**(2026-10-20,见第 12 节)。
- **但 demucs 那次的伴奏是合成的**(纯三和弦垫),属于"好分"的极端情况;
  真实编曲里的鼓、失真吉他、混响人声难得多,分离质量必然下降。第 12.5 节的数字
  只证明**管线通、时长对、人声不丢**,不证明它能把任何歌分干净。
- RVC 各版本命令行参数会变;`train.sh` 里的参数以 v2.2 一代为准,`-h` 对不上就用 WebUI。
- 云端重合成是否真的比本地好,**要你 A/B 听**。不达标就保留本地结果,别硬吹。

---

## 12. Docker 实跑记录(2026-10-20,阿里云 T4)

这套流程在这台机上**真跑通了**:训练 200 轮 → 导出 → 起服务 → `/extend` 出音频。

### 12.1 起服务(镜像已在服务器上,叫 `singmic/rvc:2610`)

```bash
# 1) 模型:把 export_model.py 打的包解到数据盘(Docker 只读挂载)
mkdir -p /data/voice_models && cd /data/voice_models
unzip -o /data/export/model.zip && ls -l          # model.pth / model.index / manifest.json

# 2) token:只存服务器,别写进仓库
test -f /data/singmic/token.txt || head -c 32 /dev/urandom | base64 | tr -d '/+=' > /data/singmic/token.txt
chmod 600 /data/singmic/token.txt

# 3) 起容器。★ -w /opt/rvc 不能省:Config 要按当前目录读 ./i18n/locale/en_US.json
docker run -d --name singmic-voice --gpus all --restart unless-stopped \
  -w /opt/rvc -e PYTHONPATH=/opt/rvc:/opt/singmic \
  -p 127.0.0.1:8000:8000 \
  -e SINGMIC_TOKEN="$(cat /data/singmic/token.txt)" \
  -v /data/rvc_assets:/opt/rvc/assets:ro \
  -v /data/voice_models:/opt/singmic/assets:ro \
  singmic/rvc:2610

# 4) 验
curl -s http://127.0.0.1:8000/health
```

`/health` 要回 `{"ok":true, "model_version":"rvc-…", "engine":"rvc-v2", "device":"cuda:0"}`。

### 12.2 不给公网开 8000:走 SSH 隧道

容器**只绑** `127.0.0.1:8000`,安全组不用改。Studio 所在机器上开一条隧道:

```bash
ssh -N -o ServerAliveInterval=30 -i ~/.ssh/你的密钥.pem \
    -L 127.0.0.1:8000:127.0.0.1:8000 root@<服务器IP>
```

Studio 里 **🎙 我的声音** 地址填 `http://127.0.0.1:8000`,token 填服务器上那串。
**隧道一断 Studio 就自动回落本地补音**,不会卡住。

### 12.3 实测数字

| 项 | 结果 |
|---|---|
| 训练 | 200 轮 / 素材 700 秒 / **1 小时 11 分**(T4,约 21 秒一轮) |
| 模型 | `model.pth` 57.6 MB + `model.index` 109 MB,版本 `rvc-1904dfdccbb59729` |
| `/extend` 冷启动首段 | 3 秒 → 6 秒:**29.0 秒**(含模型/RMVPE 加载) |
| `/extend` 热启动 | 同一段:**0.69 秒** |
| 显存占用 | 约 **1.6 GB**(T4 共 15 GB) |

### 12.4 demucs 人声分离(2026-10-20 实跑)

做法:取训练集一段 64.7 秒干净人声,人工叠一条合成伴奏(大三和弦 + 低音 + 起伏包络)
做成立体声,再喂给 `separate_vocals.py`,拿分离结果和原始干净人声对比。

```bash
docker cp /data/demucs_test/. singmic-voice:/work/
docker exec singmic-voice python /work/separate_vocals.py --in /work/mix.wav --out /work/out
```

| 指标 | 结果 |
|---|---|
| SI-SDR 分离前(混音 vs 干净人声) | 3.50 dB |
| SI-SDR 分离后 | **20.97 dB**(+17.5 dB) |
| 分离人声 ↔ 干净人声 相关 | 0.996 |
| 分离人声 ↔ 伴奏 相关 | -0.000 |
| 速度 | 70 秒音频 **5 秒**分完(T4);htdemucs 权重首次下载 80.2 MB / 41 秒 |

★ 这是**最好情况**:合成伴奏是稳态纯音,真实歌曲难得多,别拿这组数字当承诺。
★ 权重缓存在容器内 `~/.cache/torch/hub/checkpoints/`,容器一删就要重下 80 MB。

### 12.5 还没做的

- 常驻服务的成本:实例开着就计费,不用时 `docker stop singmic-voice` 或直接关机;
  数据在数据盘上,不在容器里。
- 公网直连(不隧道)要把 8000 只放你家出口 IP,并且最好挂 HTTPS;现在先用隧道。
