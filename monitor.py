#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek Chat 网站变更监测器
================================
监测 https://chat.deepseek.com/ 的内容变化：
  - 部署版本号 (meta commit-id)
  - 页面标题 / 描述 / 关键词 等文案
  - JS 前端资源文件（新增/移除/文件名变化 = 新版本发布）
  - JS 代码包内容（同名文件内容变化 = 代码更新，每轮全量哈希验证）
  - HTML 初始响应中的文字（注：chat.deepseek.com 为 SPA，动态渲染的文字
    需执行 JS 才能获取，本程序监测的是服务端返回的静态 HTML 内容；
    如需监测渲染后文字请使用 Playwright/Chromium 方案）

检测到变化后，将更新内容推送至飞书机器人。
推送失败时变更会暂存 pending.json 并在下次运行自动补推，保证不漏报。

配置（二选一）：
  1. 环境变量: FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
  2. 配置文件: 复制 config.example.json 为 config.json，填入 feishu_webhook

用法：
  python3 monitor.py          单次检查
  python3 monitor.py --reset  删除旧基线重新建立（不推送变更通知）

依赖：Python 3.6+ 标准库（无需第三方库）
定时：每 3 分钟运行一次，cron 示例：
  */3 * * * * cd /root/deepseek-monitor && /usr/bin/python3 monitor.py >> cron.log 2>&1
