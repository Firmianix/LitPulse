@echo off
cd /d "%~dp0"
title LitPulse（手机局域网访问）

echo ============================================
echo        LitPulse 启动（开启手机访问）
echo ============================================
echo 电脑和手机需连接同一个 WiFi。
echo 启动后，用窗口里显示的 Network URL（形如 http://192.168.x.x:8501）
echo 在手机浏览器打开。首次若弹 Windows 防火墙，请点"允许访问"。
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 bootstrap.py --lan
    goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
    python bootstrap.py --lan
    goto :end
)

echo [错误] 没有找到 Python，请先安装 Python 3.10+（勾选 Add to PATH）。

:end
echo.
pause
