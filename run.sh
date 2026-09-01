#!/usr/bin/env bash
# 每日百科推送 —— 唯一入口。0 token 的状态机，只有 openclaw 那一步花 token。
#
# 用法:
#   ./run.sh daily     取题 → agent 写稿 → 校验 → 渲染 → 发布（不通知）
#   ./run.sh notify     探测 Pages 生效 → 推微信          ← 与 daily 分两个 cron 窗口
#   ./run.sh refill     补选题池（月度）逐类目分批，旋钮见 refill 分支开头
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

# 告警限流：每天每类最多 ALERT_MAX 条，否则 cron 崩溃循环会把微信刷爆。
# 上限是 2 而不是 1：只发一条会让「07:47 兜底重跑仍失败」完全静默 —— 2026-08-31 的
# motif 缺失就是这样，两跑都失败却只有一条告警，最后靠人手在 11:03 才补上。
# 计数存在标记文件里（一行一次），文件由 state 的 mtime+3 清理规则回收。
ALERT_MAX="${ALERT_MAX:-2}"
alert(){
  local f="state/$TODAY.alert.$1" n=0
  # awk 而不是 grep -c：grep 对空文件输出 0 却退出 1，配 `|| echo 0` 会拼成两行 0
  [ -f "$f" ] && n=$(awk 'NF{c++} END{print c+0}' "$f" 2>/dev/null)
  n=${n:-0}
  if [ "$n" -ge "$ALERT_MAX" ]; then
    log "告警[$1]今日已发 $n 条（上限 $ALERT_MAX），抑制"; return 0
  fi
  printf '%s %s\n' "$(date '+%F %T')" "$2" >> "$f"
  local pre=""; [ "$n" -ge 1 ] && pre="（第 $((n+1)) 次，兜底重跑仍失败）"
  ./notify.sh --kind alert --text "⚠️ 百科推送失败[$1]$pre：$2" || true
}

# 防并发（不是防重复，那是 stage 的事）
exec 9>"/tmp/wiki-bot.lock"
flock -n 9 || { log "上一次仍在运行，跳过"; exit 0; }

exec >>"$LOG" 2>&1
log "=== run.sh $MODE start (stage=$(stage)) ==="

# ---------------------------------------------------------------- refill
# 逐类目、分批、增量补池。
# 以前是「一次 Write 全量重写 queue.tsv」：输入要塞 TSV 全文，输出要先复刻已有行再追加，
# 池子越空要写的越多（每类 6 条时需写满 175 行）—— 实测两次都在 300s 前后零产出，
# 而脚本只数一遍行数就打印「refill 完成」并 exit 0，失败完全静默。
# 现在：单类目 + 单批 ≤BATCH 行 + agent 只写增量文件 + refill-check.py 逐行验收 + 零产出告警。
if [ "$MODE" = refill ]; then
  ADD="data/queue.add.tsv"
  BATCH="${REFILL_BATCH:-8}"; TARGET="${REFILL_TARGET:-25}"; ROUNDS="${REFILL_ROUNDS:-3}"
  # 墙钟预算：refill 全程持着 /tmp/wiki-bot.lock，跑过头会把后面的 daily/notify 窗口挡掉
  BUDGET="${REFILL_BUDGET:-3000}"; T0=$(date +%s)
  qn(){ python3 -c 'import lib;print(len(lib.load_queue()))'; }
  added=0; thin=""
  # REFILL_ONLY="china" 只补指定类目（联调用），默认按 lib.CATS 顺序全轮
  for slug in ${REFILL_ONLY:-$(python3 -c 'import lib;print(" ".join(v[0] for v in lib.CATS.values()))')}; do
    for _ in $(seq 1 "$ROUNDS"); do
      if [ $(( $(date +%s) - T0 )) -ge "$BUDGET" ]; then
        log "refill 预算 ${BUDGET}s 用尽，提前收尾"; break 2
      fi
      STAT=$(python3 pick.py --stat --cat "$slug" --target "$TARGET" --batch "$BATCH") \
          || { alert refill "取池状态失败($slug)"; exit 1; }
      ask=$(printf '%s' "$STAT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ask"])')
      [ "${ask:-0}" -gt 0 ] || break
      before=$(qn)
      rm -f "$ADD" "$ADD.ok"     # 先删，让「文件存在」本身成为「本次写了它」的证据
      timeout "${AGENT_TIMEOUT:-300}" ${OPENCLAW:-openclaw} -p "$(cat refill-prompt.md)

