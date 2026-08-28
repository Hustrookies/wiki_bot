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
import os, re, sys
import lib

INDEX = os.path.join(lib.ROOT, ".cache",
                     "zhwiki-latest-pages-articles-multistream-index.txt")

# 与 fetch-material.py 同一份可选依赖：有 zhconv 才能试简繁形态。
sys.path.insert(0, os.path.join(lib.ROOT, "vendor"))
try:
    from zhconv import convert as _zhconv
except ImportError:
    _zhconv = None

PLACEHOLDER = re.compile(r"XXX|TODO|待补充|待填|占位|lorem|\{\{|\}\}", re.I)


def clen(s):
    """中文字数：按字符算，剔除空白。"""
    return len(re.sub(r"\s", "", s or ""))


def probe(title, note, ents):
    """拼成 lib.sim 认的形状。note 充当 summary —— 它就是将来那篇文章的摘要雏形。"""
    return {"title": title, "summary": note, "entities": ents}


def variants(t):
    """标题的原样/繁体/简体三形态 —— 与 fetch-material.py 的候选逻辑保持一致。"""
    out = []
    conv = (lambda x, to: _zhconv(x, to)) if _zhconv else (lambda x, to: None)
    for x in (t, conv(t, "zh-hant") if t else None, conv(t, "zh-hans") if t else None):
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return out


def verify_wiki(rows, warn):
    """用本地 zhwiki 索引核对第 7 列 —— 索引不在就跳过，不阻塞补池。

    这是那 20 行手工修正的自动化。实测 agent 写的 wiki 列约 1/8 是臆造名或 subject
    的同义改写，而「标题在不在索引里」是确定性的，没有第二种答案：查不到就说明
    抓锚时它一定是无效候选，与其留着误导，不如换成索引里确认存在的那个。

    索引 200MB，按行扫一遍即可 —— 候选集只有几十个，不必像 fetch-material.py
    那样建全量字典（那要 1.2GB 内存）。
    """
    if not os.path.exists(INDEX):
        warn.append("无 .cache/ 索引，跳过 wiki 列核对（在能访问 dumps 的机器上跑一次 "
                    "fetch-material.py 即可获得）")
        return
    want = set()
    for r in rows:
        want.update(variants(r[6]))
        want.update(variants(r[3]))
    hit = set()
    with open(INDEX, encoding="utf-8", errors="ignore") as f:
        for ln in f:
            p = ln.rstrip("\n").split(":", 2)
            if len(p) == 3 and p[2] in want:
                hit.add(p[2])
    for r in rows:
        wk, subj = r[6], r[3]
        if any(t in hit for t in variants(wk)):
            continue                                   # 填对了，原样保留
        sub_hit = next((t for t in variants(subj) if t in hit), "")
        # 命中的就是 subject 本身时留空 —— 这一列的语义是「与 subject 不同的真实条目名」，
        # 回填一份副本只是噪声（fetch-material.py 的候选表本来就会去重）。
        fix = "" if sub_hit == subj else sub_hit
        if wk and sub_hit:
            warn.append(f"「{subj}」wiki 列「{wk}」不在 zhwiki 索引里，"
                        + (f"已改为「{fix}」" if fix else "已清空（subject 本身就是条目名）"))
            r[6] = fix
        elif sub_hit:
            r[6] = fix                                 # 原本留空，只在标题确实不同时才补
        elif wk:
            warn.append(f"「{subj}」wiki 列「{wk}」与 subject 都不在索引里 —— 这条没有事实锚")
        else:
            warn.append(f"「{subj}」在 zhwiki 索引里查不到 —— 这条没有事实锚")


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
        # wiki 留空不是错误：prompt 明确允许拿不准就留空，verify_wiki 会拿索引补。
        seen.add(subject)
        keep.append([cat, region, title, subject, "|".join(ents), note, wiki])

    verify_wiki(keep, warn)

    for w in warn:
        print(f"WARN {w}")
    for d in drop:
        print(f"DROP {d}", file=sys.stderr)

    if not keep:
        print(f"FAIL {path} 无一行合格（丢弃 {len(drop)} 行），不追加", file=sys.stderr)
        sys.exit(1)
    with open(path + ".ok", "w", encoding="utf-8") as f:
        f.write("\n".join("\t".join(r) for r in keep) + "\n")
    print(f"OK refill-check 收 {len(keep)} 行 / 弃 {len(drop)} 行（{len(warn)} 项提醒）")


if __name__ == "__main__":
    main()
