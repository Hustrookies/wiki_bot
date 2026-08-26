#!/usr/bin/env bash
# 等 GitHub Pages 生效 —— 0 token。
# 用法: ./wait_live.sh <url> <marker> [预算秒数=300]
# 退出: 0 LIVE / 0 LIVE_DIRTY_CACHE / 1 STALE / 1 NOT_FOUND / 2 NET_DOWN
#
# 这里最隐蔽的坑是 CDN 缓存：不加 cache-buster 会读到缓存的 404，轮询到超时判定失败，
# 而站点其实早就好了 —— 表现为「有时莫名发降级消息」，随机且极难复现。
# 反过来只用 ?cb= 探测就判 LIVE 也不行：用户点的是干净 URL，可能命中旧缓存。
# 所以两步都要：cache-buster 先确认内容已上线，再复核干净 URL。
set -uo pipefail
URL="${1:?用法: wait_live.sh <url> <marker> [秒]}"
MARK="${2:?缺少 marker}"
BUDGET="${3:-300}"
END=$(( $(date +%s) + BUDGET ))
SEEN_200=0; NETFAIL=0

sleep 20                       # Pages 最快也要 ~30s，t=0 探测必然白费

while [ "$(date +%s)" -lt "$END" ]; do
  sep='?'; case "$URL" in *\?*) sep='&';; esac
  body=$(curl -fsS --max-time 10 -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' \
              "${URL}${sep}cb=$(date +%s)" 2>/dev/null)
  if [ $? -eq 0 ]; then
    SEEN_200=1; NETFAIL=0
    if printf '%s' "$body" | grep -qF "$MARK"; then
      for _ in 1 2 3; do      # 复核用户真正会点的那个 URL
        if curl -fsS --max-time 10 "$URL" 2>/dev/null | grep -qF "$MARK"; then
          echo LIVE; exit 0
        fi
        sleep 15
      done
      echo LIVE_DIRTY_CACHE; exit 0
    fi
  else
    NETFAIL=$((NETFAIL+1))
    # 连续 1 分钟连不上 = 本机出网有问题，此时微信大概也发不出去
    [ $NETFAIL -ge 6 ] && { echo NET_DOWN; exit 2; }
  fi
  sleep 10
done

[ $SEEN_200 = 1 ] && { echo STALE; exit 1; }   # 200 但 marker 是旧的：构建卡住或失败
echo NOT_FOUND; exit 1                          # 一直 404：Pages 未启用 / 目录配错 / 首次部署
