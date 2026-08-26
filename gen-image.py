#!/usr/bin/env python3
"""生成当日配图 —— 0 token，但按张计费。

读 content.json 的 art.main/art.sub，拼 prompt → 调 wan2.7-image → 下载到 docs/img/。
把解析出的文件名写回 content.json 的 art.*.file，好让 render.py 与
「data/content/<date>.json 可 0 token 重渲全站」都能拿到路径。

三条硬约束（改动前必读，每条都对应一类真实故障）：
  1. 任何失败都 exit 0。配图是增益不是依赖，图挂了不该让当天停更。
  2. 落盘文件已存在则跳过，绝不重复调 API。补跑窗口每天多跑 1–3 次 daily，
     不幂等的话每天被扣 2–4 次费。
  3. 只允许往 content.json 里新增 art.*.file / art.*.status，不得改任何其它字段
     —— selfcheck.py 是信任边界，它已经过了，本脚本不能绕过它篡改内容。

用法：
  ./gen-image.py                  正常：读 content.json + pick.json
  ./gen-image.py --dry-run        只打印将要发送的 prompt，不联网、不花钱
  ./gen-image.py --force          忽略已存在的文件，强制重新生成（会计费）
"""
import argparse, json, os, sys, time, urllib.error, urllib.request
import lib

TIMEOUT   = int(os.environ.get("IMG_TIMEOUT", "120"))
MAX_BYTES = int(os.environ.get("IMG_MAX_BYTES", str(8 * 1024 * 1024)))
MODEL     = os.environ.get("IMG_MODEL", "wan2.7-image")
APIKEY    = os.environ.get("IMG_API_KEY", "")
ENABLED   = os.environ.get("IMG_ON", "1") not in ("0", "", "false", "off")

# 每类固定画风 —— 0 token，且改全站观感只需改这张表。
# 关键一条：只有 geo 允许写实摄影。其余类目一律绘画语汇，
# 因为摄影术（1839）之前的题材若渲染成照片，读者会当成史料证据。
STYLE = {
    "event":   "古典油画质感，低饱和，侧光，可见颜料肌理，无文字",
    "person":  "单色淡彩速写，铅笔轮廓，大量留白，人物不作正面特写，无文字",
    "geo":     "自然纪录片式写实摄影，自然光，广角，地貌细节清晰",
    "science": "19世纪科学插画，铜版线刻，米白纸底，器物结构清晰，无文字标注",
    "culture": "博物馆图录摄影，柔和均匀打光，深色纯背景，器物居中",
    "bio":     "博物学手绘图谱，奥杜邦风格，米白底，无背景杂物",
    "china":   "绢本设色，水墨淡彩，散点透视，留白，无题字无印章",
}
NEGATIVE = ("文字, 汉字, 字母, 铭文, 题字, 印章, 水印, 签名, 现代物品, 手表, 眼镜, "
            "拉链, 塑料, 畸变的手, 多余手指, 面部扭曲, 过饱和, HDR, 廉价CG感, 低分辨率")
RATIO = {"main": "16:9", "sub": "4:3"}

