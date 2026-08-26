#!/usr/bin/env python3
"""content.json 校验 —— 0 token 的信任边界。

不要靠 agent 的退出码判断它成功了：headless agent 退 0 但写出残缺 JSON 是常见的。
这里是唯一一道「产物必须合格」的闸门，run.sh 在渲染前必须过它。

exit 0 = 通过（可能带 WARN）  exit 1 = 不合格，不要渲染不要推送
"""
import json, os, re, sys
import lib

PLACEHOLDER = re.compile(r"XXX|TODO|待补充|待填|占位|lorem|\{\{|\}\}", re.I)
MOTIF_LABEL = {"timeline": "世界历史", "span": "历史人物/中国历史",
               "layers": "自然地理", "contrast": "科学与技术史",
               "artifact": "文明与文化", "tradeoff": "生物与自然"}


def clen(s):
    """中文字数：按字符算，剔除空白。"""
    return len(re.sub(r"\s", "", s or ""))


def main():
    cpath = sys.argv[1] if len(sys.argv) > 1 else os.path.join(lib.ROOT, "content.json")
    ppath = os.path.join(lib.ROOT, "pick.json")
    err, warn = [], []

    try:
        c = json.load(open(cpath, encoding="utf-8"))
    except FileNotFoundError:
        print(f"FAIL 缺少 {cpath}", file=sys.stderr); sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"FAIL {cpath} 不是合法 JSON：{e}", file=sys.stderr); sys.exit(1)
    if not isinstance(c, dict):
        print("FAIL content.json 顶层不是对象", file=sys.stderr); sys.exit(1)

    pick = json.load(open(ppath, encoding="utf-8")) if os.path.exists(ppath) else {}
    want_motif = pick.get("motif_field")

    # ---- 必填标量 ----
    for k, lo, hi in [("title", 1, 20), ("hook", 1, 40), ("subject", 1, 30),
                      ("summary", 20, 100), ("lead", 60, 180)]:
        v = (c.get(k) or "").strip() if isinstance(c.get(k), str) else ""
        if not v:
            err.append(f"{k} 缺失或为空")
        elif not (lo <= clen(v) <= hi):
            (err if k in ("summary",) else warn).append(
                f"{k} 长度 {clen(v)} 不在 {lo}–{hi}")

    # ---- sections ----
    ss = c.get("sections")
    if not isinstance(ss, list) or len(ss) != 3:
        err.append(f"sections 必须恰好 3 段（实际 {len(ss) if isinstance(ss, list) else 'None'}）")
    else:
        for i, s in enumerate(ss, 1):
            if not isinstance(s, dict) or not (s.get("h") or "").strip():
                err.append(f"sections[{i}].h 缺失")
            if not (s.get("p") or "").strip():
                err.append(f"sections[{i}].p 缺失")
            elif not (80 <= clen(s["p"]) <= 260):
                warn.append(f"sections[{i}].p 长度 {clen(s['p'])} 不在 80–260")

    total = clen(c.get("lead")) + sum(clen(s.get("p")) for s in (ss or []) if isinstance(s, dict))
    # 区间与 prompt.md 的逐字段预算一致：lead 80–120 + 3×(120–180) ≈ 440–660，放宽到 420–950
    if total and not (420 <= total <= 950):
        warn.append(f"正文合计 {total} 字，偏离 500–800 的目标区间")

    # ---- entities / facts ----
    ents = c.get("entities")
    if not isinstance(ents, list) or len(ents) < 3:
        err.append(f"entities 至少 3 个（实际 {len(ents) if isinstance(ents, list) else 'None'}）")
    elif (c.get("subject") or "").strip() not in [str(e).strip() for e in ents]:
        # 实体重叠是去重主信号，subject 不在其中会直接削弱去重能力
        err.append(f"entities 必须包含 subject「{c.get('subject')}」")
    if len(c.get("facts") or []) < 3:
        warn.append(f"facts 只有 {len(c.get('facts') or [])} 条，建议 4–6")

    # ---- motif 与类目匹配 ----
    present = [k for k in MOTIF_LABEL if c.get(k)]
    if want_motif:
        if not c.get(want_motif):
            err.append(f"今日类目需要 motif 字段「{want_motif}」，未提供")
        extra = [k for k in present if k != want_motif]
        if extra:
            warn.append(f"多余的 motif 字段 {extra}（本类目只应有 {want_motif}）")
    elif not present:
        warn.append("没有任何 motif 字段")

    if c.get("timeline") is not None and c.get("timeline") != []:
        tl = c["timeline"]
        if not isinstance(tl, list) or not (3 <= len(tl) <= 5):
            err.append(f"timeline 需 3–5 项（实际 {len(tl) if isinstance(tl, list) else 'None'}）")
        else:
            for i, t in enumerate(tl, 1):
                if not (str(t.get("y", "")).strip() and (t.get("label") or "").strip()):
                    err.append(f"timeline[{i}] 缺 y 或 label")
    if c.get("span"):
        sp = c["span"]
        for k in ("from", "to", "mark"):
            if not str(sp.get(k, "")).strip():
                err.append(f"span.{k} 缺失")
        if all(str(sp.get(k, "")).strip() for k in ("from", "to", "mark")):
            if lib.parse_year(sp["from"]) is None or lib.parse_year(sp["to"]) is None:
                err.append(f"span 的 from/to 无法解析为年代：{sp.get('from')} / {sp.get('to')}")
    if c.get("layers") is not None and c.get("layers") != []:
        ly = c["layers"]
        if not isinstance(ly, list) or not (3 <= len(ly) <= 4):
            err.append(f"layers 需 3–4 层（实际 {len(ly) if isinstance(ly, list) else 'None'}）")
    for k in ("contrast", "tradeoff", "artifact"):
        if c.get(k):
            need = {"contrast": ("then", "now"), "tradeoff": ("gain", "cost"),
                    "artifact": ("name", "note")}[k]
            for f in need:
                if not (c[k].get(f) or "").strip():
                    err.append(f"{k}.{f} 缺失或为空")

    # ---- quote 不得半空；禁止编造引文的结构保障 ----
    q = c.get("quote") or {}
    t, fr = (q.get("text") or "").strip(), (q.get("from") or "").strip()
    if bool(t) != bool(fr):
        err.append("quote 的 text 与 from 必须同时有或同时为空")

    # ---- art：配图描述 ----
    # 缺失只是 WARN 不是 FAIL —— 配图是增益不是依赖，模型漏写不该让当天停更。
    # 但一旦写了就必须合格，半成品会让 gen-image.py 拿着垃圾 prompt 去花钱。
    art = c.get("art") or {}
    if not art:
        warn.append("art 缺失 —— 今日无配图。长期缺失说明 prompt 的配图小节没生效")
    else:
        BANNED = ("照片", "摄影", "镜头", "景深", "胶片", "文字", "铭文", "字迹",
                  "招牌", "4K", "8K", "大师", "电影感", "史诗")
        for k, lbl in (("main", "主图"), ("sub", "附图")):
            a = art.get(k) or {}
            s = (a.get("subject") or "").strip()
            al = (a.get("alt") or "").strip()
            if not s:
                err.append(f"art.{k}.subject 缺失（{lbl}）")
                continue
            if not (25 <= clen(s) <= 90):
                warn.append(f"art.{k}.subject 长度 {clen(s)} 不在 25–90（{lbl}）")
            if not al:
                err.append(f"art.{k}.alt 缺失（{lbl}）—— 无障碍与裂图兜底都靠它")
            elif clen(al) > 45:
                warn.append(f"art.{k}.alt 长度 {clen(al)} 超过 40（{lbl}）")
            hit = [b for b in BANNED if b in s]
            if hit:
                err.append(f"art.{k}.subject 含禁用词 {hit}（{lbl}）—— 见 prompt 三之二禁止项")
            if re.search(r"\d{3,4}\s*年|公元前?\s*\d+", s):
                err.append(f"art.{k}.subject 含年份数字（{lbl}）—— 模型只会画成乱码")
        ms = ((art.get("main") or {}).get("subject") or "").strip()
        bs = ((art.get("sub") or {}).get("subject") or "").strip()
        # 直接复用 100 天去重那套字符二元组：主图与附图必须是不同画面。
        # 不做这个检查的后果是「花两张图的钱拿到一张图的信息量」，且没人会发现。
        if ms and bs and lib.jaccard(ms, bs) >= 0.50:
            err.append(f"主图与附图描述过于相似（{lib.jaccard(ms, bs):.2f}），附图无信息增量")

    # ---- 占位残留 ----
    # 只扫字符串值本身，不扫序列化结果：嵌套对象收尾的 }} 是合法 JSON，
    # 加 art 等嵌套字段后扫 dump 会必然误报。
    def leaves(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from leaves(v)
        elif isinstance(o, list):
            for v in o:
                yield from leaves(v)
        else:
            yield "" if o is None else str(o)
    hit = next((m for s in leaves(c) if (m := PLACEHOLDER.search(s))), None)
    if hit:
        err.append(f"含占位/未完成文本：{hit.group()!r}")

    # ---- uncertain 长期为空是可疑信号，不是错误 ----
    if not (c.get("uncertain") or []):
        warn.append("uncertain 为空 —— 百科题材几乎总有存疑处，注意模型是否在硬编而非标注")
    if not (c.get("tags") or []):
        warn.append("tags 为空")

    for w in warn:
        print(f"WARN {w}")
    for e in err:
        print(f"FAIL {e}", file=sys.stderr)
    if err:
        print(f"—— 共 {len(err)} 项不合格，拒绝渲染", file=sys.stderr)
        sys.exit(1)
    print(f"OK selfcheck 通过（{len(warn)} 项提醒）正文 {total} 字")


if __name__ == "__main__":
    main()
