#!/usr/bin/env python3
"""共享工具：类目表、相似度、年代解析、数据读写。

去重相似度不用 SQLite FTS5：其 trigram 分词器要求检索词 ≥3 字符，
两字中文词（张骞/西域/靖难/汉朝）一律零命中且不报错 —— 会静默失效。
100 条量级下纯 Python 字符二元组更准，且不依赖 SQLite 版本。
"""
import hashlib, json, os, re, unicodedata

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------- 类目表：星期 → (slug, 显示名, 该类目应填的 motif 字段) ----------
# 星期用 ISO 编号（1=周一 … 7=周日）
CATS = {
    1: ("event",   "世界历史",    "timeline"),
    2: ("person",  "历史人物",    "span"),
    3: ("geo",     "自然地理",    "layers"),
    4: ("science", "科学与技术史", "contrast"),
    5: ("culture", "文明与文化",  "artifact"),
    6: ("bio",     "生物与自然",  "tradeoff"),
    7: ("china",   "中国历史",    "span"),
}
SLUG2CAT = {v[0]: (k, v[1], v[2]) for k, v in CATS.items()}

# ISO 周数 → 地域倾向，强行打散欧美偏斜
REGIONS = ["欧洲", "东亚", "南亚·中东", "非洲", "美洲", "海洋·极地·跨区域"]

THEME_COLOR = {
    "event": "#1f3352", "person": "#f7f4ef", "geo": "#33564a", "science": "#1b4a7a",
    "culture": "#f5f4f9", "bio": "#f2f5ef", "china": "#faf7f0",
}

# ---------- 相似度 ----------
_PUNCT = re.compile(r"[\s，。、；：！？「」『』《》（）()·,.:;!?\"'\-—…]+")

def bigrams(s):
    """中文字符二元组。单字退化为自身。"""
    s = _PUNCT.sub("", (s or ""))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}

def jaccard(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / len(A | B) if (A | B) else 0.0

def ent_overlap(a, b):
    """实体重叠率，按较短一侧归一 —— 「张骞」vs「张骞+丝绸之路+西汉」应算高度重叠。"""
    A, B = {x for x in (a or []) if x}, {x for x in (b or []) if x}
    return len(A & B) / min(len(A), len(B)) if (A and B) else 0.0

def sim(new, old):
    """new/old 均为 dict(title, summary, entities)。返回 0–1。

    实测（见手册 §7 验收）：文本二元组对真重复只有 0.15 量级、区分度不足，
    实体重叠才是主信号。所以取两者较大值，且 entities 质量直接决定去重成败。
    """
    t = jaccard((new.get("title") or "") + (new.get("summary") or ""),
                (old.get("title") or "") + (old.get("summary") or ""))
    e = ent_overlap(new.get("entities"), old.get("entities"))
    return max(t, e)

HARD = 0.60   # ≥ 自动跳过，不消耗当天的推送
SOFT = 0.34   # ≥ 交给模型判定 / 避免内容重叠

# ---------- 年代解析 ----------
def parse_year(s):
    """'前164'→-164  '约前164'→-164  '1398年'→1398  '前2世纪'→-150。失败返回 None。"""
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", str(s)).strip()
    neg = ("前" in s and "公元前" in s) or s.startswith("前") or "前" in s.split("世纪")[0]
    neg = neg or s.lstrip("约~约 ").startswith("-")
    m = re.search(r"\d+", s)
    if not m:
        return None
    v = int(m.group())
    if "世纪" in s:                      # 世纪取中点
        v = v * 100 - 50
    return -v if neg else v

def ruler_pct(a, b, mark):
    """mark 在 a→b 区间的百分比，钳到 4–96 保证标记点不跑出尺外。"""
    ya, yb, ym = parse_year(a), parse_year(b), parse_year(mark)
    if None in (ya, yb, ym) or yb == ya:
        return 50
    return max(4, min(96, round((ym - ya) * 100.0 / (yb - ya))))

# ---------- 数据读写 ----------
def jsonl_path():  return os.path.join(ROOT, "data", "posts.jsonl")
def queue_path():  return os.path.join(ROOT, "data", "queue.tsv")

def load_posts():
    """读 data/posts.jsonl（真相）。按 (date,subject) 去重，兜住 merge=union 可能的重复行。"""
    p, seen, out = jsonl_path(), set(), []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            k = (d.get("date"), d.get("subject"))
            if k in seen:
                continue
            seen.add(k)
            out.append(d)
    return out

def load_queue():
    """读 data/queue.tsv。列：cat  region  title  subject  entities(|分隔)  note"""
    p, out = queue_path(), []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for i, ln in enumerate(f):
            ln = ln.rstrip("\n")
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            c = ln.split("\t")
            if len(c) < 4:
                continue
            c += [""] * (7 - len(c))
            subject = c[3].strip()
            ents = [x.strip() for x in c[4].split("|") if x.strip()]
            # 结构性保证 subject ∈ entities。实体重叠是去重主信号，subject 缺席会直接
            # 削弱去重；而且 agent 会照抄这一对，不修在这里就会在 selfcheck 处炸掉一天的推送。
            if subject and subject not in ents:
                ents.insert(0, subject)
            out.append({
                "row": i + 1, "cat": c[0].strip(), "region": c[1].strip(),
                "title": c[2].strip(), "subject": subject,
                "entities": ents, "note": c[5].strip(),
                # 第7列 wiki：维基条目的真实标题，与 subject 分开。
                # 两者要求相反：subject 要专指（去重键），wiki 要能匹配到条目名
                # （zhwiki 多以繁体存储，且译名常与简体习惯不同）。留空则不抓锚。
                "wiki": c[6].strip(),
            })
    return out

def buildid(date, content_bytes):
    """内容派生、无 nonce —— 否则同一份内容重渲染字节不同，空 commit 跳过就永远不触发。"""
    return f"{date}-{hashlib.sha1(content_bytes).hexdigest()[:8]}"

def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
