#!/usr/bin/env python3
"""queue.add.tsv 校验 —— 0 token 的信任边界，refill 版的 selfcheck。

以前 refill 让 agent 全量重写 data/queue.tsv，且没有任何产物校验：
  · agent 零产出时，run.sh 只数一遍行数就打印「refill 完成」并 exit 0 —— 静默失败；
  · agent 写残时，残缺的池子会被 git add + push 直接推到远端 —— 静默损坏。
这里补上那道闸门。只读 agent 写的增量文件，绝不让它碰 queue.tsv。

行级裁决而非整文件裁决：补池是「有多少收多少」的活，一批 8 行里坏 2 行，
没有理由把另外 6 行也扔掉。合格行写入 <path>.ok，由 run.sh 追加。

用法：refill-check.py <add.tsv> <cat_slug>
exit 0 = 至少 1 行合格（已写出 <add.tsv>.ok）  exit 1 = 无一行可用，不要追加
"""
import re, sys
import lib

PLACEHOLDER = re.compile(r"XXX|TODO|待补充|待填|占位|lorem|\{\{|\}\}", re.I)


def clen(s):
    """中文字数：按字符算，剔除空白。"""
    return len(re.sub(r"\s", "", s or ""))


def probe(title, note, ents):
    """拼成 lib.sim 认的形状。note 充当 summary —— 它就是将来那篇文章的摘要雏形。"""
    return {"title": title, "summary": note, "entities": ents}


def main():
    if len(sys.argv) < 3:
        print("用法：refill-check.py <add.tsv> <cat_slug>", file=sys.stderr); sys.exit(1)
    path, slug = sys.argv[1], sys.argv[2]
    if slug not in lib.SLUG2CAT:
        print(f"FAIL 未知类目 slug：{slug}", file=sys.stderr); sys.exit(1)
    want_cat = lib.SLUG2CAT[slug][1]

    try:
        raw = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"FAIL 缺少 {path}", file=sys.stderr); sys.exit(1)

    queue = lib.load_queue()
    posts = lib.load_posts()
    # 去重要同时看两侧：池内已有（补进去也是浪费一行）和最近推过的（补进去当天就被跳过）
    exist_subj = {q["subject"] for q in queue}
    exist_probe = [probe(q["title"], q["note"], q["entities"])
                   for q in queue if q["cat"] == want_cat]
    post_probe = [probe(p.get("title"), p.get("summary"), p.get("entities"))
                  for p in posts]

    keep, warn, drop = [], [], []
    seen = set()
    for i, ln in enumerate(raw.splitlines(), 1):
        if not ln.strip():
            continue
        if ln.lstrip().startswith("#"):
            warn.append(f"第{i}行是注释行，已丢弃"); continue

        def bad(msg):
            drop.append(f"第{i}行 {msg}")

        c = ln.split("\t")
        if len(c) < 6:
            bad(f"只有 {len(c)} 列（需 6–7 列，分隔符必须是真制表符）"); continue
        if len(c) > 7:
            bad(f"有 {len(c)} 列，超过 7 —— 字段里混进了制表符"); continue
        c = [x.strip() for x in c] + [""] * (7 - len(c))
        cat, region, title, subject, ents_raw, note, wiki = c[:7]

        if cat != want_cat:
            bad(f"cat「{cat}」不是本批的「{want_cat}」"); continue
        if region not in lib.REGIONS:
            bad(f"region「{region}」不在六个合法值内"); continue
        if not subject:
            bad("subject 为空"); continue
        if not title:
            bad("title 为空"); continue
        if not note:
            bad("note 为空 —— 没有钩子的主题写出来必然是条目摘抄"); continue
        if subject in seen:
            bad(f"subject「{subject}」在本批内重复"); continue
        if subject in exist_subj:
            bad(f"subject「{subject}」池内已有"); continue

        ents = [x.strip() for x in ents_raw.split("|") if x.strip()]
        if subject not in ents:
            ents.insert(0, subject)          # 与 lib.load_queue 同样的结构性保证
        if len(ents) < 3:
            bad(f"entities 只有 {len(ents)} 个（需 ≥3）—— 实体重叠是去重主信号"); continue

        m = PLACEHOLDER.search(ln)
        if m:
            bad(f"含占位/未完成文本：{m.group()!r}"); continue

        # 与池内同类目、以及最近推过的文章比相似度。硬阈值命中的行等于死行：
        # 它永远排在候选里却每次都被 pick.py 跳过，白占水位还让 queue_left 虚高。
        p = probe(title, note, ents)
        hi = max([(lib.sim(p, o), o) for o in exist_probe + post_probe] or [(0.0, None)],
                 key=lambda t: t[0])
        if hi[0] >= lib.HARD:
            bad(f"与已有「{hi[1].get('title')}」相似度 {hi[0]:.2f} ≥ {lib.HARD}"); continue
        if hi[0] >= lib.SOFT:
            warn.append(f"第{i}行「{subject}」与「{hi[1].get('title')}」相似度 {hi[0]:.2f}，偏高")

        if clen(title) > 14:
            warn.append(f"第{i}行 title {clen(title)} 字超过 14")
        if clen(note) < 20:
            warn.append(f"第{i}行 note 仅 {clen(note)} 字，钩子可能过于笼统")
        if not wiki:
            # 不是错误：subject 恰好等于条目名时也能抓到。但整批都缺就是 prompt 没生效。
            warn.append(f"第{i}行「{subject}」缺第7列 wiki —— fetch-material.py 只能拿 subject 试")

        seen.add(subject)
        keep.append("\t".join([cat, region, title, subject, "|".join(ents), note, wiki]))

    for w in warn:
        print(f"WARN {w}")
    for d in drop:
        print(f"DROP {d}", file=sys.stderr)

    if not keep:
        print(f"FAIL {path} 无一行合格（丢弃 {len(drop)} 行），不追加", file=sys.stderr)
        sys.exit(1)
    with open(path + ".ok", "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")
    print(f"OK refill-check 收 {len(keep)} 行 / 弃 {len(drop)} 行（{len(warn)} 项提醒）")


if __name__ == "__main__":
    main()
