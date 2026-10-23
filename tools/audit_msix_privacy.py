# -*- coding: utf-8 -*-
r"""MSIX 隐私自检 —— 回答一个问题:这个即将公开下载的安装包里,有没有开发者本机的私人数据。

为什么必须按字节扫内容:
  · PyInstaller 会把源码路径写进 .pyc 的 co_filename;
  · 第三方 Rust/C++ 扩展会把构建机路径硬编进 .pyd 和 .dll;
  · 只按文件名看是查不出来的。

检查分两级。

  硬性问题(FAIL,退出码 1)—— 命中任何一条都不该提交:
    · 包内有音频文件本体(.wav/.mp3/.flac/.m4a/.ogg/.aac)
    · 包内有模型文件本体(.pth/.index/.onnx/.safetensors/.ckpt)
    · 包内有日志文件(.log)
    · 条目路径里出现 projects\ / recordings\ / voice_model\(用户工程与录音)
    · 任何文件内容里出现 C:\\Users\\<本机用户名>
    · 任何文件内容里出现本仓库的绝对路径(含去掉 -open 后的正式目录)
    · 指定 --email 时,包里出现该邮箱

  仅供参考(INFO,不影响退出码)—— 打印出来人工确认:
    · 别人的 C:\\Users\\... 路径(通常是第三方库自带的构建机路径)
    · recordings / projects 字样(多为程序自己的路由名)
    · 包里出现的全部邮箱(几乎都是第三方库的版权声明)

跑法:
    D:\\AI\\python.exe D:\\singmic-open\\tools\\audit_msix_privacy.py
    D:\\AI\\python.exe D:\\singmic-open\\tools\\audit_msix_privacy.py packaging\\out\\StudioPro_1.0.0.0_x64.msix
    D:\\AI\\python.exe D:\\singmic-open\\tools\\audit_msix_privacy.py --email someone@example.com

不给包路径时,自动取 packaging\\out\\ 下最新的 .msix。
"""
from __future__ import annotations

import argparse
import collections
import getpass
import glob
import os
import re
import sys
import zipfile

AUDIO_EXT = {'.wav', '.mp3', '.flac', '.m4a', '.ogg', '.aac', '.aiff', '.aif', '.opus', '.wma'}
MODEL_EXT = {'.pth', '.index', '.onnx', '.safetensors', '.ckpt', '.pt', '.bin'}
BAD_EXT = AUDIO_EXT | MODEL_EXT | {'.log'}

# 条目路径里不该出现的目录(用户数据)
BAD_DIRS = ('/projects/', '/recordings/', '/voice_model/')

MAX_SHOW = 6


def find_package(explicit: str | None) -> str:
    if explicit:
        p = os.path.abspath(explicit)
        if not os.path.isfile(p):
            sys.exit(f'找不到这个文件: {p}')
        return p
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = sorted(glob.glob(os.path.join(repo, 'packaging', 'out', '*.msix')), key=os.path.getmtime)
    if not hits:
        sys.exit(f'packaging\\out\\ 下没有 .msix,请显式指定包路径。\n({repo})')
    return hits[-1]


def path_regex(path: str) -> re.Pattern[bytes]:
    """把一个 Windows 路径编译成匹配正则。

    分隔符一律用 [\\\\/]{1,2}:同一串路径在二进制里可能是
    `D:\\singmic`、`D:/singmic`、JSON 转义后的 `D:\\\\singmic`。
    ★ 必须 re.I —— 源码里写的是 `C:\\Users`,运行时拼出来是 `C:\\Users`,
      而磁盘上、日志里、错误信息里大小写并不固定。区分大小写会漏掉真问题。
    """
    parts = [re.escape(p.encode('utf-8', 'ignore'))
             for p in re.split(r'[\\/]+', path.strip()) if p]
    return re.compile(rb'[\\/]{1,2}'.join(parts), re.I)


