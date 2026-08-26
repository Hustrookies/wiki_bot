#!/usr/bin/env python3
"""离线预抓取事实锚 —— 在能访问 dumps.wikimedia.org 的机器上跑，不在 ECS 上跑。

为什么这样做：中文维基的 API 和网站在中国大陆被阻断（TCP 握手被丢弃），但
dumps.wikimedia.org 可达且支持 HTTP Range。multistream 格式的索引给出每个条目
所在 bz2 流的字节偏移，于是可以只取那一段（~300KB）而不必下载 3.4GB 全量。

产物 data/material.json 提交进 git，ECS 运行时零网络依赖。

用法：
  ./fetch-material.py --limit 5      先验 5 条，看提取质量
  ./fetch-material.py                抓 queue.tsv 里所有还没有的 subject（增量）
  ./fetch-material.py --force 张骞   强制重抓某条
  ./fetch-material.py --stat         看当前覆盖率
"""
import argparse, bisect, bz2, json, os, re, sys, time, urllib.error, urllib.request
from html import unescape as html_unescape
import lib

BASE  = "https://dumps.wikimedia.org/zhwiki/latest/"
INDEX = "zhwiki-latest-pages-articles-multistream-index.txt.bz2"
DATA  = "zhwiki-latest-pages-articles-multistream.xml.bz2"
CACHE = os.path.join(lib.ROOT, ".cache")           # gitignored
MPATH = os.path.join(lib.ROOT, "data", "material.json")
MAXLEN = 600
UA = {"User-Agent": "wiki-bot/1.0 (personal daily digest)"}


def get(url, rng=None, timeout=90):
    req = urllib.request.Request(url, headers=dict(UA))
    if rng:
        req.add_header("Range", f"bytes={rng[0]}-{rng[1]}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download_resumable(url, dest, chunk=262144):
    """分块流式下载 + 断点续传 + 进度。

    dumps.wikimedia.org 对大文件限速到 ~35KB/s，41MB 要跑 ~19 分钟。所以必须
    边下边落盘（否则中断就全丢）并支持 Range 续传。
    """
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    total = None
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=dict(UA), method="HEAD"), timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0) or None
    except Exception:
        pass
    tot_s = f"{total/1048576:.0f}MB" if total else "未知大小"
    print(f"下载 {os.path.basename(url)}（{tot_s}，已有 {have/1048576:.1f}MB）", flush=True)

    stall = 0
    while total is None or have < total:
        req = urllib.request.Request(url, headers=dict(UA))
        if have:
            req.add_header("Range", f"bytes={have}-")
        got0 = have
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "ab") as f:
                while True:
                    b = r.read(chunk)
                    if not b:
                        break
                    f.write(b)
                    have += len(b)
                    el = time.time() - t0
                    if el > 0 and have % (chunk * 8) < chunk:
                        rate = (have - got0) / el
                        eta = ((total - have) / rate) if (total and rate > 0) else 0
                        print(f"  {have/1048576:6.1f}/{tot_s}  {rate/1024:5.1f}KB/s  "
                              f"剩 {eta/60:4.1f}min", flush=True)
        except Exception as e:
            print(f"  中断（{type(e).__name__}），5 秒后续传…", flush=True)
            time.sleep(5)
        if have == got0:
            stall += 1
            if stall >= 5:
                raise RuntimeError(f"连续 5 次无进展，已下 {have} 字节，稍后重跑可续传")
        else:
            stall = 0
        if total is None:
            break
    os.replace(part, dest)
    print(f"  完成 {have/1048576:.1f}MB", flush=True)


# ---------------- 索引 ----------------
def load_index():
    """下载并解压 41MB 全量索引，缓存在 .cache/。返回 ({title:(offset,pageid)}, 有序偏移表)。"""
    os.makedirs(CACHE, exist_ok=True)
    raw = os.path.join(CACHE, INDEX)
    txt = raw[:-4]
    if not os.path.exists(txt):
        if not os.path.exists(raw):
            download_resumable(BASE + INDEX, raw)
        print("解压索引…", flush=True)
        with open(txt, "wb") as f:
            f.write(bz2.decompress(open(raw, "rb").read()))
    print("载入索引…", flush=True)
    idx, offs = {}, set()
    with open(txt, encoding="utf-8", errors="ignore") as f:
        for ln in f:
            p = ln.rstrip("\n").split(":", 2)
            if len(p) != 3:
                continue
            o = int(p[0])
            offs.add(o)
            idx[p[2]] = (o, int(p[1]))
    print(f"  索引 {len(idx)} 条，{len(offs)} 个流")
    return idx, sorted(offs)


def stream_end(offs, start):
    i = bisect.bisect_right(offs, start)
    return offs[i] - 1 if i < len(offs) else None


# ---------------- 取文 ----------------
_streams = {}   # 同一个流里通常有 100 个条目，缓存能省掉大量重复 Range 请求

