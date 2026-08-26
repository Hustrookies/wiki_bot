#!/usr/bin/env bash
# 每日百科推送 —— 唯一入口。0 token 的状态机，只有 openclaw 那一步花 token。
#
# 用法:
#   ./run.sh daily     取题 → agent 写稿 → 校验 → 渲染 → 发布（不通知）
#   ./run.sh notify     探测 Pages 生效 → 推微信          ← 与 daily 分两个 cron 窗口
#   ./run.sh refill     补选题池（月度）
#   ./run.sh once       daily + 立即 notify（联调用，会自己轮询等 Pages）
#
# 为什么 daily 与 notify 分开：Pages 构建有 30s–2min 延迟。分成两个 cron 窗口（相隔 30 分钟）
# 之后这个竞态结构性消失，不需要在同一次运行里阻塞轮询。日更内容晚半小时没有任何成本。
#
# stage 状态机（state/<date>.stage）：
#   none → content → rendered → pushed → notified
# 重复执行时按 stage 分流，已完成直接 exit 0（0 token）—— 这是补跑窗口能安全存在的前提。

set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH
[ -f .env ] && { set -a; . ./.env; set +a; }
export TZ="${TZN:-Asia/Shanghai}"

MODE="${1:-daily}"
TODAY=$(date +%F)
mkdir -p state logs docs/p data/content
LOG="logs/$(date +%F).log"
ST="state/$TODAY.stage"

log(){ printf '%s %s\n' "$(date '+%F %T')" "$*"; }
stage(){ cat "$ST" 2>/dev/null || echo none; }
set_stage(){ printf '%s\n' "$1" > "$ST.tmp" && mv -f "$ST.tmp" "$ST"; }  # 原子，防 kill -9 截断

# 告警限流：每天每类最多一条，否则 cron 崩溃循环会把微信刷爆
alert(){
  local f="state/$TODAY.alert.$1"
  [ -f "$f" ] && { log "告警[$1]今日已发过，抑制"; return 0; }
  : > "$f"
  ./notify.sh --kind alert --text "⚠️ 百科推送失败[$1]：$2" || true
}

# 防并发（不是防重复，那是 stage 的事）
exec 9>"/tmp/wiki-bot.lock"
flock -n 9 || { log "上一次仍在运行，跳过"; exit 0; }

exec >>"$LOG" 2>&1
log "=== run.sh $MODE start (stage=$(stage)) ==="

# ---------------------------------------------------------------- refill
if [ "$MODE" = refill ]; then
  STAT=$(python3 pick.py --stat) || { alert refill "取池状态失败"; exit 1; }
  PROMPT="$(cat refill-prompt.md)

$STAT"
  timeout "${AGENT_TIMEOUT:-300}" ${OPENCLAW:-openclaw} -p "$PROMPT" \
      ${OPENCLAW_ARGS:---allowed-tools Write --max-turns 3} || true
  n=$(grep -cve '^\s*$' -e '^\s*#' data/queue.tsv 2>/dev/null || echo 0)
  log "refill 完成，queue.tsv 现有 $n 行"
  git add -- data/queue.tsv 2>/dev/null
  git diff --cached --quiet || { git commit -q -m "wiki: refill queue ($n 行)"; git push -q origin "${GIT_BRANCH:-main}" || log "refill push 失败，下次 daily 会带上"; }
  exit 0
fi

# ---------------------------------------------------------------- notify
if [ "$MODE" = notify ]; then
  case "$(stage)" in
    notified) log "今日已通知，跳过"; exit 0 ;;
    none|content) log "今日尚未发布（stage=$(stage)），无可通知内容"; exit 0 ;;
  esac
  BID=$(cat "state/$TODAY.buildid" 2>/dev/null || echo "")
  [ -n "$BID" ] || { log "缺少 buildid"; exit 1; }
  if [ "$(stage)" = rendered ]; then          # 渲染了但没推成功
    ./notify.sh --kind nolink && set_stage notified
    exit 0
  fi
  URL="${PAGE_BASE%/}/p/$TODAY.html"
  RES=$(./wait_live.sh "$URL" "$BID" "${WAIT_BUDGET:-300}"); rc=$?
  log "wait_live → $RES (rc=$rc)"
  case "$RES" in
    LIVE|LIVE_DIRTY_CACHE) ./notify.sh --kind ok       && set_stage notified ;;
    STALE|NOT_FOUND)       ./notify.sh --kind degraded && set_stage notified
                           alert pages "页面未生效($RES)，已发降级消息" ;;
    NET_DOWN)              log "本机出网异常，不通知，留待下个窗口重试"; exit 2 ;;
  esac
  exit 0
fi

# ---------------------------------------------------------------- daily / once
case "$(stage)" in
  notified)          log "今日已完成，跳过"; exit 0 ;;
  pushed)            SKIP_AGENT=1 ;;
  content|rendered)  SKIP_AGENT=1 ;;
  *)                 SKIP_AGENT=0 ;;
esac

if [ "$SKIP_AGENT" = 0 ]; then
  PICK=$(python3 pick.py) || {
    r=$(python3 -c "import json;print(json.load(open('pick.json')).get('reason',''))" 2>/dev/null || echo 取题失败)
    alert pick "${r:0:40}"; exit 1; }

  PROMPT="$(cat prompt.md)

$PICK"
  # 先删旧产物，让「文件存在」本身成为「本次写了它」的证据。
  # 不要靠 openclaw 的退出码 —— headless agent 退 0 但什么都没写是常见的。
  rm -f content.json
  timeout "${AGENT_TIMEOUT:-300}" ${OPENCLAW:-openclaw} -p "$PROMPT" \
      ${OPENCLAW_ARGS:---allowed-tools Write --max-turns 3} || true

  if [ ! -s content.json ]; then
    # agent 可能判了 DUP/ABORT 而故意不写文件，这是正常路径，不该告警
    log "agent 未产出 content.json（可能判定 DUP/ABORT），今日不推送"
    set_stage none; exit 0
  fi
  python3 selfcheck.py || { alert schema "content.json 校验不通过"; exit 1; }
  set_stage content
fi

if [ "$(stage)" = content ]; then
  python3 render.py || { alert render "渲染失败"; exit 1; }
  set_stage rendered
fi

if [ "$(stage)" = rendered ]; then
  if ./publish.sh; then
    set_stage pushed
  else
    alert git "发布失败，已保留内容待重试"
    ./notify.sh --kind nolink && set_stage notified   # 内容不因发布失败而丢失
    exit 1
  fi
fi

log "=== daily 完成 stage=$(stage) ==="

if [ "$MODE" = once ]; then
  exec "$ROOT/run.sh" notify
fi

# 队列低水位自愈：月度 cron 漏了也不断供
if [ -f pick.json ] && python3 -c "import json,sys;sys.exit(0 if json.load(open('pick.json')).get('queue_low') else 1)" 2>/dev/null; then
  log "队列低水位，追加一次 refill"
  "$ROOT/run.sh" refill || true
fi

find logs -name '*.log' -mtime +14 -delete 2>/dev/null
find state -name '*.alert.*' -mtime +3 -delete 2>/dev/null
