# 补音推理 + 训练镜像(RVC v2 新版工具链)
#
# ★ 2026-10 起 RVC 仓库改版:依赖文件叫 `requirments_cu118_py312.txt`(原仓库拼写如此),
#   跑 Python 3.12 + torch 2.7.1;老教程里的 `requirements.txt` / fairseq 那一套已经不适用。
#   所以这里钉死 tag `2.3.260718`,不跟 main 漂移。
# ★ torch 走 PyPI 源(PyPI 上的 linux wheel 自带 CUDA 12.6),不额外拉 CUDA 基础镜像 ——
#   nvidia-container-toolkit 会把宿主驱动挂进来,T4(驱动 580)能直接用。
# ★ Ubuntu 24.04 的 pip 是 externally-managed,所以全程用 /opt/venv,不碰系统 python。
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/rvc \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3-pip \
        ffmpeg git libsndfile1 ca-certificates curl \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python3.12 -m venv /opt/venv \
    && pip install --no-cache-dir -U pip wheel

# 国内直连:RVC 依赖文件里写死的 pku 源在构建时经常超时,构建里 sed 掉。
# ★ 源速实测(同一台机):
#   USTC 23.9MB/s、腾讯云 17.1、华为云 10.6、清华 2.0、阿里云 0.10。
#   阿里云对 pip 的下载限得离谱,别用;默认走 USTC。
ARG PIP_INDEX=https://mirrors.ustc.edu.cn/pypi/simple
# ★ RVC 那份依赖文件是**按 CUDA 11.8 验过的**:它钉死 nvidia-cudnn-cu11==8.9.5.29
#   这类 cu11 DLL(onnxruntime-gpu 1.18 要 cuDNN 8,和 cu12 的 torch 不同代)。
#   USTC 上没有 8.9.5.29(只有 .30),所以留 pku 做兜底索引。
ARG PIP_EXTRA=https://mirrors.pku.edu.cn/pypi/simple
# ★ torch 也用 cu118 版(不用 PyPI 上那个 cu12 轮子),和依赖文件对齐;
#   南大镜像实测 20.7MB/s。cu118 轮子自带 CUDA 运行库,不依赖 nvidia-* 包。
ARG PT_INDEX=https://mirrors.nju.edu.cn/pytorch/whl/cu118

ARG RVC_REPO=https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI
ARG RVC_REF=2.3.260718
RUN git clone --depth 1 --branch "${RVC_REF}" "${RVC_REPO}" /opt/rvc

# torch 与 torchaudio 成对装(2.7.1+cu118,满足 RVC「torch 必须 < 2.8」的约束)
RUN pip install --no-cache-dir "torch==2.7.1+cu118" "torchaudio==2.7.1+cu118" \
        --index-url "$PT_INDEX"

# RVC 自己那份依赖(文件首行的 --index-url 删掉,统一走 PIP_INDEX + PIP_EXTRA)
# ★ 还要删掉 `nvidia-cudnn-cu11==8.9.5.29` 这一行:那是 RVC 为 **Windows 便携包**
#   钉的 cuDNN8(给 onnxruntime-gpu 1.18 用),和 Linux 上 torch 2.7.1+cu118 自带的
#   cuDNN 9.1.0.70 冲突 —— 装上它 torch 找不到 libcudnn.so.9,卷积直接崩。
#   代码里 onnxruntime 只在 infer/rmvpe.py 的 onnx 导出分支里 import,训练用不到。
RUN sed -i '/^--index-url/d; /^nvidia-cudnn-cu11==/d' /opt/rvc/requirments_cu118_py312.txt \
    && pip install --no-cache-dir -r /opt/rvc/requirments_cu118_py312.txt \
         -i "$PIP_INDEX" --extra-index-url "$PIP_EXTRA"

# 素材分离伴奏用 demucs(和 RVC 共用同一套 torch)
RUN pip install --no-cache-dir "demucs==4.0.1" -i "$PIP_INDEX"

COPY serve.py /opt/singmic/serve.py
# ★ 工作目录必须是 RVC 仓库根:Config 按**当前目录**读 ./i18n/locale/en_US.json,
#   在 /opt/singmic 起会 FileNotFoundError(2026-10-20 实跑踩过)。
#   serve.py 本身靠 PYTHONPATH 找得到。
ENV PYTHONPATH=/opt/rvc:/opt/singmic
WORKDIR /opt/rvc

# 模型与清单不打进镜像:挂载进来,换模型不用重建镜像
#   -v /data/rvc_assets:/opt/rvc/assets:ro        (hubert / rmvpe / pretrained_v2)
#   -v /data/voice_models:/opt/singmic/assets:ro  (自己训出来的 model.pth / model.index)
ENV SINGMIC_MODEL=/opt/singmic/assets/model.pth \
    SINGMIC_INDEX=/opt/singmic/assets/model.index \
    SINGMIC_MANIFEST=/opt/singmic/assets/manifest.json \
    SINGMIC_DEVICE=cuda:0

EXPOSE 8000
CMD ["uvicorn", "serve:app", "--host", "0.0.0.0", "--port", "8000"]
