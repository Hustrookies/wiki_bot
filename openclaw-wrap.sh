#!/usr/bin/env bash
# run.sh 以 `<OPENCLAW> -p "<prompt>"` 调用；openclaw CLI 无 -p 参数，
# 此适配器把它翻译成专用 agent 的 headless 调用。其余参数一律忽略。
set -uo pipefail
PROMPT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -p) PROMPT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$PROMPT" ] || { echo "openclaw-wrap: 未收到 -p 提示词" >&2; exit 2; }
exec openclaw agent --agent "${WIKI_AGENT_ID:-wiki}" --message "$PROMPT"
