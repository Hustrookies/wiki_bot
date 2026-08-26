#!/usr/bin/env python3
"""content.json + pick.json → docs/p/<date>.html（+ index.html 副本 + archive.html）

0 token。不写 posts.jsonl、不碰 git —— 那是 publish.sh 的事（推送成功才算数）。

用法：
  ./render.py                        # 正常：读 pick.json + content.json
  ./render.py --sample samples/x.json  # 预览：单文件自带 meta+content，输出到 /tmp
"""
import argparse, html, json, os, re, shutil, sys
import lib

ROOT = lib.ROOT
NO_ESC = {"__css"}          # 唯一的不转义白名单：CSS 注入


# ---------------- 模板引擎（沿用 football-bot，仅加 NO_ESC） ----------------
def esc(v):
    return html.escape("" if v is None else str(v), quote=True)

def render(tpl, sc):
    def each(mo):
        body = mo.group(2)
        return "".join(render(body, {**sc, **it} if isinstance(it, dict) else sc)
                       for it in (sc.get(mo.group(1)) or []))
    tpl = re.sub(r"<!--\s*each:(\w+)\s*-->(.*?)<!--\s*/each:\1\s*-->", each, tpl, flags=re.S)

    def cond(mo):
        v = sc.get(mo.group(1))
        return render(mo.group(2), sc) if v not in (None, "", [], {}, 0, False) else ""
    tpl = re.sub(r"<!--\s*if:(\w+)\s*-->(.*?)<!--\s*/if:\1\s*-->", cond, tpl, flags=re.S)

    def var(mo):
        k = mo.group(1)
        v = sc.get(k, "")
        return ("" if v is None else str(v)) if k in NO_ESC else esc(v)
    return re.sub(r"\{\{(\w+)\}\}", var, tpl)


# ---------------- 打平嵌套 / 归一列表 ----------------
def strlist(xs):
    """模板引擎的 each 无法取裸字符串项，统一包成 {"v": ...}"""
    return [{"v": x} for x in (xs or []) if str(x).strip()]

def build_scope(meta, content):
    cat_slug = meta["cat_slug"]
    sc = {
        "cat_slug":   cat_slug,
        "cat_label":  meta["cat_label"],
        "date_label": meta["date_label"],
        "theme_color": lib.THEME_COLOR.get(cat_slug, "#1f3352"),
        "archive_rel": meta.get("archive_rel", "../archive.html"),
        "source":     meta.get("source", ""),
        "title":  (content.get("title") or "").strip(),
        "hook":   (content.get("hook") or "").strip(),
        "lead":   (content.get("lead") or "").strip(),
        "summary": (content.get("summary") or "").strip(),
        "sections": [s for s in (content.get("sections") or []) if s.get("p")],
        "facts":    [f for f in (content.get("facts") or []) if f.get("v")],
        "trivia":   strlist(content.get("trivia")),
        "uncertain": strlist(content.get("uncertain")),
        "tags":     strlist(content.get("tags")),
    }

    # quote：只有 text 非空才整块出现
    q = content.get("quote") or {}
    sc["quote_text"] = (q.get("text") or "").strip()
    sc["quote_from"] = (q.get("from") or "").strip()

    # ---- motif ----
    sc["timeline"] = [t for t in (content.get("timeline") or []) if t.get("label")]
    sc["layers"]   = [l for l in (content.get("layers") or []) if l.get("name")][:4]

    bars = []
    for b in (content.get("bars") or []):
        if not b.get("k"):
            continue
        try:
            pct = max(2, min(100, int(round(float(b.get("pct", 0))))))
        except (TypeError, ValueError):
            pct = 2
        bars.append({"k": b["k"], "v": b.get("v", ""), "pct": pct})
    sc["bars"] = bars

    sp = content.get("span") or {}
    sc["span_from"] = str(sp.get("from", "") or "").strip()
    sc["span_to"]   = str(sp.get("to", "") or "").strip()
    sc["span_mark"] = str(sp.get("mark", "") or "").strip()
    sc["span_mark_label"] = (sp.get("mark_label") or "").strip()
    sc["span_pct"] = lib.ruler_pct(sc["span_from"], sc["span_to"], sc["span_mark"]) \
        if sc["span_from"] else 50

    ct = content.get("contrast") or {}
    sc["contrast_then"] = (ct.get("then") or "").strip()
    sc["contrast_now"]  = (ct.get("now") or "").strip()

    tr = content.get("tradeoff") or {}
    sc["tradeoff_gain"] = (tr.get("gain") or "").strip()
    sc["tradeoff_cost"] = (tr.get("cost") or "").strip()

    af = content.get("artifact") or {}
    sc["artifact_name"] = (af.get("name") or "").strip()
    sc["artifact_note"] = (af.get("note") or "").strip()
    return sc


def load_css(cat_slug):
    """内联 base.css + 当前皮肤。只注入一套皮肤，7 套之间不存在级联冲突。

    内联而非外链的理由：github.io 跨境访问不稳，多一个 CSS 往返就多一类
    「HTML 到了、样式没到 → 用户看到裸文本」的半成功失败。见手册 §2.3。
    """
    out = []
    for name in ("base.css", f"t-{cat_slug}.css"):
        p = os.path.join(ROOT, "themes", name)
        if not os.path.exists(p):
            sys.exit(f"缺少皮肤文件：themes/{name}")
        css = open(p, encoding="utf-8").read()
        if "</style" in css.lower():
            sys.exit(f"themes/{name} 含 </style，会逃出 <style> 块，拒绝渲染")
        out.append(css)
    return "\n".join(out)