def fetch_stream(offs, start):
    if start in _streams:
        return _streams[start]
    end = stream_end(offs, start)
    rng = (start, end) if end else (start, start + 2_000_000)
    blob = get(BASE + DATA, rng=rng)
    try:
        xml = bz2.decompress(blob).decode("utf-8", "ignore")
    except Exception:
        d = bz2.BZ2Decompressor()          # 末尾流可能被 Range 截断
        try:
            xml = d.decompress(blob).decode("utf-8", "ignore")
        except Exception:
            return ""
    if len(_streams) > 40:
        _streams.clear()
    _streams[start] = xml
    return xml


def extract(xml, title):
    m = re.search(r"<title>" + re.escape(title) + r"</title>(.*?)</page>", xml, re.S)
    if not m:
        return None
    t = re.search(r"<text[^>]*>(.*?)</text>", m.group(1), re.S)
    return t.group(1) if t else None


REDIR = re.compile(r"^\s*#(?:REDIRECT|重定向|重定向至)\s*\[\[([^\]|#]+)", re.I)


def _strip_nested(s, open_, close, keep_last_field=False):
    """用计数扫描删除成对嵌套结构。正则做不到这件事 —— {{Infobox}} 跨多行且内含
    嵌套模板与 [[链接]]，`\\{\\{[^{}]*\\}\\}` 那种写法永远匹配不上，图片说明里的
    嵌套 [[ ]] 也会让 `[^\\]]*` 提前收尾。

    keep_last_field=True 时保留 | 之后的最后一段（用于 [[目标|显示文字]] → 显示文字）。
    """
    out, i, n, ol, cl = [], 0, len(s), len(open_), len(close)
    while i < n:
        if s.startswith(open_, i):
            depth, j, start = 1, i + ol, i + ol
            while j < n and depth:
                if s.startswith(open_, j):
                    depth += 1; j += ol
                elif s.startswith(close, j):
                    depth -= 1; j += cl
                else:
                    j += 1
            inner = s[start: j - cl] if depth == 0 else s[start:]
            if keep_last_field:
                # 只对不含冒号前缀（File:/Image: 等）的内链保留显示文字
                head = inner.split("|")[0]
                if re.match(r"^\s*(?:File|Image|文件|檔案|图像|圖像|Category|分类|分類)\s*:", head, re.I):
                    pass                        # 整块丢掉
                else:
                    out.append(_strip_nested(inner.split("|")[-1], open_, close, True))
            i = j
        else:
            out.append(s[i]); i += 1
    return "".join(out)


_ZH = re.compile(r"-\{(.*?)\}-", re.S)

def _zh_variant(w, prefer=("zh-hans", "zh-cn", "zh-sg", "zh-hant", "zh-tw", "zh-hk")):
    """处理 zhwiki 的繁简转换语法 -{zh-hans:X; zh-hant:Y;}-，优先取简体变体。
    不处理的话事实锚里会出现一长串 `-{zh-hant:…; zh-hans:…;}-` 原文。"""
    def one(mo):
        body = mo.group(1)
        if ":" not in body:
            return body                      # -{纯文本}- 保护标记，去壳即可
        parts = {}
        for seg in body.split(";"):
            if ":" in seg:
                k, _, v = seg.partition(":")
                parts[k.strip().lower()] = v.strip()
        for k in prefer:
            if parts.get(k):
                return parts[k]
        return next((v for v in parts.values() if v), "")
    prev = None
    while prev != w:                         # 可能嵌套
        prev = w
        w = _ZH.sub(one, w)
    return w


