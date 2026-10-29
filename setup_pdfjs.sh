#!/usr/bin/env bash
# 一键下载 pdf.js 官方查看器到 static/pdfjs（引用点击跳回原文功能需要它）。
# 已存在则跳过。网络不通时可手动下载 dist.zip 解压到 static/pdfjs。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$DIR/static/pdfjs"
VER="6.4.299"

if [[ -f "$DEST/web/viewer.html" ]]; then
  echo "pdf.js 已存在：$DEST"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
URL="https://github.com/mozilla/pdf.js/releases/download/v${VER}/pdfjs-${VER}-dist.zip"
echo "下载 pdf.js v${VER} ..."
# 直连失败时尝试常见 GitHub 代理
curl -fL --retry 2 -o "$TMP/p.zip" "$URL" \
  || curl -fL --retry 2 -o "$TMP/p.zip" "https://ghfast.top/$URL"
mkdir -p "$DEST"
unzip -q -o "$TMP/p.zip" -d "$DEST"
echo "完成：$DEST/web/viewer.html"
