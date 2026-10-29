"""
pdfserve.py —— 应用内的 PDF / 静态资源小服务器（标准库，零依赖）
================================================================
为什么需要它：
  Streamlit 1.66 没有官方静态文件服务，而 pdf.js 查看器需要像普通网页一样
  被浏览器加载（viewer.html、pdf.mjs、以及你的 PDF 文件）。
  所以我们在同一个进程里起一个【后台守护线程】HTTP 服务，只白名单托管：
    /static/...   → 项目 static 目录（放 pdf.js 查看器 + 我们自己的自动保存脚本）
    /uploads/...  → 网页上传的临时 PDF
    /library/...  → 我的文库里持久保存的 PDF
  其它路径（.env、代码、data 等）一律 404，不暴露。

画的高亮如何自动存回文库：
  1. 打开【文库里的 PDF】时，viewer_link 会生成一个随机会话令牌并拼在链接里；
  2. 服务端返回 viewer.html 时，在 </head> 前注入我们的 autosave.js（不改动
     第三方 pdf.js 任何文件）；
  3. 在 pdf.js 里画高亮/文字/手绘后，autosave.js 调 saveDocument() 导出带标注
     的 PDF，用 PUT 回传 /library-save（必须带正确令牌），服务端原子覆盖原文件；
  4. 下次打开，标注已烘焙进 PDF，天然还在。

安全：防目录穿越、只允许文件、.mjs 以 JS MIME 发送；写接口必须持一次性令牌，
      目标文件名被严格白名单约束（不能由客户端指定任意路径），并校验 PDF 魔数。
"""

import os
import re
import json
import secrets
import mimetypes
from functools import partial
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_ROOT = os.path.join(PROJECT_DIR, "static")
UPLOAD_ROOT = os.path.join(PROJECT_DIR, "uploads")
LIBRARY_ROOT = os.path.join(PROJECT_DIR, "library")
PREFERRED_PORT = 8502

# 单次回传的 PDF 大小上限（300MB），防止异常超大请求把磁盘写满
MAX_SAVE_BYTES = 300 * 1024 * 1024
VIEWER_REL = os.path.join("pdfjs", "web", "viewer.html")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.pdf$")

# 让 ES Module 正确加载
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/pdf", ".pdf")

# 一次性保存令牌：token -> 文库条目 id（library.json 里的 item_id）
SAVE_TOKENS = {}

_server = None
PORT = None


def new_save_token(item_id):
    """为某个文库条目生成一次性保存令牌；返回 token。"""
    token = secrets.token_urlsafe(24)
    SAVE_TOKENS[token] = item_id
    # 简单控制内存：只保留最近 200 个
    if len(SAVE_TOKENS) > 200:
        for old in list(SAVE_TOKENS.keys())[:-200]:
            SAVE_TOKENS.pop(old, None)
    return token


class _WhitelistHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 安静一点，不刷屏

    # -- 工具 ----------------------------------------------------------
    def _send_bytes(self, body, ctype, status=200, extra_headers=None, close=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_fail(self, status, msg, drain=True):
        # 出错时：若请求体还没读，先读出并丢弃，避免客户端还在发 body 造成连接重置
        # （读过 body 的分支如魔数校验失败，应传 drain=False），然后关闭连接。
        if drain:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if 0 < length <= MAX_SAVE_BYTES:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
        self._send_bytes(json.dumps({"ok": False, "error": msg},
                                    ensure_ascii=False).encode(),
                         "application/json", status=status, close=True)

    def _send_file(self, full_path):
        if not os.path.isfile(full_path):
            self.send_error(404, "Not found")
            return
        ctype = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        try:
            with open(full_path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, "Not found")
            return
        # pdf.js 查看器：运行时注入我们的自动保存脚本（不修改第三方原文件）
        if os.path.relpath(full_path, STATIC_ROOT).replace(os.sep, "/") == VIEWER_REL:
            inject = b'<script src="/static/litpulse-autosave.js" defer></script>'
            if b"</head>" in body:
                body = body.replace(b"</head>", inject + b"</head>", 1)
            elif b"</body>" in body:
                body = body.replace(b"</body>", inject + b"</body>", 1)
        self._send_bytes(body, ctype)

    # -- GET -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/healthz"):
            self._send_bytes(b"litpulse-pdfserve-ok", "text/plain")
            return

        for prefix, root in (("/static/", STATIC_ROOT),
                             ("/uploads/", UPLOAD_ROOT),
                             ("/library/", LIBRARY_ROOT)):
            if path.startswith(prefix):
                rel = os.path.normpath(path[len(prefix):])
                # 防目录穿越：规范化后不得跳出该目录
                if rel.startswith("..") or os.path.isabs(rel) or rel == ".":
                    self.send_error(403, "Forbidden")
                    return
                self._send_file(os.path.join(root, rel))
                return

        self.send_error(404, "Only /static, /uploads and /library are served")

    # -- PUT：把画好标注的 PDF 存回文库 --------------------------------
    def do_PUT(self):
        parsed = urlparse(self.path)
        if unquote(parsed.path) != "/library-save":
            # 消费掉请求体再 404，避免客户端仍在发 body 时连接被重置
            try:
                ln = int(self.headers.get("Content-Length", "0") or "0")
                if 0 < ln <= MAX_SAVE_BYTES:
                    self.rfile.read(ln)
            except (ValueError, OSError):
                pass
            self.send_error(404, "Unknown endpoint")
            return

        # 1) 令牌鉴权
        token = self.headers.get("X-LitPulse-Token", "")
        item_id = SAVE_TOKENS.get(token)
        if not item_id:
            self._json_fail(403, "bad token")
            return

        # 2) 目标文件名必须严格等于 <item_id>.pdf（忽略客户端传入的其它名字）
        qs = parse_qs(parsed.query)
        name = (qs.get("name", [""])[0] or "").strip()
        if not SAFE_NAME_RE.match(name) or not name.endswith(item_id + ".pdf"):
            self._json_fail(403, "bad name")
            return

        # 3) 按声明长度读取（超限拒绝）
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_SAVE_BYTES:
            self._json_fail(413, "bad length")
            return
        body = self.rfile.read(length)

        # 4) 必须是 PDF（魔数校验）
        if not body.startswith(b"%PDF-"):
            self._json_fail(400, "not a pdf", drain=False)
            return

        # 5) 原子写：先写临时文件再替换，避免写一半损坏正在读的 PDF
        os.makedirs(LIBRARY_ROOT, exist_ok=True)
        dest = os.path.join(LIBRARY_ROOT, name)
        tmp = dest + ".saving"
        try:
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, dest)
        except OSError as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            self._send_bytes(
                json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode(),
                "application/json", status=500)
            return

        self._send_bytes(b'{"ok":true}', "application/json")


def ensure_started():
    """幂等启动后台静态服务；返回实际端口。"""
    global _server, PORT
    if _server is not None:
        return PORT

    handler = partial(_WhitelistHandler)
    last_err = None
    httpd = None
    for port in range(PREFERRED_PORT, PREFERRED_PORT + 10):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
            httpd.daemon_threads = True
            break
        except OSError as e:       # 端口被占就试下一个
            last_err = e
            httpd = None
    if httpd is None:
        raise RuntimeError(f"找不到可用端口启动静态服务：{last_err}")

    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    _server, PORT = httpd, port
    return port