def clean(w):
    """wikitext → 纯文本。事实锚只要陈述句，模板/表格/引用/图注全部丢掉。"""
    # ① 先反转义：dump 里的 XML 是实体化的，不做这步 <ref> 之类永远匹配不到，
    #    &lt;ref&gt; 会原样进事实锚，比没有锚更糟。个别页面双重转义，所以做两遍。
    for _ in range(2):
        prev = w
        w = html_unescape(w)
        if w == prev:
            break

    w = re.sub(r"<!--.*?-->", "", w, flags=re.S)
    w = re.sub(r"<ref[^>]*/\s*>", "", w, flags=re.I)
    w = re.sub(r"<ref[^>]*>.*?</ref\s*>", "", w, flags=re.S | re.I)
    w = re.sub(r"<(math|score|syntaxhighlight|gallery|timeline|imagemap)[^>]*>.*?</\1\s*>",
               "", w, flags=re.S | re.I)

    # ② 表格、模板、链接：一律用计数扫描，不用正则
    w = re.sub(r"^\s*\{\|.*?^\s*\|\}", "", w, flags=re.S | re.M)   # 表格（行首锚定）
    w = _strip_nested(w, "{{", "}}")                                # 模板全删
    w = _zh_variant(w)                                              # -{zh-hans:…}- 繁简转换语法
    w = _strip_nested(w, "[[", "]]", keep_last_field=True)          # 内链留显示文字
    w = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", w)            # 外链留文字
    w = re.sub(r"\[https?://\S+\]", "", w)
    w = re.sub(r"https?://\S+", "", w)

    w = re.sub(r"'''''|'''|''", "", w)
    w = re.sub(r"<[^>]+>", "", w)                                   # 残余 HTML 标签
    w = re.sub(r"^\s*[*#:;].*$", "", w, flags=re.M)                 # 列表行
    w = re.sub(r"^\s*=+.*?=+\s*$", "", w, flags=re.M)               # 小节标题

    # ③ 清掉模板被剥离后留下的空壳：「智慧之家（），」这种
    for _ in range(3):
        prev = w
        w = re.sub(r"（\s*[，、；\s]*\s*）|\(\s*[,;\s]*\s*\)|「\s*」|《\s*》|\[\s*\]", "", w)
        w = re.sub(r"，\s*，", "，", w)
        if w == prev:
            break
    w = w.replace(" ", " ").replace("&nbsp;", " ")
    w = re.sub(r"[ \t]+", " ", w)
    w = re.sub(r"\n{2,}", "\n", w)
    w = re.sub(r"^[\s，。、；：]+", "", w)
    return w.strip()


def resolve(idx, offs, title, depth=0):
    """取正文，跟随最多 2 次重定向。

    zhwiki 常把条目存在繁体标题下，简体标题只是 #REDIRECT。不跟随的话会拿到
    一个非空的垃圾串（"#REDIRECT 張騫"）当事实锚 —— 能通过所有非空检查。
    """
    if depth > 2:
        return None, "redirect_loop"
    if title not in idx:
        return None, ("not_in_index" if depth == 0 else "redirect_target_missing")
    off, _ = idx[title]
    xml = fetch_stream(offs, off)
    raw = extract(xml, title)
    if raw is None:
        return None, "not_in_stream"
    r = REDIR.match(raw)
    if r:
        return resolve(idx, offs, r.group(1).strip(), depth + 1)
    txt = clean(raw)
    if len(txt) < 40:
        return None, "too_short"
    return txt[:MAXLEN], ("ok" if depth == 0 else "ok_via_redirect")


# ---------------- 入口 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="append", default=[])
    ap.add_argument("--stat", action="store_true")
    a = ap.parse_args()

    mat = json.load(open(MPATH, encoding="utf-8")) if os.path.exists(MPATH) else {}
    # 用 wiki 列查维基（真实条目名），用 subject 列做 material.json 的键
    pairs, seen = [], set()
    for q in lib.load_queue():
        if q["subject"] in seen:
            continue
        seen.add(q["subject"])
        pairs.append((q["subject"], q["wiki"]))
    subs = [p[0] for p in pairs]
    WIKI = dict(pairs)

    if a.stat:
        hit = [s for s in subs if mat.get(s, {}).get("text")]
        print(f"覆盖率 {len(hit)}/{len(subs)} = {len(hit)*100//max(1,len(subs))}%\n")
        for s in subs:
            e = mat.get(s)
            print(f"  {'✓' if (e and e.get('text')) else '✗'} {s:18} "
                  f"{(e.get('status') if e else '未抓取'):24} {(e.get('len') if e else 0)} 字")
        return

    todo = [s for s in subs if s in a.force or s not in mat]
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        print("没有需要抓取的 subject（--force <subject> 可强制重抓）")
        return
    print(f"待抓 {len(todo)} 条\n")

    idx, offs = load_index()
    ok = 0
    for i, s in enumerate(todo, 1):
        wk = WIKI.get(s, "")
        if not wk:
            mat[s] = {"text": "", "status": "no_wiki_title", "len": 0}
            print(f"[{i}/{len(todo)}] {s:18} {'no_wiki_title（明确无锚）':22}")
            continue
        try:
            txt, st = resolve(idx, offs, wk)
        except urllib.error.URLError as e:
            txt, st = None, f"unreachable:{e.reason}"
        except Exception as e:
            txt, st = None, f"error:{type(e).__name__}"
        mat[s] = {"text": txt or "", "status": st, "len": len(txt or "")}
        ok += 1 if txt else 0
        print(f"[{i}/{len(todo)}] {s:18} {st:22} {len(txt or ''):4} 字")
        if txt:
            print(f"          {txt[:110]}")
        time.sleep(0.4)          # 对公共 dump 服务器客气一点

    os.makedirs(os.path.dirname(MPATH), exist_ok=True)
    json.dump(mat, open(MPATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\n本轮成功 {ok}/{len(todo)}，已写入 data/material.json")
    print("提醒：material.json 必须提交进 git —— ECS 运行时只读它，不联网。")


if __name__ == "__main__":
    main()
