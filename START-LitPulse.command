#!/bin/bash
# Mac 双击启动 LitPulse（在 Finder 中双击本文件即可）。
# 若提示没有权限：打开"终端"，cd 到本目录，执行一次  chmod +x 启动LitPulse.command
cd "$(dirname "$0")"

echo "============================================"
echo "             LitPulse 一键启动"
echo "============================================"

if command -v python3 >/dev/null 2>&1; then
    python3 bootstrap.py
else
    echo "[错误] 没有找到 python3。请先安装 Python 3.10+："
    echo "  方式一：App Store 安装 Xcode 命令行工具，或"
    echo "  方式二：到 https://www.python.org/downloads/ 下载 macOS 安装包。"
fi

echo ""
echo "程序已退出。"
read -n 1 -s -r -p "按任意键关闭窗口..."
