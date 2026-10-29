@echo off
cd /d "%~dp0"
title LitPulse 研究脉搏

echo ============================================
echo            LitPulse 一键启动
echo ============================================
echo.

rem 优先用 Windows 的 py 启动器，找不到再用 python
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 bootstrap.py
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python bootstrap.py
    goto :end
)

echo [错误] 没有找到 Python。
echo 请先到 https://www.python.org/downloads/ 安装 Python 3.10 或更高版本，
echo 安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
echo.

:end
echo.
echo 程序已退出。如需手机同 WiFi 访问，请用「启动LitPulse-手机访问.bat」。
pause