def render_page(meta, content):
    sc = build_scope(meta, content)
    sc["buildid"] = meta["buildid"]
    sc["__css"] = load_css(meta["cat_slug"])
    tpl = open(os.path.join(ROOT, "template.html"), encoding="utf-8").read()
    return render(tpl, sc)


def selfcheck_html(page, where):
    """football-bot 手册 §4 那三行自检，升级成每次必跑。"""
    bad = []
    left = sorted(set(re.findall(r"\{\{\w+\}\}", page)))
    if left:
        bad.append(f"残留占位符 {left}")
    marks = sorted(set(re.findall(r"<!--\s*(?:each|if|/each|/if):\w+\s*-->", page)))
    if marks:
        bad.append(f"残留模板标记 {marks}")
    for a, b in (("<section", "</section"), ("<div", "</div"), ("<ul", "</ul")):
        if page.count(a) != page.count(b):
            bad.append(f"标签不配对 {a} {page.count(a)}≠{page.count(b)}")
    if bad:
        sys.exit(f"渲染自检失败（{where}）：" + "；".join(bad))


# ---------------- 归档索引 ----------------
def render_archive():
    posts = sorted(lib.load_posts(), key=lambda d: d.get("date", ""), reverse=True)
    rows = []
    for p in posts:
        rows.append(
            f'<li><a href="p/{html.escape(p["date"])}.html">'
            f'<span class="d">{html.escape(p["date"])}</span>'
            f'<span class="c">{html.escape(p.get("cat_label", ""))}</span>'
            f'<span class="t">{html.escape(p.get("title", ""))}</span></a>'
            f'<p>{html.escape(p.get("summary", ""))}</p></li>')
    page = ARCHIVE_TPL.replace("{{n}}", str(len(posts))).replace("{{rows}}", "\n".join(rows))
    out = os.path.join(ROOT, "docs", "archive.html")
    open(out, "w", encoding="utf-8").write(page)
    return len(posts)


ARCHIVE_TPL = """<!DOCTYPE html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>归档 · 每日百科</title><style>
:root{--paper:#f7f7f5;--card:#fff;--fg:#141414;--dim:#5b5a57;--muted:#8a8884;--line:#e4e3de}
@media(prefers-color-scheme:dark){:root{--paper:#0e1013;--card:#181b20;--fg:#e8eaed;--dim:#9aa0a8;--muted:#7d838b;--line:#282c33}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--fg);
font:16px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:600px;margin:0 auto;padding:28px 16px 40px}
h1{font-size:21px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
ul{list-style:none;margin:0;padding:0}
li{background:var(--card);border-radius:12px;padding:14px 16px;margin-bottom:10px}
a{text-decoration:none;color:inherit;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.d{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
.c{font-size:11.5px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:1px 6px;flex:none}
.t{font-weight:600;font-size:16px}
li p{margin:7px 0 0;font-size:13.5px;color:var(--dim);line-height:1.65}
</style></head><body><div class="wrap">
<h1>每日百科 · 归档</h1><p class="sub">共 {{n}} 期</p>
<ul>{{rows}}</ul></div></body></html>"""


# ---------------- 入口 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", help="预览单个 samples/*.json，输出到 /tmp，不动 docs/")
    ap.add_argument("--archive-only", action="store_true", help="只重建归档索引")
    a = ap.parse_args()

    if a.archive_only:
        print(f"archive rebuilt: {render_archive()} 期")
        return

    if a.sample:
        d = json.load(open(a.sample, encoding="utf-8"))
        meta, content = d["meta"], d["content"]
        meta["buildid"] = "sample"
        meta.setdefault("archive_rel", "archive.html")
        page = render_page(meta, content)
        selfcheck_html(page, a.sample)
        out = f"/tmp/wiki-preview-{meta['cat_slug']}.html"
        open(out, "w", encoding="utf-8").write(page)
        print(out)
        return

    pick = json.load(open(os.path.join(ROOT, "pick.json"), encoding="utf-8"))
    content = json.load(open(os.path.join(ROOT, "content.json"), encoding="utf-8"))
    meta = {
        "cat_slug":   pick["cat_slug"],
        "cat_label":  pick["cat_label"],
        "date_label": pick["date_label"],
        "source":     "维基百科摘要" if pick.get("material") else "",
        "archive_rel": "../archive.html",
    }
    meta["buildid"] = lib.buildid(pick["date"], lib.canonical(content))

    page = render_page(meta, content)
    selfcheck_html(page, "content.json")

    docs = os.path.join(ROOT, "docs")
    os.makedirs(os.path.join(docs, "p"), exist_ok=True)
    day = os.path.join(docs, "p", f"{pick['date']}.html")
    open(day, "w", encoding="utf-8").write(page)

    # index.html 是最新一期的稳定入口。相对路径要改，否则归档链接指错
    open(os.path.join(docs, "index.html"), "w", encoding="utf-8").write(
        page.replace('href="../archive.html"', 'href="archive.html"'))

    # 把 buildid 落盘给 wait_live.sh 用
    os.makedirs(os.path.join(ROOT, "state"), exist_ok=True)
    open(os.path.join(ROOT, "state", f"{pick['date']}.buildid"), "w").write(meta["buildid"])

    n = render_archive()
    print(f"rendered {pick['date']} {pick['cat_slug']} buildid={meta['buildid']} archive={n}")


if __name__ == "__main__":
    main()
