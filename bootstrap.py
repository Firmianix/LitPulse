#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bootstrap.py —— LitPulse 一键启动引导（零第三方依赖，系统 Python 即可运行）
=============================================================================
双击「启动LitPulse.bat」(Windows) 或「启动LitPulse.command」(Mac) 时，
本脚本会自动把一切准备好并打开网页：

  1. 找到 Python 3.10+；
  2. 在项目目录创建独立虚拟环境 .venv（已有则跳过）；
  3. 用国内镜像安装 requirements.txt（失败自动回退官方源，装过则跳过）；
  4. 下载 pdf.js 查看器到 static/pdfjs（用于"点页码跳原文"，失败不影响主功能）；
  5. 启动 streamlit，并自动用默认浏览器打开 http://localhost:8501。

全程不需要用户敲命令。网页第一次打开后，在左侧【⚙️ 模型设置】填一次 Key 即可。
"""

import os
import sys
import shutil
import zipfile
import subprocess
import urllib.request
import threading
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(ROOT, ".venv")
REQ = os.path.join(ROOT, "requirements.txt")
PDFJS_VER = "6.4.299"
PDFJS_DEST = os.path.join(ROOT, "static", "pdfjs")
PDFJS_VIEWER = os.path.join(PDFJS_DEST, "web", "viewer.html")
PORT = int(os.environ.get("LITPULSE_PORT", "8501"))

# pip 镜像：优先国内，失败回退官方源
MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]


def info(msg):
    print(msg, flush=True)


def venv_python(venv_dir):
    """虚拟环境里的解释器路径（区分 Windows / Mac-Linux）。"""
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def running_in_venv():
    """当前解释器是不是项目 .venv 里的那个。"""
    return os.path.abspath(sys.executable) == os.path.abspath(venv_python(VENV_DIR))


def ensure_venv():
    """若 .venv 不存在则创建；创建好后用 venv 解释器重新运行本脚本。"""
    if running_in_venv():
        return
    py = venv_python(VENV_DIR)
    if not os.path.exists(py):
        info("首次启动，正在创建独立运行环境（约需 1 分钟，只这一次）...")
        try:
            subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
        except subprocess.CalledProcessError:
            info("创建虚拟环境失败。请确认已安装 Python 3.10+，且安装时勾选了 pip。")
            sys.exit(1)
        info("运行环境已创建。")
    # 用 venv 解释器重新运行自己，后续 pip / streamlit 都在隔离环境里
    os.chdir(ROOT)
    extra = [a for a in sys.argv[1:] if a != "restarted"]
    os.execv(py, [py, os.path.join(ROOT, "bootstrap.py"), "restarted", *extra])


def ensure_deps():
    """确保 streamlit 已安装；缺失则装 requirements.txt（镜像+回退）。"""
    check = subprocess.run(
        [sys.executable, "-c", "import streamlit, openai, pypdf"],
        capture_output=True,
    )
    if check.returncode == 0:
        return
    info("正在安装运行依赖（首次约 2~5 分钟，请耐心等待）...")
    last_err = None
    for mirror in MIRRORS:
        info(f"  使用源：{mirror}")
        cmd = [sys.executable, "-m", "pip", "install",
               "-r", REQ, "-i", mirror, "--disable-pip-version-check"]
        rc = subprocess.run(cmd).returncode
        if rc == 0:
            info("依赖安装完成。")
            return
        last_err = rc
    info(f"依赖安装失败（返回码 {last_err}）。可检查网络后重新双击启动。")
    sys.exit(1)


def _download(url, dest, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def ensure_pdfjs():
    """下载 pdf.js（可选功能）。任何失败都只提示，不中断启动。"""
    if os.path.exists(PDFJS_VIEWER):
        return
    info("未检测到 pdf.js，尝试下载（用于点击页码跳回原文，可稍后手动补）...")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="litpulse_pdfjs_")
    zpath = os.path.join(tmp, "pdfjs.zip")
    urls = [
        f"https://github.com/mozilla/pdf.js/releases/download/v{PDFJS_VER}/pdfjs-{PDFJS_VER}-dist.zip",
        f"https://ghfast.top/https://github.com/mozilla/pdf.js/releases/download/v{PDFJS_VER}/pdfjs-{PDFJS_VER}-dist.zip",
        f"https://ghproxy.net/https://github.com/mozilla/pdf.js/releases/download/v{PDFJS_VER}/pdfjs-{PDFJS_VER}-dist.zip",
    ]
    ok = False
    for u in urls:
        try:
            info(f"  下载：{u.split('/')[2]}")
            _download(u, zpath)
            if os.path.getsize(zpath) > 1_000_000:
                ok = True
                break
        except Exception as e:  # noqa: BLE001
            info(f"    失败（{type(e).__name__}），换源重试 ...")
    if not ok:
        info("  pdf.js 自动下载失败，不影响其它功能；"
             "可稍后运行 setup_pdfjs.ps1(Windows) 或 setup_pdfjs.sh(Mac) 补装。")
        shutil.rmtree(tmp, ignore_errors=True)
        return
    extract_root = os.path.join(tmp, "x")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(extract_root)
    # 兼容压缩包顶层是否多包一层目录
    src = extract_root
    if not os.path.exists(os.path.join(extract_root, "web", "viewer.html")):
        dirs = [d for d in os.listdir(extract_root)
                if os.path.isdir(os.path.join(extract_root, d))]
        if dirs:
            src = os.path.join(extract_root, dirs[0])
    os.makedirs(PDFJS_DEST, exist_ok=True)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(PDFJS_DEST, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    shutil.rmtree(tmp, ignore_errors=True)
    if os.path.exists(PDFJS_VIEWER):
        info("pdf.js 已就绪。")
    else:
        info("pdf.js 解压后结构异常，跳过（不影响主功能）。")


def open_browser_later(url, delay=3.0):
    def _go():
        import time
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_go, daemon=True).start()


def launch(lan=False):
    os.chdir(ROOT)
    host = "0.0.0.0" if lan else "localhost"
    url = f"http://localhost:{PORT}"
    info("=" * 60)
    info("LitPulse 启动中...")
    info(f"浏览器若没自动打开，请手动访问：{url}")
    if lan:
        info("已开启局域网模式：同一 WiFi 的手机可用本机 IP 访问（见下）。")
    info("关闭这个窗口即可停止程序。")
    info("=" * 60)
    open_browser_later(url)
    cmd = [sys.executable, "-m", "streamlit", "run", "app.py",
           "--server.port", str(PORT),
           "--server.address", host,
           "--server.headless", "true",
           "--browser.gatherUsageStats", "false"]
    try:
        subprocess.run(cmd, cwd=ROOT, check=False)
    except KeyboardInterrupt:
        pass


def main():
    lan = "--lan" in sys.argv
    setup_only = "--setup-only" in sys.argv
    ensure_venv()        # 首次会 exec 重新进入 venv 解释器
    ensure_deps()
    try:
        ensure_pdfjs()
    except Exception as e:  # noqa: BLE001
        info(f"pdf.js 准备环节出错（已跳过）：{e}")
    if setup_only:
        info("环境准备完成 ✅（--setup-only，未启动服务）")
        return
    launch(lan=lan)


if __name__ == "__main__":
    main()
