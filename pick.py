#!/usr/bin/env python3
"""取题 —— 0 token。stdout 一行 JSON，就是 agent 的全部输入。

排班表（确定性，可审计，可手工插队）：
  ISO 星期 → 大类     1 世界历史 … 7 中国历史
  ISO 周数 → 地域倾向  week % 6，强行打散欧美偏斜
去重：subject 精确 + 二元组/实体相似度，窗口 100 天。命中硬阈值自动取下一个候选，
不中止 —— 重复检测不该以停更一天为代价。

用法：
  ./pick.py                  正常取题，输出一行 JSON（并落盘 pick.json）
  ./pick.py --date 2026-09-01  指定日期（联调用）
  ./pick.py --skip 张骞        排除某个 subject 后重取（agent 判 DUP 后由 run.sh 调）
  ./pick.py --no-wiki         不抓维基摘要（离线联调）
  ./pick.py --stat            各类目水位概览（人看的）
  ./pick.py --stat --cat china  单类目补池输入（喂 refill-prompt.md）
"""
import argparse, datetime as dt, json, os, sys, urllib.parse, urllib.request
import lib

WINDOW = 100          # 去重窗口（天）
NEAR_N = 3            # 交给模型的近似条目数
WIKI_TIMEOUT = 8


def sched(date):
    """(cat_slug, cat_label, motif, region)"""
    iso = date.isocalendar()
    slug, label, motif = lib.CATS[iso[2]]
    return slug, label, motif, lib.REGIONS[iso[1] % len(lib.REGIONS)]


