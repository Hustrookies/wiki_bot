#!/usr/bin/env bash
# 渲染 7 套样例并（在 macOS 上）打开浏览器。0 token，不碰 docs/ 和 git。
# 改皮肤时反复跑这个，比等每日推送快得多。
set -euo pipefail
cd "$(dirname "$0")"
for c in event person geo science culture bio china; do
  printf '%-9s ' "$c"
  python3 render.py --sample "samples/$c.json"
done
if [ "${1:-}" = --open ] && command -v open >/dev/null; then
  open /tmp/wiki-preview-*.html
fi
echo "提示：加 --open 直接在浏览器打开；亮/暗两版都要看（系统外观里切）"