$STAT" ${OPENCLAW_ARGS:---allowed-tools Write --max-turns 3} || true
      if [ ! -s "$ADD" ]; then
        log "refill $slug: agent 无产出（本批要 $ask 行）"; thin="$thin $slug"; break
      fi
      if ! python3 refill-check.py "$ADD" "$slug"; then
        log "refill $slug: 无一行合格，已丢弃"; thin="$thin $slug"; rm -f "$ADD" "$ADD.ok"; break
      fi
      [ -n "$(tail -c1 data/queue.tsv)" ] && printf '\n' >> data/queue.tsv
      cat "$ADD.ok" >> data/queue.tsv; rm -f "$ADD" "$ADD.ok"
      after=$(qn); log "refill $slug: $before → $after 行 (+$((after - before)))"
      added=$((added + after - before))
    done
  done
  n=$(qn)
  if [ "$added" -le 0 ]; then
    alert refill "补池零产出，水位仍 $n 条（未补足：${thin:-全部}）"
    log "refill 零产出，queue.tsv 仍 $n 行"; exit 1
  fi
  log "refill 完成 +$added 行，queue.tsv 现有 $n 行${thin:+（未补足：$thin）}"
  git add -- data/queue.tsv 2>/dev/null
  git diff --cached --quiet || { git commit -q -m "wiki: refill queue (+$added → $n 行)"; git push -q origin "${GIT_BRANCH:-main}" || log "refill push 失败，下次 daily 会带上"; }
  exit 0
fi

# ---------------------------------------------------------------- notify
if [ "$MODE" = notify ]; then
  case "$(stage)" in
    notified) log "今日已通知，跳过"; exit 0 ;;
    none|content|imaged) log "今日尚未发布（stage=$(stage)），无可通知内容"; exit 0 ;;
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
  content|imaged|rendered)  SKIP_AGENT=1 ;;
  *)                 SKIP_AGENT=0 ;;
esac

if [ "$SKIP_AGENT" = 0 ]; then
  PICK=$(python3 pick.py) || {
    r=$(python3 -c "import json;print(json.load(open('pick.json')).get('reason',''))" 2>/dev/null || echo 取题失败)
    alert pick "${r:0:40}"; exit 1; }

  # motif 字段名单独点名一次。$PICK 里本来就带 motif_field，但它混在一堆取题元数据
  # 里容易被忽略：timeline 首次出场那天 agent 把它包进了 __motif__，整期停更。
  MOTIF=$(python3 -c "import json;print(json.load(open('pick.json')).get('motif_field',''))" 2>/dev/null || echo "")
  PROMPT="$(cat prompt.md)

$PICK"
  [ -n "$MOTIF" ] && PROMPT="$PROMPT

【本期 motif】content.json 必须包含顶层字段 \"$MOTIF\"，与 \"art\" 平级，不要包在任何对象里。"
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
  # 留一份失败样本：下一个 cron 窗口补跑时会 rm -f content.json，不留证据就没法
  # 事后定位 schema 失败到底错在哪个字段。
  python3 selfcheck.py || { cp -f content.json "state/$TODAY.content.bad" 2>/dev/null || true
                            alert schema "content.json 校验不通过"; exit 1; }
  set_stage content
fi

if [ "$(stage)" = content ]; then
  # 失败不致命：配图是增益不是依赖。gen-image.py 自己保证 exit 0 与幂等。
  python3 gen-image.py || log "gen-image 异常退出（已忽略，无图继续）"
  set_stage imaged
fi

if [ "$(stage)" = imaged ]; then
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
  # 必须先放锁：refill 是子进程，flock -n 拿不到父进程正持着的同一把锁 ——
  # 这条自愈路径以前每次都只在日志里留下一句「上一次仍在运行，跳过」，从未真正跑过。
  # 放锁后可能与补跑窗口并发，但那一侧会被 stage 拦住（防重复本来就是 stage 的职责）。
  flock -u 9
  # 预算收紧：daily 之后半小时就是 notify 窗口，refill 跑满默认预算会把它整个挡掉
  REFILL_BUDGET="${REFILL_SELFHEAL_BUDGET:-600}" "$ROOT/run.sh" refill || true
fi

if [ -d docs/img ]; then
  IMGMB=$(du -sm docs/img 2>/dev/null | cut -f1)
  log "docs/img 累计 ${IMGMB}MB"
  [ "${IMGMB:-0}" -gt 700 ] && alert size "docs/img 已 ${IMGMB}MB，接近 GitHub 仓库软限制，需处理"
fi

find logs -name '*.log' -mtime +14 -delete 2>/dev/null
find state -name '*.alert.*' -mtime +3 -delete 2>/dev/null