"""

import difflib
import fcntl
import hashlib
import html as html_mod
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

TARGET_URL = "https://chat.deepseek.com/"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "snapshot.json"
PENDING_FILE = BASE_DIR / "pending.json"
LOG_FILE = BASE_DIR / "monitor.log"
LOCK_FILE = BASE_DIR / "monitor.lock"

MAX_LOG_SIZE = 2 * 1024 * 1024   # 日志超过 2MB 自动截断
MAX_MSG_LEN = 5000               # 飞书消息最大字符数（超出截断）
MAX_TEXT_LEN = 20000             # 页面文字快照保存上限
MAX_JS_BYTES = 5 * 1024 * 1024   # 单个 JS 文件读取上限（超限视为异常，保留旧哈希）
MAX_NEWS_KEEP = 100              # seen_news 最多保留条数

# 官方新闻公告：Docusaurus 静态站，侧边栏 News 链接指向最新新闻 /news/newsYYMMDD
NEWS_INDEX_URL = "https://api-docs.deepseek.com/news/"
NEWS_LINK_RE = re.compile(r'href="(/news/news\d+[^"]*)"')

# API 文档站监测：sitemap 页面结构变化 + 核心页面正文内容变化
API_DOCS_BASE = "https://api-docs.deepseek.com/"
API_DOCS_WATCH_PAGES = [
    "",                          # 首页（API 总览）
    "quick_start/pricing",       # 价格
    "api/create-chat-completion",  # 核心 API 参考
    "quick_start/rate_limit",    # 限流
    "quick_start/token_usage",   # Token 用量
    "guides/thinking_mode",      # 深度思考
]
BEIJING_TZ = timezone(timedelta(hours=8))

# 动态变化的 meta（每次请求都不同，必须排除，否则会误报）
DYNAMIC_META = {"ip", "region"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


# ---------------------------------------------------------------- 工具函数

def now_beijing():
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = "[%s] %s" % (now_beijing(), msg)
    print(line, flush=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE:
            # 简单截断：只保留最后 100 行
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-100:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(state):
    """原子写入状态文件：先写临时文件再 os.replace，
    防止进程被杀/断电时 JSON 写一半导致状态损坏、基线丢失。"""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(STATE_FILE))


def sha256_hex(data):
    return hashlib.sha256(data.encode("utf-8", errors="replace")).hexdigest()


def fetch(url, timeout=30, max_bytes=None, etag=None, last_modified=None):
    """GET 请求，返回 (状态码, 最终URL, 文本内容, 响应ETag, Last-Modified)。

    携带 If-None-Match / If-Modified-Since 时为条件请求：
    内容未变时服务器返回 304（此时 body 为空），用于零流量验证 JS 是否更新。
    """
    headers = dict(HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes) if max_bytes else resp.read()
            return (resp.getcode(), resp.geturl(),
                    body.decode("utf-8", errors="replace"),
                    resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, url, "", None, None
        raise


def get_webhook():
    """获取飞书机器人 Webhook：
    优先取环境变量 FEISHU_WEBHOOK，其次读取 config.json 的 feishu_webhook 字段。"""
    env = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if env:
        return env
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        wh = cfg.get("feishu_webhook", "").strip()
        if wh:
            return wh
    except Exception:
        pass
    log("ERROR 未配置飞书 Webhook：请设置环境变量 FEISHU_WEBHOOK，"
        "或创建 config.json（{\"feishu_webhook\": \"https://...\"}）")
    return ""


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
URL_RE = re.compile(r"^(https?:)?//|^data:|^[\w-]+\.(com|net|cn|org|io|dev|ai)[/.]", re.I)
UNICODE_ESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
# minified 代码里单引号字符字面量会导致字符串匹配错位，吞入代码片段；
# 用代码特征关键字把这些"伪字符串"过滤掉
CODE_FRAGMENT_RE = re.compile(
    r"!==|===|=>|\.length|lastIndexOf|indexOf\(|\breturn\b|\btypeof\b|\bif\(|"
    r"\.jsx|\bfunction\b|__DEV__|\bthrow\b|new Error|Object\.entries|"
    r"\bconst\b|\blet\b|\bvar\b|\.prototype\b")


def extract_js_strings(js_text, max_items=200, min_len=4, max_len=80):
    """从 JS bundle 中提取可读字符串常量（中文文案/英文句子/版本号）。

    用于展示"新版本具体内容"：对比新旧版本提取出的字符串集合，
    新增的文案往往就是新功能/新提示，是比 commit-id 更直观的更新信号。

    实现：轻量词法扫描而非正则——minified JS 中大量悬空引号
    （模板字符串、HTML 内嵌等）会导致正则错位匹配、吞掉真实字符串；
    逐字符扫描能严格按引号配对识别字符串字面量，模板字符串整体跳过。
    """
    found = set()
    n = len(js_text)
    cursor = 0

    def process(s):
        s = UNICODE_ESC_RE.sub(lambda x: chr(int(x.group(1), 16)), s)
        s = s.replace("\\n", " ").replace("\\t", " ").replace("\\/", "/")
        s = s.strip()
        if not (min_len <= len(s) <= max_len):
            return
        if URL_RE.match(s) or HEX_RE.match(s) or s.isdigit():
            return
        if CODE_FRAGMENT_RE.search(s):
            return  # 伪字符串：含代码特征
        # 错位匹配的伪字符串几乎都以代码符号开头/结尾；真实文案以文字或数字开头
        if not (s[0].isalnum() or CJK_RE.match(s[0])):
            return
        if not (s[-1].isalnum() or s[-1] in "。！？!?…."):
            return
        alnum = sum(c.isalnum() for c in s)
        if alnum / max(len(s), 1) < 0.5:
            return  # 符号占比过高，疑似代码
        if not CJK_RE.search(s):
            # 非中文：要求像英文句子（含空格或版本号），且字母占比高
            letters = sum(c.isalpha() for c in s)
            if letters < 6:
                return
            if not (re.search(r"\s", s) or re.search(r"\d+\.\d+", s)
                    or re.search(r"\bv\d", s, re.I)):
                return
            if letters / max(len(s), 1) < 0.6:
                return
        found.add(s)

    while True:
        # 找下一个候选引号（" / ' / `），用 C 实现的 find 加速
        i = n
        for ch in ('"', "'", "`"):
            pos = js_text.find(ch, cursor)
            if pos != -1 and pos < i:
                i = pos
                ch_found = ch
        if i >= n:
            break
        if ch_found == "`":
            # 模板字符串：整体跳过，内部引号不参与匹配
            j = i + 1
            while j < n:
                if js_text[j] == "\\":
                    j += 2
                elif js_text[j] == "`":
                    break
                else:
                    j += 1
            cursor = j + 1
            continue
        # 普通字符串字面量：配对扫描到结束引号
        quote = ch_found
        j = i + 1
        buf = []
        while j < n:
            if js_text[j] == "\\":
                buf.append(js_text[j:j + 2])
                j += 2
            elif js_text[j] == quote:
                break
            else:
                buf.append(js_text[j])
                j += 1
        process("".join(buf))
        cursor = j + 1
        if len(found) > max_items * 3:
            break  # 提前退出，避免超大 bundle 拖慢扫描
    return sorted(found)[:max_items]


def build_string_diff_report(old_set, new_set, max_items=15):
    """对比新旧版本 JS 字符串集合，生成"新版本具体内容"摘要 markdown。

    旧基线缺失（旧资源已下线）时返回 None，提示无法对比。
    """
    if not old_set:
        return None
    added = sorted(new_set - old_set, key=len, reverse=True)[:max_items]
    removed = sorted(old_set - new_set, key=len, reverse=True)[:max_items]
    if not added and not removed:
        return None
    lines = ["**🧬 新版本具体内容（提取自代码包）**"]
    if added:
        lines.append("**新增：**")
        for s in added:
            lines.append("- ➕ %s" % md_escape(s[:60]))
    if removed:
        lines.append("**移除：**")
        for s in removed:
            lines.append("- ➖ %s" % md_escape(s[:60]))
    return "\n".join(lines)


def md_escape(text):
    """转义 lark_md 中的特殊字符，避免排版错乱。"""
    text = text.replace("\\", "\\\\")
    text = text.replace("|", "\\|")
    text = text.replace("*", "\\*")
    text = text.replace("`", "\\`")
    return text


# ---------------------------------------------------------------- 内容提取

def extract_meta(html_text):
    """提取全部 meta name/property -> content 映射。"""
    metas = {}
    for m in re.finditer(r"(?is)<meta[^>]+>", html_text):
        tag = m.group(0)
        key = None
        km = re.search(r'(?is)\b(?:name|property)=["\']([^"\']+)["\']', tag)
        if km:
            key = km.group(1).strip().lower()
        cm = re.search(r'(?is)\bcontent=["\']([^"\']*)["\']', tag)
        if key and cm:
            metas[key] = html_mod.unescape(cm.group(1).strip())
    return metas


def extract_snapshot(html_text):
    """从 HTML 中提取用于对比的『快照』。"""
    # 标题
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text)
    title = html_mod.unescape(tm.group(1).strip()) if tm else ""

    # 资源文件（script src + 样式表）
    assets = set()
    for s in re.findall(r'(?i)<script[^>]+src=["\']([^"\']+)["\']', html_text):
        assets.add(s)
    for s in re.findall(r'(?i)<link[^>]+href=["\']([^"\']+\.css[^"\']*)["\']', html_text):
        assets.add(s)
    assets = sorted(urljoin(TARGET_URL, u) for u in assets)

    # 可见文字（去掉 script/style 标签，保留句子结构）
    # 注：这是 HTML 初始响应中的静态文字（SPA 动态渲染的文字需执行 JS 才能获取）
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html_text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()

    metas = extract_meta(html_text)
    # 排除动态 meta
    meta_snapshot = {k: v for k, v in metas.items() if k not in DYNAMIC_META}

    return {
        "title": title,
        "commit_id": metas.get("commit-id", ""),
        "metas": meta_snapshot,
        "assets": assets,
        "visible_text": text[:MAX_TEXT_LEN],
    }


def is_valid_page(status, snapshot, raw_html):
    """判断抓到的页面是否为真实 DeepSeek 页面（防止验证码/挑战页/错误页干扰）。

    校验顺序：
    1. 状态码与最小体积
    2. 反爬挑战特征（Cloudflare / AWS WAF）
    3. DeepSeek 品牌特征（保持宽松，避免改版时误判）
    """
    if status != 200 or len(raw_html) < 500:
        return False
    low = raw_html.lower()
    if "just a moment" in low or "cf-challenge" in low or "awswaf" in low \
            or "challenge.js" in low or "aws-waf-token" in low:
        return False
    has_marker = (
        bool(snapshot["commit_id"])
        or "deepseek" in snapshot["title"].lower()
        or "fe-static.deepseek.com" in low
        or ("<title>" in low and "deepseek" in low)
    )
    return has_marker


def deep_hash_js(assets, prev_meta):
    """对全部 JS 资源做内容哈希（每轮全量验证，保证同名文件内容变化也能检出）。

    - 优先用 ETag / Last-Modified 条件请求：内容未变 → 304，零流量复用旧哈希
    - 内容变化（200）或服务器不支持条件请求 → 全量下载重新计算哈希
    - 抓取失败 → 保留旧哈希并记录，避免单次网络抖动引发假警报
    - 返回 (hashes, bodies)：bodies 保存本轮实际下载的 JS 内容，
      供"新版本具体内容"字符串提取使用（304 时无 body）

    hashes 格式 {url: {"hash": sha256前12位, "etag": ..., "last_modified": ...}}
    """
    result = {}
    bodies = {}
    for url in assets:
        path = urlparse(url).path
        if not path.lower().endswith(".js"):
            continue  # 含 query string 的 .js?v=xxx 也能正确识别
        prev = prev_meta.get(url) or {}
        if isinstance(prev, str):
            prev = {"hash": prev}  # 兼容旧版状态文件（js_hashes 为 {url: "hash"} 格式）
        try:
            code, _, body, etag, lm = fetch(
                url, timeout=60, max_bytes=MAX_JS_BYTES,
                etag=prev.get("etag"), last_modified=prev.get("last_modified"))
            if code == 304:
                result[url] = {
                    "hash": prev.get("hash", ""),
                    "etag": etag or prev.get("etag", ""),
                    "last_modified": lm or prev.get("last_modified", ""),
                }
                continue
            result[url] = {
                "hash": sha256_hex(body)[:12],
                "etag": etag or "",
                "last_modified": lm or "",
            }
            bodies[url] = body
        except Exception as e:
            log("WARN JS 抓取失败 %s: %s" % (url, e))
            if prev:
                result[url] = prev  # 保留旧哈希，本轮跳过该文件
    return result, bodies


# ---------------------------------------------------------------- 差异报告

def split_sentences(text):
    """按中英文句号等切分成句子，供文本 diff 使用。"""
    parts = re.split(r"(?<=[。！？!?；;])|\n", text)
    sents = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 过长句子截断
        if len(p) > 120:
            p = p[:120] + "…"
        sents.append(p)
    return sents


def text_diff_report(old_text, new_text, max_items=8):
    """句子级 diff，返回 (removed, added) 两个列表。"""
    old_sents = split_sentences(old_text)
    new_sents = split_sentences(new_text)
    sm = difflib.SequenceMatcher(None, old_sents, new_sents, autojunk=False)
    removed, added = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(old_sents[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(new_sents[j1:j2])
    if len(removed) > max_items:
        removed = removed[:max_items] + ["…（共 %d 处删除）" % len(removed)]
    if len(added) > max_items:
        added = added[:max_items] + ["…（共 %d 处新增）" % len(added)]
    return removed, added


def build_change_report(old, new, old_hashes, new_hashes):
    """生成飞书 markdown 格式的变更报告。"""
    parts = []

    # 1. 部署版本
    if old["commit_id"] != new["commit_id"]:
        parts.append("**⏱ 部署版本 (commit-id)**\n- `%s` → `%s`" % (
            old["commit_id"] or "无", new["commit_id"] or "无"))

    # 2. 文案变化（标题/描述/关键词/og）
    for field, label in (("title", "标题"), ("description", "描述"),
                         ("keywords", "关键词"), ("og:title", "分享标题"),
                         ("og:description", "分享描述")):
        o, n = old["metas"].get(field, old.get(field, "")), new["metas"].get(field, new.get(field, ""))
        if field in ("title",):
            o, n = old.get("title", ""), new.get("title", "")
        if o != n:
            parts.append("**📝 %s变化**\n- 旧: %s\n- 新: %s" % (
                label, md_escape(o[:300] or "（空）"), md_escape(n[:300] or "（空）")))

    # 3. 前端资源变化
    o_set, n_set = set(old["assets"]), set(new["assets"])
    added, removed = sorted(n_set - o_set), sorted(o_set - n_set)
    if added or removed:
        lines = ["**🖥 前端资源变化**"]
        for u in added:
            lines.append("- ➕ 新增: `%s`" % u.split("/")[-1])
        for u in removed:
            lines.append("- ➖ 移除: `%s`" % u.split("/")[-1])
        parts.append("\n".join(lines))

    # 4. 代码包内容变化（同名文件内容变了）
    def _js_hash(m):
        return m.get("hash") if isinstance(m, dict) else m

    changed_js = []
    for url in sorted(set(old_hashes) | set(new_hashes)):
        if url in old_hashes and url in new_hashes \
                and _js_hash(old_hashes[url]) != _js_hash(new_hashes[url]):
            changed_js.append(url)
    if changed_js:
        lines = ["**📦 代码包内容变化**"]
        for u in changed_js:
            lines.append("- ⚠️ `%s` 内容已更新（%s → %s）" % (
                u.split("/")[-1], _js_hash(old_hashes.get(u, "")), _js_hash(new_hashes.get(u, ""))))
        parts.append("\n".join(lines))

    # 5. 页面可见文字变化
    removed_s, added_s = text_diff_report(old["visible_text"], new["visible_text"])
    if removed_s or added_s:
        lines = ["**📄 页面文字变化**"]
        for s in removed_s:
            lines.append("- 旧: %s" % md_escape(s))
        for s in added_s:
            lines.append("+ 新: %s" % md_escape(s))
        parts.append("\n".join(lines))

    if not parts:
        return None
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 官方新闻公告

def fetch_news_index():
    """抓取 DeepSeek 官方新闻索引页，返回新闻 URL 列表（按页面出现顺序去重）。"""
    try:
        status, _, raw, _, _ = fetch(NEWS_INDEX_URL, timeout=30)
        if status != 200:
            log("WARN 新闻索引页状态异常: HTTP %s" % status)
            return []
        urls = []
        for m in NEWS_LINK_RE.finditer(raw):
            u = urljoin(NEWS_INDEX_URL, m.group(1))
            if u not in urls:
                urls.append(u)
        return urls
    except Exception as e:
        log("WARN 新闻索引抓取失败: %s" % e)
        return []


def fetch_news_article(url):
    """抓取单篇新闻，返回 (标题, 正文摘要)。失败返回 ("", "")。"""
    try:
        status, _, raw, _, _ = fetch(url, timeout=30)
        if status != 200:
            return "", ""
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.S)
        title = re.sub(r"<[^>]+>", " ", h1.group(1)) if h1 else ""
        title = html_mod.unescape(re.sub(r"\s+", " ", title)).strip()
        body = re.sub(r"(?is)<(script|style|nav|footer)[^>]*>.*?</\1>", " ", raw)
        body = re.sub(r"(?is)<[^>]+>", " ", body)
        body = html_mod.unescape(re.sub(r"\s+", " ", body)).strip()
        return title, body[:400]
    except Exception as e:
        log("WARN 新闻页抓取失败 %s: %s" % (url, e))
        return "", ""


def check_news(seen):
    """检查官方新闻公告，新公告推送飞书；推送失败保留待重试。

    seen 结构: {url: {"title": str, "summary": str, "pushed": bool}}
    返回更新后的 seen（已见集合只增，超出 MAX_NEWS_KEEP 时丢弃最旧的）。
    """
    if not seen:
        # 首次部署：只记录现有新闻为已见，不推送历史（避免刷屏）
        urls = fetch_news_index()
        for u in urls:
            seen[u] = {"title": "", "summary": "", "pushed": True}
        log("OK 新闻公告基线已建立（%d 条已有公告记为已见）" % len(urls))
        return seen

    urls = fetch_news_index()
    for u in urls:
        entry = seen.get(u) or {}
        if entry.get("pushed"):
            continue  # 已推送过
        title, summary = entry.get("title"), entry.get("summary")
        if not title:
            title, summary = fetch_news_article(u)
            if not title:
                seen[u] = {"title": "", "summary": "", "pushed": False}
                continue
        markdown = ("**%s**\n\n%s\n\n🔗 %s"
                    % (md_escape(title), md_escape(summary) or "（无摘要）", u))
        ok, _ = send_feishu("📢 DeepSeek 官方新公告", markdown, template="blue")
        seen[u] = {"title": title, "summary": summary, "pushed": ok}
        log("NEWS 新公告「%s」推送%s" % (title[:40], "成功 ✅" if ok else "失败，待重试"))
    if len(seen) > MAX_NEWS_KEEP:
        for k in list(seen)[:len(seen) - MAX_NEWS_KEEP]:
            seen.pop(k, None)
    return seen


# ---------------------------------------------------------------- API 文档站

def fetch_sitemap_urls():
    """抓取 api-docs sitemap.xml，返回全部页面 URL 列表。失败返回 []。"""
    try:
        status, _, raw, _, _ = fetch(urljoin(API_DOCS_BASE, "sitemap.xml"), timeout=30)
        if status != 200:
            return []
        return re.findall(r"<loc>([^<]+)</loc>", raw)
    except Exception as e:
        log("WARN sitemap 抓取失败: %s" % e)
        return []


def fetch_doc_page_text(url):
    """抓取文档页正文纯文本（Docusaurus SSR 的 <article> 区域）。失败返回 ""。"""
    try:
        status, _, raw, _, _ = fetch(url, timeout=30)
        if status != 200:
            return ""
        m = re.search(r"(?is)<article[^>]*>(.*?)</article>", raw)
        body = m.group(1) if m else raw
        body = re.sub(r"(?is)<(script|style|nav)[^>]*>.*?</\1>", " ", body)
        body = re.sub(r"(?is)<[^>]+>", " ", body)
        return html_mod.unescape(re.sub(r"\s+", " ", body)).strip()
    except Exception as e:
        log("WARN 文档页抓取失败 %s: %s" % (url, e))
        return ""


def doc_text_diff(old_text, new_text, max_items=8):
    """文档正文句子级 diff，返回 (removed, added)。"""
    old_sents = split_sentences(old_text)
    new_sents = split_sentences(new_text)
    sm = difflib.SequenceMatcher(None, old_sents, new_sents, autojunk=False)
    removed, added = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(old_sents[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(new_sents[j1:j2])
    return (removed[:max_items], added[:max_items],
            len(removed) > max_items or len(added) > max_items)


def check_api_docs(api_state):
    """监测 API 文档站：
    1. sitemap 页面结构变化（新增/移除文档页，含新新闻页）
    2. 核心页面正文内容变化（句子级 diff 推送）
    推送失败不更新基线，下次运行自动重试。首次运行只建立基线。

    api_state 结构: {"sitemap": [url], "pages": {url: {"hash": str, "text": str}}}
    返回更新后的 api_state。
    """
    if not api_state:
        api_state = {"sitemap": [], "pages": {}}

    # ---- 1. 页面结构变化
    urls = fetch_sitemap_urls()
    if urls:
        prev_urls = set(api_state.get("sitemap") or [])
        cur_urls = set(urls)
        if prev_urls:
            added = sorted(cur_urls - prev_urls)
            removed = sorted(prev_urls - cur_urls)
            for u in added:
                if "/news/" in u:
                    title, summary = fetch_news_article(u)
                    markdown = ("**%s**\n\n%s\n\n🔗 %s"
                                % (md_escape(title) or u, md_escape(summary) or "（无摘要）", u))
                else:
                    markdown = "📄 **新增文档页面：** `%s`" % u.replace(API_DOCS_BASE, "/")
                ok, _ = send_feishu("📄 DeepSeek API 文档更新", markdown, template="blue")
                log("APIDOCS 新增页面 %s 推送%s" % (u, "成功 ✅" if ok else "失败，待重试"))
            for u in removed:
                markdown = "🗑 **移除文档页面：** `%s`" % u.replace(API_DOCS_BASE, "/")
                ok, _ = send_feishu("📄 DeepSeek API 文档更新", markdown, template="orange")
                log("APIDOCS 移除页面 %s 推送%s" % (u, "成功 ✅" if ok else "失败，待重试"))
        api_state["sitemap"] = urls  # 推送失败也更新（页面结构变化是事实，避免重复刷屏）

    # ---- 2. 核心页面正文变化
    pages = api_state.setdefault("pages", {})
    for rel in API_DOCS_WATCH_PAGES:
        url = urljoin(API_DOCS_BASE, rel)
        text = fetch_doc_page_text(url)
        if not text:
            continue  # 抓取失败跳过，保留旧基线
        h = sha256_hex(text)
        prev = pages.get(url)
        if prev and prev.get("hash") != h:
            removed, added, truncated = doc_text_diff(prev.get("text", ""), text)
            lines = ["**%s**" % md_escape(url.replace(API_DOCS_BASE, "/"))]
            for s in removed:
                lines.append("- 旧: %s" % md_escape(s[:80]))
            for s in added:
                lines.append("+ 新: %s" % md_escape(s[:80]))
            if truncated:
                lines.append("…（差异较大，已截断）")
            ok, _ = send_feishu("📝 API 文档内容更新", "\n".join(lines), template="blue")
            log("APIDOCS 内容变化 %s 推送%s" % (url, "成功 ✅" if ok else "失败，待重试"))
            if ok:
                pages[url] = {"hash": h, "text": text}  # 推送成功才更新基线
        elif prev is None:
            pages[url] = {"hash": h, "text": text}  # 首次记录
    return api_state


# ---------------------------------------------------------------- 飞书推送

def send_feishu(title, markdown, template="blue"):
    """发送飞书互动卡片消息，返回 (ok, resp_text)。"""
    webhook = get_webhook()
    if not webhook:
        return False, "webhook not configured"
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": markdown}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": "🕐 %s | 来源: %s" % (now_beijing(), TARGET_URL)},
                ]},
            ],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8", errors="replace"))
        ok = result.get("code") == 0 or result.get("StatusCode") == 0
        if not ok:
            log("WARN 飞书推送失败: %s" % json.dumps(result, ensure_ascii=False))
        return ok, json.dumps(result, ensure_ascii=False)
    except Exception as e:
        log("ERROR 飞书推送异常: %s" % e)
        return False, str(e)


def push_update(report):
    """推送变更通知（带截断保护）。"""
    if len(report) > MAX_MSG_LEN:
        report = report[:MAX_MSG_LEN] + "\n\n…（内容过长已截断，完整记录见服务器日志）"
    ok, _ = send_feishu("🚨 DeepSeek 网站检测到更新", report, template="red")
    return ok


# ---------------------------------------------------------------- 主流程

def acquire_lock():
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        log("WARN 已有实例在运行，本次跳过")
        return None


def run():
    lock = acquire_lock()
    if lock is None:
        return
    try:
        do_run()
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception:
            pass
        lock.close()


def do_run():
    state = load_state()

    # ---- 优先补推未送达的变更（监测与消息投递解耦）
    # 上一轮推送失败时，变更已暂存 pending.json；补推成功后才应用其基线，
    # 保证「检测到的更新」永远不会因飞书临时故障而丢失。
    if PENDING_FILE.exists():
        try:
            pending = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        except Exception:
            log("WARN pending.json 损坏，删除后继续")
            PENDING_FILE.unlink()
            pending = None
        if pending:
            ok, _ = send_feishu("🚨 DeepSeek 网站检测到更新（补推）",
                                pending["report"], template="red")
            if not ok:
                log("ERROR 补推失败，保留 pending.json，下次运行继续重试")
                return
            state = {
                "baseline_hash": pending["baseline_hash"],
                "snapshot": pending["snapshot"],
                "js_hashes": pending["js_hashes"],
                "js_strings": pending.get("js_strings") or {},
                "consecutive_failures": 0,
                "last_check": now_beijing(),
                "last_status": "changed",
            }
            save_state(state)
            PENDING_FILE.unlink()
            log("OK 补推成功，已应用暂存变更的基线，继续正常检查")

    # ---- 检查官方新闻公告（独立于页面监测，出错不影响主流程）
    if state is None:
        state = {}  # 首次运行（无基线）时初始化
    state["seen_news"] = check_news(state.get("seen_news") or {})
    # ---- 检查 API 文档站（sitemap 页面结构 + 核心页面正文）
    state["api_docs"] = check_api_docs(state.get("api_docs") or {})

    prev = state.get("snapshot") if state else None
    prev_hashes = (state.get("js_hashes") or {}) if state else {}

    # ---- 抓取
    try:
        status, _, raw, _, _ = fetch(TARGET_URL, timeout=30)
    except urllib.error.HTTPError as e:
        status = e.code
        raw = ""
    except Exception as e:
        log("ERROR 抓取失败: %s" % e)
        status = 0
        raw = ""

    if not is_valid_page(status, extract_snapshot(raw) if raw else {}, raw):
        # 抓取失败 / 疑似挑战页
        failures = (state.get("consecutive_failures") or 0) + 1 if state else 1
        new_state = {
            "snapshot": (prev if prev else None),
            "js_hashes": prev_hashes,
            "consecutive_failures": failures,
            "last_check": now_beijing(),
            "last_status": "fetch_failed" if status != 200 else "invalid_page",
        }
        save_state(new_state)
        log("FAIL 抓取失败(status=%s)，连续失败 %d 次" % (status, failures))
        if failures == 1:
            push_update("**⚠️ DeepSeek 监测告警**\n\n网站无法访问或返回异常页面（HTTP %s）。\n\n"
                        "若持续出现，可能是站点反爬/维护中，程序会自动重试。" % status)
        return

    # ---- 解析快照
    snap = extract_snapshot(raw)
    # 每轮全量验证全部 JS 内容（ETag 条件请求省流量），js_hashes 集合恒定，
    # snap_hash 不会因哈希集合数量变化而漂移
    js_hashes, js_bodies = deep_hash_js(snap["assets"], prev_hashes)
    snap_hash = sha256_hex(json.dumps({
        "title": snap["title"],
        "commit_id": snap["commit_id"],
        "metas": snap["metas"],
        "assets": snap["assets"],
        "js_hashes": {u: v["hash"] for u, v in js_hashes.items()},
        "visible_text": snap["visible_text"],
    }, ensure_ascii=False, sort_keys=True))

    # ---- 首次运行：建立基线
    if prev is None or state.get("baseline_hash") is None:
        # 基线轮提取 JS 字符串并入库，作为下次变化的对比基准
        js_strings = {url: extract_js_strings(body)
                      for url, body in js_bodies.items()}
        new_state = {
            "baseline_hash": snap_hash,
            "snapshot": snap,
            "js_hashes": js_hashes,
            "js_strings": js_strings,
            "seen_news": state.get("seen_news") or {},
            "api_docs": state.get("api_docs") or {},
            "consecutive_failures": 0,
            "last_check": now_beijing(),
            "last_status": "baseline",
        }
        save_state(new_state)
        log("OK 基线已建立 (commit-id=%s, 资源=%d, 提取文案%d条)"
            % (snap["commit_id"], len(snap["assets"]),
               sum(len(v) for v in js_strings.values())))
        # 发送启动确认（同时验证飞书通道）
        summary = "**✅ DeepSeek 网站监测已启动**\n\n已抓取当前页面并建立基线，之后每 3 分钟自动检查一次。\n\n"
        summary += "- 部署版本 (commit-id): `%s`\n" % (snap["commit_id"] or "无")
        summary += "- 前端资源: %d 个 JS/CSS 文件\n" % len(snap["assets"])
        summary += "- 页面标题: %s\n" % (snap["title"] or "无")
        summary += "- 检测范围: 版本号 / 文案 / 前端资源 / 代码包内容 / 页面文字\n"
        summary += "\n> 发现任何变化将立即推送本群。"
        send_feishu("🔍 DeepSeek 网站监测已启动", summary, template="green")
        return

    # ---- 对比
    prev_hash = state.get("baseline_hash", "")
    if snap_hash == prev_hash:
        new_state = dict(state)
        new_state.update({
            # 写回本轮 js_hashes（新格式，含 ETag 元数据）：
            # 1) 完成旧格式迁移 2) 下一轮即可用 ETag 条件请求，零流量验证 JS
            "js_hashes": js_hashes,
            "consecutive_failures": 0,
            "last_check": now_beijing(),
            "last_status": "ok",
        })
        save_state(new_state)
        log("OK 无变化 (commit-id=%s)" % snap["commit_id"])
        return

    # ---- 检测到变化
    report = build_change_report(state["snapshot"], snap, prev_hashes, js_hashes)
    if not report:
        log("WARN 哈希变化但未生成差异报告")
        report = "页面发生变化，但未能提取出可读差异，请人工查看网站。"

    # ---- 提取"新版本具体内容"：对比新旧 JS 中的字符串常量（文案/提示语/功能名）
    stored_strings = state.get("js_strings") or {}
    new_js_strings = dict(stored_strings)
    for url, body in js_bodies.items():
        new_js_strings[url] = extract_js_strings(body)
    old_set = set()
    for v in stored_strings.values():
        old_set |= set(v)
    # 已移除的旧 JS 可能还在 CDN 上，尝试下载提取旧版本字符串用于对比
    removed_urls = [u for u in prev.get("assets", [])
                    if u not in snap["assets"]
                    and urlparse(u).path.lower().endswith(".js")]
    for u in removed_urls:
        new_js_strings.pop(u, None)
        try:
            _, _, body, _, _ = fetch(u, timeout=30, max_bytes=MAX_JS_BYTES)
            old_set |= set(extract_js_strings(body))
        except Exception:
            pass  # 旧文件已下线，无法对比，忽略
    new_set = set()
    for v in new_js_strings.values():
        new_set |= set(v)
    string_report = build_string_diff_report(old_set, new_set)
    if string_report:
        report = report + "\n\n" + string_report

    ok = push_update(report)
    if not ok:
        # 推送失败：绝不更新基线！变更暂存 pending.json，下次运行优先补推，
        # 避免「飞书网络异常 → 基线已更新 → 更新永久丢失」的漏报。
        pending = {
            "report": report,
            "snapshot": snap,
            "js_hashes": js_hashes,
            "js_strings": new_js_strings,
            "baseline_hash": snap_hash,
        }
        tmp = PENDING_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(pending, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(PENDING_FILE))
        log("ERROR 飞书推送失败，变更已暂存 pending.json，保留旧基线待重试: %s"
            % report.replace("\n", " ")[:200])
        return

    new_state = {
        "baseline_hash": snap_hash,
        "snapshot": snap,
        "js_hashes": js_hashes,
        "js_strings": new_js_strings,
        "seen_news": state.get("seen_news") or {},
        "api_docs": state.get("api_docs") or {},
        "consecutive_failures": 0,
        "last_check": now_beijing(),
        "last_status": "changed",
    }
    save_state(new_state)
    log("CHANGE 检测到变化，飞书推送成功: %s" % report.replace("\n", " ")[:200])


def main():
    if "--reset" in sys.argv:
        for f in (STATE_FILE, PENDING_FILE):
            if f.exists():
                f.unlink()
                log("OK 已删除 %s，下次运行将重新建立基线" % f)
            else:
                log("INFO 无 %s 可删" % f.name)
        return
    run()


if __name__ == "__main__":
    main()