def wiki_summary(subject, reason=None):
    """中文维基摘要，事实锚。免 key。失败返回空串 —— 事实锚是增益，不是依赖。

    reason 是一个可选的 list，用来带回失败原因。必须区分「网络阻断」和「条目不存在」：
    两者都返回空串，但前者说明事实锚整体失效（境内 ECS 的常见情况），后者只是
    这一条 subject 写错了。混为一谈会让你以为锚在工作，而它每天都是空的。
    走 urllib，会自动尊重 HTTPS_PROXY / https_proxy 环境变量。
    """
    def note(x):
        if reason is not None:
            reason.append(x)
    if not subject:
        note("empty_subject"); return ""
    if os.environ.get("WIKI_OFF"):
        note("disabled_by_WIKI_OFF"); return ""
    url = (os.environ.get("WIKI_API", "https://zh.wikipedia.org/api/rest_v1/page/summary/")
           + urllib.parse.quote(subject.replace(" ", "_"), safe=""))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "wiki-bot/1.0 (daily encyclopedia push; personal use)",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=WIKI_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("type", "").endswith("not_found"):
            note("not_found"); return ""
        ex = (d.get("extract") or "").strip()[:600]
        note("ok" if ex else "empty_extract")
        return ex
    except urllib.error.HTTPError as e:
        note(f"http_{e.code}")           # 404 = 条目名不对，改 queue.tsv
    except Exception as e:
        note(f"unreachable_{type(e).__name__}")   # 超时/连接失败 = 网络层，事实锚整体失效
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--skip", action="append", default=[])
    ap.add_argument("--no-wiki", action="store_true")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--cat", help="类目 slug，配合 --stat 只输出该类目的补池输入")
    ap.add_argument("--target", type=int, default=25, help="每类目目标水位")
    ap.add_argument("--batch", type=int, default=8, help="单批最多要多少行")
    a = ap.parse_args()

    today = (dt.date.fromisoformat(a.date) if a.date else dt.date.today())
    cutoff = (today - dt.timedelta(days=WINDOW)).isoformat()

    posts = lib.load_posts()
    queue = lib.load_queue()
    recent = [p for p in posts if p.get("date", "") >= cutoff]
    recent_subjects = {p.get("subject") for p in recent if p.get("subject")}

    # ---------- --stat：给 refill-prompt.md ----------
    if a.stat:
        # have 与 queue_low 同一口径：窗口内已推过的 subject 不计入水位
        # （100 天后回收才重新可用）。所以 have 是「现在还能取的条数」，不是行数。
        left = {}
        for lbl in (v[1] for v in lib.CATS.values()):
            left[lbl] = sum(1 for q in queue
                            if q["cat"] == lbl and q["subject"] not in recent_subjects)
        if not a.cat:
            print(json.dumps({"queue_left": left, "target": a.target,
                              "recent_subjects": sorted(recent_subjects)},
                             ensure_ascii=False))
            return
        if a.cat not in lib.SLUG2CAT:
            print(f"未知类目 slug：{a.cat}", file=sys.stderr); sys.exit(2)
        label = lib.SLUG2CAT[a.cat][1]
        mine = [q for q in queue if q["cat"] == label]
        rc = {r: sum(1 for q in mine if q["region"] == r) for r in lib.REGIONS}
        need = max(0, a.target - left[label])
        # 只给 subject/region/title，不给 queue.tsv 全文。以前塞 3.4KB 原文并要求
        # 「原样复刻后追加」，输出量被输入撑到 175 行，必然在 timeout 里零产出。
        # 去重判断只需要可比的 subject 列表，note/entities 全文不必进 prompt。
        print(json.dumps({
            "cat": label, "cat_slug": a.cat,
            "target": a.target, "have": left[label], "need": need,
            "ask": min(need, max(1, a.batch)) if need else 0,
            "regions": lib.REGIONS,
            "region_counts": rc,
            "region_thin": [r for r in lib.REGIONS if rc[r] <= 1],
            "existing": [{"subject": q["subject"], "region": q["region"],
                          "title": q["title"]} for q in mine],
            "recent_subjects": sorted(recent_subjects),
        }, ensure_ascii=False))
        return

    slug, label, motif, region = sched(today)
    skip = set(a.skip)

    # ---------- 候选：本类目、未在窗口内用过、未被 --skip ----------
    cands = [q for q in queue
             if q["cat"] == label and q["subject"] not in recent_subjects
             and q["subject"] not in skip]
    # 地域优先，其余保持 queue.tsv 行序（可手工插队）
    cands.sort(key=lambda q: (q["region"] != region, q["row"]))

    chosen, near, skipped = None, [], []
    for q in cands:
        probe = {"title": q["title"], "summary": q["note"], "entities": q["entities"]}
        scored = []
        for p in recent:
            s = lib.sim(probe, p)
            if s >= lib.SOFT:
                scored.append((s, p))
        scored.sort(key=lambda t: -t[0])
        if scored and scored[0][0] >= lib.HARD:
            skipped.append({"subject": q["subject"],
                            "hit": scored[0][1].get("title"),
                            "score": round(scored[0][0], 3)})
            continue                      # 硬重复 → 换下一个，不放弃今天
        chosen = q
        near = [{"title": p.get("title"), "summary": p.get("summary"),
                 "days_ago": (today - dt.date.fromisoformat(p["date"])).days,
                 "score": round(s, 3)}
                for s, p in scored[:NEAR_N]]
        break

    if chosen is None:
        print(json.dumps({
            "ok": False, "date": today.isoformat(), "cat": label,
            "reason": f"{label} 类目无可用候选（池内 {sum(1 for q in queue if q['cat']==label)} 条，"
                      f"窗口内已用 {len([q for q in queue if q['cat']==label and q['subject'] in recent_subjects])} 条）",
            "skipped": skipped,
        }, ensure_ascii=False))
        sys.exit(2)

    # 100 天前用过 → 回收题，必须换切入角度；把旧文摘要塞进 near 供模型避开
    old = [p for p in posts
           if p.get("subject") == chosen["subject"] and p.get("date", "") < cutoff]
    recycled = bool(old)
    if recycled:
        o = max(old, key=lambda p: p["date"])
        near.insert(0, {"title": o.get("title"), "summary": o.get("summary"),
                        "days_ago": (today - dt.date.fromisoformat(o["date"])).days,
                        "score": 1.0, "is_previous_take": True})

    # 事实锚优先读本地 data/material.json（由 fetch-material.py 在能访问
    # dumps.wikimedia.org 的机器上预抓取并提交进 git）。ECS 运行时零网络依赖 ——
    # 中文维基的 API/网站在境内被阻断，运行时抓取必然失败。
    material, mstatus = "", "skipped_no_wiki"
    if not a.no_wiki:
        mpath = os.path.join(lib.ROOT, "data", "material.json")
        local = {}
        if os.path.exists(mpath):
            try:
                local = json.load(open(mpath, encoding="utf-8"))
            except Exception:
                local = {}
        ent = local.get(chosen["subject"])
        if ent and ent.get("text"):
            material, mstatus = ent["text"], "local"
        elif ent:
            material, mstatus = "", f"local_empty:{ent.get('status', '?')}"
        elif os.environ.get("WIKI_LIVE"):
            # 仅在显式开启时才走网络（能直连维基的环境，比如境外节点）
            _r = []
            material = wiki_summary(chosen.get("wiki") or chosen["subject"], _r)
            mstatus = "live_" + (_r[0] if _r else "unknown")
        else:
            mstatus = "not_prefetched"

    out = {
        "ok": True,
        "date": today.isoformat(),
        "date_label": f"{today.month}月{today.day}日",
        "cat": label, "cat_label": label, "cat_slug": slug,
        "motif_field": motif,
        "region": region,
        "topic": {"title": chosen["title"], "subject": chosen["subject"],
                  "note": chosen["note"], "entities": chosen["entities"]},
        "material": material,
        "material_status": mstatus,   # ok / not_found / unreachable_* / disabled_by_WIKI_OFF
        "near": near[:NEAR_N + 1],
        "recycled": recycled,
        "queue_left": {lbl: sum(1 for q in queue
                                if q["cat"] == lbl and q["subject"] not in recent_subjects)
                       for lbl in (v[1] for v in lib.CATS.values())},
        "skipped_as_dup": skipped,
    }
    out["queue_low"] = any(v < 8 for v in out["queue_left"].values())

    line = json.dumps(out, ensure_ascii=False)
    with open(os.path.join(lib.ROOT, "pick.json"), "w", encoding="utf-8") as f:
        f.write(line)
    print(line)


if __name__ == "__main__":
    main()