def main() -> int:
    ap = argparse.ArgumentParser(description='MSIX 隐私自检')
    ap.add_argument('package', nargs='?', help='MSIX 路径;不给则取 packaging\\out\\ 下最新的')
    ap.add_argument('--user', help='要搜的本机用户名;默认取 USERPROFILE 的最后一段')
    ap.add_argument('--email', action='append', default=[], help='必须不存在于包内的邮箱(可重复)')
    args = ap.parse_args()

    pkg = find_package(args.package)
    user = args.user or (os.environ.get('USERNAME')
                         or getpass.getuser()
                         or os.path.basename(os.environ.get('USERPROFILE', '')))
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 正式目录(去掉 -open 后缀)同样不能出现
    roots = [repo]
    if repo.endswith('-open'):
        roots.append(repo[:-5])

    print(f'包   : {pkg}')
    print(f'大小 : {os.path.getsize(pkg) / 1048576:.1f} MB')
    print(f'用户名: {user}')
    print(f'仓库根: {repo}')
    print()

    user_rx = path_regex('C:\\Users\\' + user)
    root_rxs = [path_regex(r) for r in roots]
    email_rx = re.compile(rb'[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]{2,}\.[A-Za-z]{2,}')

    fails: list[tuple[str, str]] = []
    notes: collections.OrderedDict[str, set[str]] = collections.OrderedDict(
        [('别人的 C:\\Users 路径', set()),
         ('recordings / projects 字样', set())])
    emails: set[str] = set()

    z = zipfile.ZipFile(pkg)
    names = z.namelist()
    total = 0
    other_user_rx = re.compile(rb'c:{1,2}[\\/]{1,2}users[\\/]{1,2}([A-Za-z0-9._-]{1,40})', re.I)
    word_rx = re.compile(rb'recordings|projects[\\/]', re.I)

    for n in names:
        low = n.lower()
        ext = os.path.splitext(low)[1]
        if ext in BAD_EXT:
            fails.append((n, f'含 {ext} 文件本体'))
        norm = '/' + low.replace('\\', '/')
        for bd in BAD_DIRS:
            if bd in norm:
                fails.append((n, f'条目路径含用户目录 {bd}'))
                break
        base = os.path.basename(low)
        if base in ('secrets.json', 'cloud.json'):
            fails.append((n, f'条目名是配置文件 {base}(可能含密钥)'))

        try:
            d = z.read(n)
        except Exception as ex:            # 损坏条目不应让检查静默通过
            fails.append((n, f'读取失败: {ex}'))
            continue
        total += len(d)

        if user_rx.search(d):
            fails.append((n, '内容含本机用户名路径'))
        for rx in root_rxs:
            if rx.search(d):
                fails.append((n, '内容含本仓库绝对路径'))
                break

        for m in other_user_rx.finditer(d):
            who = m.group(1).decode('ascii', 'ignore').lower()
            if who and who != user.lower():
                notes['别人的 C:\\Users 路径'].add(f'{n}  ->  {who}')
        if word_rx.search(d):
            notes['recordings / projects 字样'].add(n)
        for m in email_rx.finditer(d):
            emails.add(m.group(0).decode('ascii', 'ignore'))

    for want in args.email:
        if want in emails:
            fails.append((want, '指定的邮箱出现在包内'))

    print(f'文件数: {len(names)}   解压后: {total / 1048576:.1f} MB')
    print()

    if fails:
        print(f'[FAIL] 硬性问题 {len(fails)} 条 —— 不要提交')
        for n, why in fails[:40]:
            print(f'    {why:<28s} {n}')
        if len(fails) > 40:
            print(f'    ...还有 {len(fails) - 40} 条')
    else:
        print('[ OK ] 硬性问题 0 条')

    for label, items in notes.items():
        print(f'\n[INFO] {label}: {len(items)} 处')
        for x in sorted(items)[:MAX_SHOW]:
            print('    ', x)
        if len(items) > MAX_SHOW:
            print(f'     ...还有 {len(items) - MAX_SHOW} 处')

    print(f'\n[INFO] 包内邮箱共 {len(emails)} 个(绝大多数是第三方库的版权声明)')
    for e in sorted(emails)[:10]:
        print('    ', e)
    if len(emails) > 10:
        print(f'     ...还有 {len(emails) - 10} 个')

    print()
    print('结论: ' + ('有内容需要处理,先别提交。' if fails else '干净,可以提交。'))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
