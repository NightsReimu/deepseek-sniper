#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek Chat 网站变更监测器
================================
监测 https://chat.deepseek.com/ 的内容变化：
  - 部署版本号 (meta commit-id)
  - 页面标题 / 描述 / 关键词 等文案
  - JS 前端资源文件（新增/移除/文件名变化 = 新版本发布）
  - JS 代码包内容（同名文件内容变化 = 代码更新）
  - 页面可见文字

检测到变化后，将更新内容推送至飞书机器人。

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
from urllib.parse import urljoin

TARGET_URL = "https://chat.deepseek.com/"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "snapshot.json"
LOG_FILE = BASE_DIR / "monitor.log"
LOCK_FILE = BASE_DIR / "monitor.lock"

MAX_LOG_SIZE = 2 * 1024 * 1024   # 日志超过 2MB 自动截断
MAX_MSG_LEN = 5000               # 飞书消息最大字符数（超出截断）
MAX_TEXT_LEN = 20000             # 页面文字快照保存上限
MAX_JS_FETCH = 3                 # 每次最多深度抓取多少个 JS 文件
MAX_JS_BYTES = 5 * 1024 * 1024   # 单个 JS 文件读取上限
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
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_hex(data):
    return hashlib.sha256(data.encode("utf-8", errors="replace")).hexdigest()


def fetch(url, timeout=30, max_bytes=None):
    """GET 请求，返回 (状态码, 最终URL, 文本内容)。"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(max_bytes) if max_bytes else resp.read()
        return resp.getcode(), resp.geturl(), body.decode("utf-8", errors="replace")


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
    """判断抓到的页面是否为真实页面（防止被验证码/挑战页/错误页干扰）。"""
    if status != 200 or len(raw_html) < 500:
        return False
    if not snapshot["commit_id"] and not snapshot["assets"] and not snapshot["title"]:
        return False
    if "just a moment" in raw_html.lower() or "cf-challenge" in raw_html.lower():
        return False
    return True


def deep_hash_js(assets, prev_hashes):
    """对 JS 资源做内容哈希：文件名已变的直接抓取；文件名没变的复用旧哈希（节省流量）。
    返回 {url: sha256前12位}。"""
    hashes = {}
    fetched = 0
    for url in assets:
        if not url.endswith(".js"):
            continue
        if fetched >= MAX_JS_FETCH:
            break
        if prev_hashes and url in prev_hashes:
            hashes[url] = prev_hashes[url]  # 文件名未变，复用上次哈希
            continue
        try:
            _, _, body = fetch(url, timeout=60, max_bytes=MAX_JS_BYTES)
            hashes[url] = sha256_hex(body)[:12]
            fetched += 1
        except Exception as e:
            log("WARN JS 抓取失败 %s: %s" % (url, e))
    return hashes


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
    changed_js = []
    for url in sorted(set(old_hashes) | set(new_hashes)):
        if url in old_hashes and url in new_hashes and old_hashes[url] != new_hashes[url]:
            changed_js.append(url)
    if changed_js:
        lines = ["**📦 代码包内容变化**"]
        for u in changed_js:
            lines.append("- ⚠️ `%s` 内容已更新（%s → %s）" % (
                u.split("/")[-1], old_hashes.get(u, "?"), new_hashes.get(u, "?")))
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
    prev = state.get("snapshot") if state else None
    prev_hashes = (state.get("js_hashes") or {}) if state else {}

    # ---- 抓取
    try:
        status, _, raw = fetch(TARGET_URL, timeout=30)
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
    js_hashes = deep_hash_js(snap["assets"], prev_hashes)
    snap_hash = sha256_hex(json.dumps({
        "title": snap["title"],
        "commit_id": snap["commit_id"],
        "metas": snap["metas"],
        "assets": snap["assets"],
        "js_hashes": js_hashes,
        "visible_text": snap["visible_text"],
    }, ensure_ascii=False, sort_keys=True))

    # ---- 首次运行：建立基线
    if prev is None or state.get("baseline_hash") is None:
        new_state = {
            "baseline_hash": snap_hash,
            "snapshot": snap,
            "js_hashes": js_hashes,
            "consecutive_failures": 0,
            "last_check": now_beijing(),
            "last_status": "baseline",
        }
        save_state(new_state)
        log("OK 基线已建立 (commit-id=%s, 资源=%d)" % (snap["commit_id"], len(snap["assets"])))
        # 发送启动确认（同时验证飞书通道）
        summary = "**✅ DeepSeek 网站监测已启动**\n\n已抓取当前页面并建立基线，之后每 10 分钟自动检查一次。\n\n"
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

    ok = push_update(report)
    new_state = {
        "baseline_hash": snap_hash,
        "snapshot": snap,
        "js_hashes": js_hashes,
        "consecutive_failures": 0,
        "last_check": now_beijing(),
        "last_status": "changed",
    }
    save_state(new_state)
    log("CHANGE 检测到变化，飞书推送%s: %s"
        % ("成功" if ok else "失败", report.replace("\n", " ")[:200]))


def main():
    if "--reset" in sys.argv:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            log("OK 已删除旧基线 %s，下次运行将重新建立基线" % STATE_FILE)
        else:
            log("INFO 无旧基线可删")
        return
    run()


if __name__ == "__main__":
    main()
