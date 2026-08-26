#!/usr/bin/env bash
# 判定 ECS 能否访问维基（事实锚数据源）。在 ECS 上直接跑：
#   cd /opt/wiki && ./check-net.sh
#
# 为什么需要单独一个脚本：pick.py 把所有异常都吞成空串，所以「网络被阻断」和
# 「条目名写错了」在日志里长得一模一样。必须在这里把它们分开，否则你会以为
# 事实锚在工作，而它其实每天都是空的。
set -uo pipefail
[ -f .env ] && { set -a; . ./.env; set +a; }

PROXY_NOTE=""
[ -n "${HTTPS_PROXY:-${https_proxy:-}}" ] && PROXY_NOTE=" (经代理 ${HTTPS_PROXY:-$https_proxy})"
echo "=== 维基可达性检查${PROXY_NOTE} ==="
echo

# ---------- 1. DNS ----------
printf '1) DNS 解析 zh.wikipedia.org ... '
IP=$(python3 -c "
import socket,sys
try: print(socket.gethostbyname('zh.wikipedia.org'))
except Exception as e: print('FAIL',e)
" 2>&1)
echo "$IP"

# ---------- 2. 三个 URL，分阶段计时 ----------
# 存在的条目 / 不存在的条目 / 英文站。三者对比能定位问题层次。
probe() {
  local label="$1" url="$2"
  printf '   %-26s ' "$label"
  curl -sS -o /tmp/.wikibody --max-time 12 \
    -w 'http=%{http_code} connect=%{time_connect}s tls=%{time_appconnect}s total=%{time_total}s' \
    "$url" 2>&1 | tr '\n' ' '
  echo
}
echo "2) HTTP 探测（connect=0 表示 TCP 握手就没成功 = 被阻断，不是 404）"
probe "存在的条目(张骞)"   "https://zh.wikipedia.org/api/rest_v1/page/summary/%E5%BC%A0%E9%AA%9E"
probe "不存在的条目"       "https://zh.wikipedia.org/api/rest_v1/page/summary/NoSuchPageXYZ123"
probe "英文站(对照)"       "https://en.wikipedia.org/api/rest_v1/page/summary/Zhang_Qian"
echo

# ---------- 3. 用 pick.py 自己的代码路径验一次 ----------
echo "3) pick.py 的实际取值（这才是每天真正会拿到的东西）"
python3 - <<'PY'
import pick
for s in ["张骞", "卡帕多细亚", "靖难之役"]:
    m = pick.wiki_summary(s)
    print(f"   {s:12} material 长度={len(m):4}  {'✓' if m else '✗ 空'}")
PY
echo

# ---------- 判定 ----------
cat <<'EOF'
=== 怎么读这份结果 ===

A) http=200 且 material 长度 > 0
   → 可达，什么都不用改。事实锚生效。

B) connect 有耗时但 http=404（不存在的条目那行本就该 404）
   → 网络通的。若「存在的条目」也 404，是条目名与中文维基不匹配，
     改 data/queue.tsv 的 subject 列，不是网络问题。

C) connect=0.000000 且 total≈12s（两个 URL 都一样）
   → 网络阻断。中文维基在中国大陆境内不可达，境内 ECS 基本必然是这个结果。
     三个选择：
       1. ECS 有代理：在 .env 加一行，pick.py 用 urllib，会自动走代理，代码零改动
            HTTPS_PROXY=http://127.0.0.1:7890
          然后重跑本脚本确认变成 A。
       2. 换境外 ECS/香港节点。
       3. 放弃事实锚（见下），并把 prompt 的事实约束调紧。

D) 英文站通、中文站不通
   → 少见但可能。可以把 pick.py 的 wiki_summary 改抓 en.wikipedia.org，
     但摘要是英文，需要在 prompt 里说明「material 为英文，翻译后使用」。

=== 选择 3（放弃事实锚）要做什么 ===
   在 .env 加 WIKI_OFF=1（pick.py 会跳过抓取，省掉每天白等 8 秒），
   并接受一个后果：prompt 里「有 material 时数字必须与 material 一致」那条永不生效，
   模型全程处于「只写高置信度内容」模式。
   对策是把校验拧紧 —— 要求 uncertain 非空、禁止未经锚定的精确数字、
   人工抽查前两周的年代与数字。这是本方案最大的剩余风险，不要假装它不存在。
EOF
