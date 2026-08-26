#!/usr/bin/env bash
# 发布 —— 0 token。追加 posts.jsonl + 白名单 git add + 跳空 commit + 分类重试 push。
# 绝不出现 --force：远端是唯一的备份。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
[ -f .env ] && { set -a; . ./.env; set +a; }

DATE=$(python3 -c "import json;print(json.load(open('pick.json'))['date'])") || {
  echo "publish: 读不到 pick.json"; exit 1; }

# ---------- 1. 追加 posts.jsonl（真相），再写 data/content/ ----------
# 顺序定死：jsonl 先写且是唯一被提交的写入。它是 100 天去重的唯一依据。
python3 - "$DATE" <<'PY' || exit 1
import json, os, sys, lib
date = sys.argv[1]
pick = json.load(open(os.path.join(lib.ROOT, "pick.json"), encoding="utf-8"))
c    = json.load(open(os.path.join(lib.ROOT, "content.json"), encoding="utf-8"))
bid  = open(os.path.join(lib.ROOT, "state", f"{date}.buildid")).read().strip()

# 幂等：同一天已在 jsonl 里就不再追加
if any(p.get("date") == date for p in lib.load_posts()):
    print(f"publish: {date} 已在 posts.jsonl，跳过追加")
else:
    rec = {"date": date, "cat": pick["cat_slug"], "cat_label": pick["cat_label"],
           "title": c["title"], "subject": c["subject"], "summary": c["summary"],
           "entities": c.get("entities", []), "tags": c.get("tags", []),
           "buildid": bid, "url": f"p/{date}.html"}
    with open(lib.jsonl_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"publish: posts.jsonl +1 ({date} {c['title']})")

os.makedirs(os.path.join(lib.ROOT, "data", "content"), exist_ok=True)
# 一天一个新文件、永不改写 —— 对 git 是最优形状，也让日后换模板能 0 token 重渲全站
json.dump(c, open(os.path.join(lib.ROOT, "data", "content", f"{date}.json"), "w",
                  encoding="utf-8"), ensure_ascii=False, indent=1)
PY

# 归档索引依赖 jsonl，刚追加完要重建
python3 render.py --archive-only || exit 1

# ---------- 2. git ----------
command -v git >/dev/null || { echo "publish: 无 git"; exit 1; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "publish: 不是 git 仓库"; exit 1; }

br=$(git rev-parse --abbrev-ref HEAD)
[ "$br" = "${GIT_BRANCH:-main}" ] || {
  echo "publish: HEAD 不在 ${GIT_BRANCH:-main}（当前 $br），拒绝发布"; exit 3; }

# 只 add 显式路径。绝不 git add -A —— .gitignore 写错、目录里有临时文件都不会被带进来
git add -- docs data/posts.jsonl data/content data/queue.tsv 2>/dev/null

NEED_PUSH=0
if git diff --cached --quiet; then
  echo "publish: 无变更，跳过 commit"
else
  git commit -q -m "wiki: $DATE $(python3 -c "import json;print(json.load(open('content.json'))['title'])")" \
    || { echo "publish: commit 失败"; exit 1; }
  NEED_PUSH=1
fi

# 上次可能 commit 成功但 push 失败 —— 本地有未推的 commit 也要推
if [ "$NEED_PUSH" = 0 ]; then
  if ! git diff --quiet '@{u}' HEAD 2>/dev/null; then NEED_PUSH=1; fi
fi
[ "$NEED_PUSH" = 0 ] && { echo "publish: 无需 push"; exit 0; }

# ---------- 3. push，按 stderr 分类重试 ----------
# 不分类的话，一个过期 token 会被重试 3 次、白等 70 秒才告警
n=0
while :; do
  n=$((n+1))
  out=$(git push origin "${GIT_BRANCH:-main}" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then echo "publish: push ok (第 $n 次)"; exit 0; fi
  case "$out" in
    *non-fast-forward*|*"fetch first"*|*rejected*) kind=conflict ;;
    *"Authentication failed"*|*"Permission denied"*|*"could not read Username"*|*403*) kind=auth ;;
    *"Could not resolve host"*|*"Connection timed out"*|*"Failed to connect"*|*"TLS"*|*"Operation timed out"*) kind=net ;;
    *) kind=other ;;
  esac
  echo "publish: push 失败[$kind] 第 $n 次：$(printf '%s' "$out" | tail -2)"
  case "$kind" in
    net)      [ $n -ge 3 ] && exit 1; sleep $((n * n * 5)) ;;
    conflict) [ $n -ge 2 ] && exit 1
              git pull --rebase --autostash origin "${GIT_BRANCH:-main}" || exit 1 ;;
    *)        exit 1 ;;      # auth / other 不重试，立刻让上层告警
  esac
done
