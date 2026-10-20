@echo off
chcp 65001
cd /d "%~dp0"

rem 把手机的 8765 端口转发到本机 —— 手机浏览器打开 http://127.0.0.1:8765/phone 时,
rem 实际上是在跟电脑上的服务说话。
rem 需要 adb 在 PATH 里(Android SDK platform-tools);没有的话把下面这行改成 adb.exe 的完整路径。
adb reverse tcp:8765 tcp:8765

rem Python 换成装好 requirements.txt 的解释器;也可以直接用 python。
python "%~dp0server.py"
pause