# magic bytes → 扩展名。不靠 Content-Type，也不假定 .jpg：
# 接口返回一段 JSON 错误体时，若盲信扩展名就会得到一个「打不开的 .jpg」，
# 而页面只会显示裂图，日志全绿。
MAGIC = [(b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"),
         (b"RIFF", "webp"), (b"GIF8", "gif")]


def sniff(b):
    for sig, ext in MAGIC:
        if b.startswith(sig):
            return "webp" if ext == "webp" and b[8:12] != b"WEBP" and False else ext
    return None


def build_prompt(subject, slug):
    return f"{subject}。{STYLE.get(slug, STYLE['event'])}"


# ╔═════════════════ 需要按 I1 文档实现，只改这个函数 ═════════════════╗
def call_model(prompt, ratio):
    """提交一次生成请求，返回 (图片URL 或 None, 状态串)。

    契约（务必遵守，框外代码依赖它）：
      - 成功 → (url_str, "ok")
      - 任何失败 → (None, "<简短状态>")，**不要抛异常，不要 sys.exit**
    I1 实测结论（2026-08-26 已用真实请求验证）：
      - endpoint: POST {base}/compatible-mode/v1/chat/completions，Bearer 鉴权
      - 同步调用；messages 的 content 必须是 parts 列表，纯字符串会 400
      - 返回 output.choices[0].message.content[] 中 type=image 的项，其 "image" 为图片 URL
      - ratio 参数不支持（顶层 size 被忽略，恒定输出 2048*2048）→ 不发送
      - 无独立 negative prompt 参数 → 负向词并入正向 prompt 末尾
    """
    if not APIKEY:
        return None, "no_key"
    endpoint = os.environ.get(
        "IMG_ENDPOINT",
        "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions")
    text = f"{prompt}。避免以下元素：{NEGATIVE}"
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {APIKEY}",
                 "Content-Type": "application/json",
                 "User-Agent": "wiki-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, f"http_{e.code}"
        if e.code == 429:
            return None, "http_429_quota"
        return None, f"http_{e.code}"
    except TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, f"req_{type(e).__name__}"
    try:
        parts = d["output"]["choices"][0]["message"]["content"]
        for p in parts if isinstance(parts, list) else []:
            if isinstance(p, dict) and p.get("type") == "image" and p.get("image"):
                return p["image"], "ok"
    except (KeyError, IndexError, TypeError):
        pass
    code = d.get("code")
    if code:
        return None, f"api_{code}"
    return None, "bad_response"
# ╚═══════════════════════════════════════════════════════════════════╝


def fetch(url):
    """下载并按 magic bytes 判定真实格式。返回 (bytes, ext) 或 (None, 状态)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wiki-bot/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return None, f"dl_http_{e.code}"
    except Exception as e:
        return None, f"dl_{type(e).__name__}"
    if len(data) > MAX_BYTES:
        return None, "dl_too_large"
    if len(data) < 1024:
        return None, "dl_too_small"
    ext = sniff(data)
    if not ext:
        return None, "dl_not_an_image"      # 大概率是 JSON 错误体
    return data, ext


def one(kind, subject, slug, date, force):
    """返回 (相对路径 或 "", 状态)。相对路径形如 ../img/2026-08-26-main.jpg"""
    outdir = os.path.join(lib.ROOT, "docs", "img")
    os.makedirs(outdir, exist_ok=True)
    if not force:
        for ext in ("jpg", "png", "webp", "gif"):
            p = os.path.join(outdir, f"{date}-{kind}.{ext}")
            if os.path.exists(p) and os.path.getsize(p) > 1024:
                return f"../img/{date}-{kind}.{ext}", "cached"

    prompt = build_prompt(subject, slug)
    url, st = call_model(prompt, RATIO.get(kind, "1:1"))
    if not url:
        return "", st
    data, ext = fetch(url)
    if data is None:
        return "", ext                       # 此时 ext 是状态串
    dest = os.path.join(outdir, f"{date}-{kind}.{ext}")
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)                    # 原子落盘，防半截文件被 git 提交
    return f"../img/{date}-{kind}.{ext}", f"ok_{len(data)//1024}kb"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    cpath = os.path.join(lib.ROOT, "content.json")
    ppath = os.path.join(lib.ROOT, "pick.json")
    if not os.path.exists(cpath):
        print("skip: 无 content.json"); return
    content = json.load(open(cpath, encoding="utf-8"))
    pick = json.load(open(ppath, encoding="utf-8")) if os.path.exists(ppath) else {}
    slug, date = pick.get("cat_slug", "event"), pick.get("date", "0000-00-00")

    art = content.get("art") or {}
    if not art:
        print("skip: content.json 无 art 字段（模型未产出配图描述）"); return
    if not ENABLED:
        print("skip: IMG_ON=0"); return

    changed, report = False, []
    for kind in ("main", "sub"):
        node = art.get(kind)
        if not isinstance(node, dict) or not (node.get("subject") or "").strip():
            report.append(f"{kind}=no_subject"); continue
        if a.dry_run:
            print(f"--- {kind} ({RATIO.get(kind)}) ---")
            print(build_prompt(node['subject'].strip(), slug))
            print(f"negative: {NEGATIVE}\n")
            continue
        rel, st = one(kind, node["subject"].strip(), slug, date, a.force)
        # 只新增这两个键，不动任何既有字段
        if node.get("file") != rel or node.get("status") != st:
            node["file"], node["status"] = rel, st
            changed = True
        report.append(f"{kind}={st}")

    if a.dry_run:
        return
    if changed:
        json.dump(content, open(cpath, "w", encoding="utf-8"), ensure_ascii=False)
    tot = sum(os.path.getsize(os.path.join(lib.ROOT, "docs", "img", f))
              for f in os.listdir(os.path.join(lib.ROOT, "docs", "img"))
              if not f.endswith(".part")) if os.path.isdir(
                  os.path.join(lib.ROOT, "docs", "img")) else 0
    print(f"gen-image {date} {slug}: {' '.join(report)} · docs/img 累计 {tot/1048576:.1f}MB")


if __name__ == "__main__":
    main()
