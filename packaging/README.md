# 上架 Microsoft Store

这份文档只做一件事：把「代码」变成「商店能收的包」，并列出哪些步骤必须由你本人在商店后台完成。

---

## 一、三条命令（一个产品，全部功能）

```bat
python packaging\make_icons.py    :: 生成图标资源（改过图标才需要重跑）
python tools\build_app.py         :: 打 exe → dist\SingMic\
python packaging\make_msix.py     :: MSIX → packaging\out\
```

商店里只有**一个产品**「声麦 Studio Pro」，所有功能都开，免费发布。
包里会写一份 `edition.txt`（内容 `pro`），只是把「全功能」这个事实落到盘上 ——
`edition.py` 认不出来时也默认全功能，所以写不写都不影响结果。

`make_msix.py` 第一次运行会因为没有 `packaging\identity.json` 停下，并写出模板。填好再跑。

本机没有装 Windows SDK 也能打包：脚本会自己从 NuGet 取 `Microsoft.Windows.SDK.BuildTools`，
解出 `makeappx.exe` 放到 `packaging\.sdk\`（这个目录不进仓库）。

本机试装（不签名也能装，需要开发者模式）：

```powershell
python packaging\make_msix.py --layout-only
Add-AppxPackage -Register "$PWD\packaging\layout\AppxManifest.xml"
```

卸载：`Get-AppxPackage *SingMic* | Remove-AppxPackage`。
★ 只做 layout-only 的话，`edition.txt` 是准的；但 `Add-AppxPackage` 走的是开发身份，
商店功能（若将来接）不会生效，那必须是从商店安装的包。

---

## 二、必须由你本人做的事（我碰不到）

### 1. 开发者账号（一次性，需要付费）

- 地址：<https://partner.microsoft.com/dashboard/registration>
- 个人开发者注册费约 ¥120（一次性），需要实名与支付方式。
- 用哪个账号注册、是否实名到个人，由你决定；注册完成后把账号绑定的信息告诉我（**不要把密码给我**）。

### 2. 预留产品名称

- 后台 →「新建产品」→「MSIX 或 PWA 应用」→ 预留名称 `声麦 Studio Pro`。
- **只建这一个产品**。全部功能都在里面，不另外建 Pro（2026-10-23 决定免费发布，不分版）。
- 名称被占用时后台会给替代建议；换名字只影响对应身份文件的 `displayName` 与商店文案，代码不用改。

### 3. 抄「产品标识」两样东西

后台 → 你的应用 →「产品管理」→「产品标识」：

| 后台字段 | 填到 `packaging/identity.json` 的 |
|---|---|
| 包名（Package/Identity/Name），形如 `1234567890ABCDE.SingMicStudio` | `identityName` |
| 发行商（Package/Identity/Publisher），形如 `CN=8F3C1A2B-…` | `publisher` |

**必须一字不差**（包括 `CN=` 和大小写）。对不上，商店会在上传阶段直接拒包。

身份文件当前的内容（已按 Partner Center 抄好）：

```json
{
  "identityName": "B438DEB3.StudioPro",
  "publisher": "CN=7A708E1A-4DDF-40BA-9672-5368EBE6ABA3",
  "publisherDisplayName": "夏蝉是秋风的前奏",   // ★ 公开可见
  "displayName": "声麦 Studio Pro",
  "description": "录音、修音、编曲、混音与导出，全部在本机完成的一体化音频工作站。"
}
```

`identity.dev.json` 保持原样，它只用于本机验证打包流程。

### 4. 联系邮箱

`version.py` 里的 `CONTACT_EMAIL` 现在是空的，「设置 → 联系与反馈」会显示「还没填」。
商店的隐私政策页面与支持页面都要填一个能收到信的邮箱 —— **填什么由你定**（公开仓库里的邮箱全世界都能抓）。

### 5. 隐私政策网址

商店要求一个可访问的 URL。`packaging/store/privacy-policy.zh-CN.md` 与 `privacy-policy.en-US.md` 是成稿，
建议放进公开仓库后开 GitHub Pages（Settings → Pages → 从分支发布），或放到自己的站点，拿到链接填进后台。

**注意**：政策里写着「不收集、不遥测、录音不上传」，这是当前实现的真实情况。
如果以后加了任何统计或云端默认开启的功能，必须同步改这份政策，否则属于虚假陈述。

### 6. 截图

见 `store/listing.md` 末尾的表格。至少 1 张，建议 6 张，1366×768 以上。

### 7. 提交时后台要填的表（一个产品，填一遍）

| 页面 | 填什么 |
|---|---|
| 定价与可用性 | 免费；市场建议先选中国 + 美国 + 日本 + 韩国 |
| 属性 | 分类：音乐 → 音乐；系统要求照抄 `store/listing.md` |
| 年龄分级 | 问卷全部选「否」，预期结果 3+ |
| 声明 | 无障碍声明按实际填；「是否使用用户生成内容」否 |
| 程序包 | 上传 `packaging\out\StudioPro_1.0.0.0_x64.msix`（**不需要自己签名，商店会重签**） |
| 商店列表 | 照抄 `store/listing.md` |

★ 不收费，所以「付款与税务」整块不用填 —— 个人账号在中国区建付款资料有失败案例，正好绕开。

### 8. 提审

点「提交审核」后通常 1–3 个工作日。被驳回时后台会给具体条目，把原文贴给我。

---

## 三、打包这块的技术事实（免得下次重新摸）

- **清单**：`Windows.FullTrustApplication` + `runFullTrust`。这个包就是个普通桌面程序：
  自己起本地 HTTP 服务、自己开 WebView2 窗口，所以不需要任何网络或文件系统能力声明。
- **资源**：`Square44` 需要 `targetsize-*` 全套（任务栏按像素高挑图，缺了会拿 100% 那张硬缩，很糊）。
  `make_icons.py` 每一档都是**按该尺寸重新绘制**，不是拿大图缩。
- **版本号**：清单里必须是四段（`1.0.0` → `1.0.0.0`），脚本自动补齐；
  商店要求新提交的版本号**严格大于**上一次，改 `version.py` 的 `VERSION` 即可。
- **数据目录**：装到 `Program Files` 后安装目录只读，exe 会把数据放到
  `%LOCALAPPDATA%\SingMic\data`（`desktop.py` 里设 `SINGMIC_DATA`）。卸载不删这个目录。
  设置与密钥（`%USERPROFILE%\.singmic\`）同在这台用户下。
- **单实例**：命名互斥量 `SingMic.Studio.Desktop.v1`，双击第二次会提示已在运行。
- **功能开关**：`edition.py` 先看环境变量 `SINGMIC_EDITION`，再看 exe 同目录的 `edition.txt`，
  都没有一律**全功能**。发布包写的也是 `pro`。这一层只影响接口放不放行，
  **不改产品名、不改数据目录** —— 商店里就一个「声麦 Studio Pro」。
- **WebView2**：Windows 11 自带；Windows 10 一般随 Edge 已装。缺失时程序会提示下载地址并退出，
  不会只给一个白窗口。
- **体积**：`dist\SingMic` 约 190 MB，打包后 MSIX 大致同量级。商店对 MSIX 没有硬性上限，
  但审核会看首次启动时间；PyInstaller 是 onedir，冷启动约 2–4 秒。
- **不进仓库**：`packaging\.sdk\`、`packaging\layout\`、`packaging\out\` 都在 `.gitignore` 里。

---

## 四、本机服务的安全边界

程序只监听 `127.0.0.1`，局域网内的设备无法访问。但「监听本机」不等于「只接受本机页面的请求」，
所以 `server.py` 另加了一道护栏（`_request_allowed`）：

| 检查 | 挡住什么 |
|---|---|
| `Host` 必须是 `127.0.0.1:端口` 或 `localhost:端口` | **DNS rebinding**：攻击者域名先解析到自己服务器，再改解析到 `127.0.0.1`，借此绕过同源策略读写本机接口。浏览器发请求时 `Host` 头跟着 URL 走，仍是原域名，因此被拒。 |
| 带 `Origin` 的请求必须来自本机页面 | **跨站直连**：别的网页直接 `fetch('http://127.0.0.1:8765/api/…')`。响应读不到（未开 CORS），但 `text/plain` 的 POST 不触发预检，副作用仍会发生。 |
| 两条 WebSocket 同样校验 | WebSocket 不受 CORS 约束，任何网页都能连接 `/ws/mic`、`/ws/ui`。 |

没有引入随机 token：本机同用户的其他进程本来就能直接读取文件，token 只能防浏览器，
而上述两条已经封住浏览器路径；加 token 需要改动前端数十处 `fetch`，回归风险高于收益。
如果将来增加对外监听（例如让手机通过局域网而非 USB 隧道接入），必须重新评估这一决定。

`tools/smoke_studio.py` 有 7 条用例固定验证这道护栏（外站 `Host` / 外站 `Origin` /
`Origin: null` / 两条 WebSocket 握手），改动相关代码后请照常运行冒烟。

