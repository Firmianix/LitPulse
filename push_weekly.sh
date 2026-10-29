#!/usr/bin/env bash
# push_weekly.sh —— 供定时任务（cron）每周调用：
#   1. 运行追新+打分+立场记忆，生成当周 Markdown 周报；
#   2. 把周报正文输出到 stdout（定时系统会把 stdout 作为消息推送给你）；
#   3. 末尾附 MEDIA: 绝对路径，把 .md 文件作为可收藏附件一并发送。
#
# 说明：使用系统 python3（仅需 openai）；配置见 .env 或环境变量。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

if ! python3 weekly_pulse.py >"$LOG" 2>&1; then
  echo "📡 LitPulse 周报运行出错，日志如下："
  echo '```'
  tail -n 25 "$LOG"
  echo '```'
  exit 1
fi

TODAY="$(date +%F)"
RPT="reports/pulse_${TODAY}.md"

if [[ -f "$RPT" ]]; then
  cat "$RPT"
  echo
  echo "MEDIA:${DIR}/${RPT}"
else
  echo "📡 LitPulse 本周已运行，但未找到周报文件 ${RPT}"
fi
