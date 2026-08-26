#!/usr/bin/env bash
# 推送到微信 —— 0 token。
# 用法:
#   ./notify.sh --kind ok                  页面已验证，正文 + 阅读全文
#   ./notify.sh --kind degraded            页面未验证，正文 + 链接标注生成中
#   ./notify.sh --kind nolink              push 失败，只有正文，无链接
#   ./notify.sh --kind alert --text "..."  纯告警
#
# 设计要点：消息正文自带 title + hook + summary，链接只是「读全文」。
# github.io 在国内可达性是这套方案最弱的一环且无法靠工程修好；正文自带内容意味着
# 链接打不开时这条消息仍然是一张有用的每日百科卡片。这也是「超时也推」的前提。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
[ -f .env ] && { set -a; . ./.env; set +a; }

KIND=ok; TEXT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --kind) KIND="$2"; shift 2 ;;
    --text) TEXT="$2"; shift 2 ;;
    *) echo "未知参数 $1"; exit 2 ;;
  esac
done

TODAY=$(TZ="${TZN:-Asia/Shanghai}" date +%F)
mkdir -p state

# ---------- 组装 markdown ----------
if [ "$KIND" = alert ]; then
  MD="${TEXT:-⚠️ 百科推送异常}"
else
  IFS=$'\x01' read -r TITLE HOOK SUMMARY < <(python3 - <<'PY'
import json, os, lib
c = json.load(open(os.path.join(lib.ROOT, "content.json"), encoding="utf-8"))
def one(s): return " ".join((s or "").split())
print("\x01".join([one(c.get("title")), one(c.get("hook")), one(c.get("summary"))]))
PY
)
  CAT=$(python3 -c "import json;print(json.load(open('pick.json'))['cat_label'])" 2>/dev/null || echo 百科)
  URL="${PAGE_BASE%/}/p/${TODAY}.html"
  ARCH="${PAGE_BASE%/}/archive.html"

  MD="**${TITLE}**
${HOOK}

${SUMMARY}
"
  case "$KIND" in
    ok)       MD="${MD}
[阅读全文 →](${URL})" ;;
    degraded) MD="${MD}
[阅读全文 →](${URL})（页面生成中，1–2 分钟后可访问）" ;;
    nolink)   MD="${MD}
⚠️ 今日页面未发布，稍后补" ;;
  esac
  MD="${MD}
${CAT} · ${TODAY} · [往期](${ARCH})"

  # 通知层自己的第二道锁：同一天只推一次内容
  if [ -f "state/${TODAY}.notified" ]; then
    echo "notify: ${TODAY} 已通知（$(cat "state/${TODAY}.notified")），跳过"; exit 0
  fi
fi

# ============================================================================
# ↓↓↓ 只需要改这一段：换成你已经打通的推送实现 ↓↓↓
# 本机实现：openclaw 微信通道（openclaw-weixin 插件），目标/账号由 .env 提供。
send() {
  local md="$1"
  [ -n "${WEIXIN_TARGET:-}" ] || { echo "notify: WEIXIN_TARGET 未设置"; return 1; }
  openclaw message send --channel "${WEIXIN_CHANNEL:-openclaw-weixin}" \
    --account "${WEIXIN_ACCOUNT:-}" --target "$WEIXIN_TARGET" -m "$md"
}
# ↑↑↑ 改到这里为止 ↑↑↑
# ============================================================================

if send "$MD"; then
  echo
  [ "$KIND" = alert ] || printf '%s %s\n' "$KIND" "$(cat "state/${TODAY}.buildid" 2>/dev/null || echo -)" \
      > "state/${TODAY}.notified"
  echo "notify: 已发送 [$KIND]"
else
  echo "notify: 发送失败 [$KIND]" >&2
  # 通知失败必须能在不重跑 agent 的前提下重试
  [ "$KIND" = alert ] || printf '%s\n' "$KIND" > "state/${TODAY}.notify_pending"
  exit 1
fi
