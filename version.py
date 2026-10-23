# -*- coding: utf-8 -*-
"""SingMic 的版本与身份信息 —— 「设置 → 关于」那一页读的就是这里。

★ 只改这一个文件就能改版号 / 开发者 / 联系邮箱,别在网页里写死第二份。
★ CONTACT_EMAIL 留空时,「联系邮箱」那一栏会显示「还没填」——
  填什么、要不要公开,由你自己定(公开仓库里的邮箱全世界都能抓)。
"""

VERSION = "1.0.0"
VERSION_DATE = "2026-10-22"

DEVELOPER = "IMXia-Chan"
COPYRIGHT = "2026"
LICENSE = "GPL-3.0"
LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"

CONTACT_EMAIL = ""   # 显示在「设置 → 联系与反馈」;留空则只显示 GitHub
CONTACT_GITHUB = "https://github.com/IMXia-Chan"

# 第三方组件(NOTICE 里的那份是人看的,这里是网页上那几行)
THIRD_PARTY = [
    {"name": "DeepFilterNet3", "use": "AI 降噪(可选)",
     "license": "MIT", "url": "https://github.com/Rikorose/DeepFilterNet"},
    {"name": "pedalboard", "use": "VST3 离线渲染(可选)",
     "license": "GPL-3.0", "url": "https://github.com/spotify/pedalboard"},
    {"name": "sherpa-onnx", "use": "语音识别 / 唤醒(可选)",
     "license": "Apache-2.0", "url": "https://github.com/k2-fsa/sherpa-onnx"},
]
