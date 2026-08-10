# 🎯 DeepSeek Sniper · 深度求索狙击手

> **实时监控 `chat.deepseek.com` 的一举一动——版本发布、文案变动、代码更新，3 分钟之内，飞书群里见分晓。** 🚀

<p align="center">
  <img src="https://readme-typing-svg.demolab.com?font=Fira+Code&size=22&duration=3500&pause=500&color=4D6BFE&center=true&vCenter=true&width=600&lines=%F0%9F%94%AB+DeepSeek+%E7%BD%91%E7%AB%99%E5%8F%98%E6%9B%B4%E7%9B%91%E6%B5%8B%E5%99%A8;%E6%AF%8F+3+%E5%88%86%E9%92%9F%E4%B8%80%E6%AC%A1%E5%B7%A1%E6%9F%A5+%C2%B7+%E5%8F%91%E7%8E%B0%E5%8F%98%E5%8C%96%E7%AB%8B%E5%8D%B3%E6%8E%A8%E9%80%81+%E2%9A%A1;%E7%BA%AF+Python+%E6%A0%87%E5%87%86%E5%BA%93+%C2%B7+%E9%9B%B6%E4%BE%9D%E8%B5%96+%C2%B7+%E4%B8%80%E8%A1%8C%E5%91%BD%E4%BB%A4%E9%83%A8%E7%BD%B2+%F0%9F%9A%80" alt="typing-svg">
</p>

<p align="center">
  <a href="https://github.com/NightsReimu/deepseek-sniper"><img src="https://img.shields.io/github/stars/NightsReimu/deepseek-sniper?style=for-the-badge&logo=github&logoColor=white&color=4D6BFE" alt="GitHub stars"></a>
  <a href="https://github.com/NightsReimu/deepseek-sniper/blob/main/LICENSE"><img src="https://img.shields.io/github/license/NightsReimu/deepseek-sniper?style=for-the-badge&color=green" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.6%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/纯标准库-零依赖-FF6B6B?style=for-the-badge" alt="零依赖">
  <img src="https://img.shields.io/badge/检查频率-3%20分钟-orange?style=for-the-badge" alt="频率">
  <img src="https://img.shields.io/badge/推送-飞书机器人-3370FF?style=for-the-badge&logo=lark&logoColor=white" alt="飞书">
</p>

<p align="center">
  <strong>🎯 狙击手就位 · 静候 DeepSeek 更新 · 一击即中，飞书报捷</strong>
</p>

---

## 📡 它能干什么？

`deepseek-sniper` 是一把架在 `chat.deepseek.com` 门口的**狙击枪**。它每 3 分钟开一枪（探一枪），一旦发现网站有任何风吹草动——**版本发布、文案修改、前端代码更新**——立刻把差异报告**精准投递到你的飞书群**。

```
        🎯 瞄准：chat.deepseek.com
        🔫 射速：每 3 分钟
        📨 弹着点：飞书机器人
        💥 命中即推送：版本号 / 文案 / 资源 / 代码 / 文字 五大目标
```

## 🕵️ 监测信号 · 五大目标

DeepSeek 的前端页面自带「指纹」，我们利用这些特征精准识别每一次更新：

| 🎯 目标 | 📡 信号源 | 🔍 变化特征 | 说明 |
|---|---|---|---|
| **部署版本号** | 页面 `<meta name="commit-id">` | `a610cb2` → `04c4137` | ⭐ 最强信号：每次代码发布必变，直接暴露 git commit |
| **页面文案** | title / description / keywords / og 标签 | 标题、描述改写 | 营销文案、SEO 调整都能捕捉 |
| **前端资源** | JS/CSS 文件名（内容哈希命名） | `main.7863ea53ee.js` → `main.cd9b6ed73e.js` | 文件名变化 = 全新构建产物上线 |
| **代码包内容** | JS 文件内容 SHA-256 | 同名文件哈希变化 | 文件名没变但代码改了，照样抓出来（**每轮全量哈希**，ETag 条件请求省流量） |
| **页面文字** | 渲染可见文本 | 句子级 diff | 展示「删了什么、加了什么」逐句对比 |

### 🧠 智能防误报

- ✅ 自动剔除动态 meta（`ip`、`region` 等每次请求都变的字段）
- ✅ 页面被反爬/验证码拦截时，跳过对比、不误报，恢复后自动继续
- ✅ JS 哈希缓存：文件名没变就不重复下载大文件，省流量省带宽

## 🚀 快速开始

### 1️⃣ 获取飞书机器人 Webhook

飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook 地址

### 2️⃣ 配置

**方式 A：环境变量**
```bash
export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/你的Webhook"
```

**方式 B：配置文件**
```bash
cp config.example.json config.json
# 编辑 config.json，填入你的 Webhook
```

### 3️⃣ 运行

```bash
# 手动检查一次（首次运行会自动建立基线并推送启动确认）
python3 monitor.py

# 运行结果示例
# [2026-08-10 16:33:32] CHANGE 检测到变化，飞书推送成功: **⏱ 部署版本** - `a610cb2` → `04c4137` ...

# 重置基线（重新建立快照，不推送变更通知）
python3 monitor.py --reset
```

### 4️⃣ 部署为定时任务（推荐 3 分钟）

```bash
# 编辑 crontab
crontab -e

# 加入这一行
*/3 * * * * cd /path/to/deepseek-sniper && /usr/bin/python3 monitor.py >> cron.log 2>&1
```

> 💡 仅需 **Python 3.6+ 标准库**，无需 `pip install` 任何东西，任何 VPS / 云服务器 / 树莓派都能跑。

## 🧩 工作原理

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│ 抓取页面 HTML │ ──▶ │ 提取五类信号   │ ──▶ │ 与基线哈希对比 │ ──▶ │ 生成差异报告   │
└─────────────┘     └──────────────┘     └─────────────┘     └──────┬───────┘
        │                    │                    │                 │
        ▼                    ▼                    ▼                 ▼
  commit-id/meta        JS 深度哈希           snapshot.json      飞书互动卡片
  /资源列表/文字         (内容指纹)             (上次快照)          (红色告警)
```

1. **抓取**：请求首页 HTML，解析 commit-id、meta 文案、资源文件列表、静态文字
2. **指纹**：每轮对**全部** JS 做哈希验证（携带 ETag/Last-Modified 条件请求：内容未变 → 304 零流量复用；内容变化 → 下载重新计算哈希，同名文件更新必检出）
3. **对比**：与 `snapshot.json` 中的基线做哈希比对（状态文件**原子写入**，断电不损坏）
4. **推送**：有变化 → 生成 markdown 差异报告 → 飞书互动卡片推送（红色模板）
5. **可靠投递**：推送失败**绝不更新基线**，变更暂存 `pending.json`，下次运行自动补推，保证不漏报
6. **容错**：抓取失败自动计数，首次失败推送告警，恢复后继续运行

### 📨 飞书推送效果

| 场景 | 推送样式 |
|---|---|
| 🚀 监测启动 | 绿色卡片：基线信息 + 监测范围 |
| 🚨 检测到更新 | 红色卡片：版本号 / 资源 / 文案 / 文字逐项 diff |
| ⚠️ 网站异常 | 橙色卡片：无法访问 / 反爬拦截告警 |

## 📂 项目结构

```
deepseek-sniper/
├── monitor.py           # 🎯 核心监测程序（单文件，纯标准库）
├── config.example.json  # 📋 配置模板（复制为 config.json 使用）
├── snapshot.json        # 💾 基线快照（自动生成，无需手动管理）
├── monitor.log          # 📜 运行日志（超 2MB 自动截断）
└── README.md            # 📖 本文件
```

## ⚙️ 高级配置

| 环境变量 / 配置项 | 说明 | 默认值 |
|---|---|---|
| `feishu_webhook` | 飞书机器人 Webhook | 必填 |
| `target_url` | 监测目标地址 | `https://chat.deepseek.com/` |
| `MAX_JS_FETCH` | 每次深度哈希的 JS 文件数 | 3 |
| `MAX_MSG_LEN` | 飞书消息最大长度（超长截断） | 5000 |
| `MAX_TEXT_LEN` | 页面文字快照上限 | 20000 |

## 🧪 测试

内置冒烟测试（无需网络 / 无需飞书），覆盖核心逻辑：

```bash
python3 tests/test_smoke.py
```

| 用例 | 验证内容 |
|---|---|
| `test_query_string_js` | `.js?v=123` 这类带 query string 的资源正确识别 |
| `test_is_valid_page` | 真实页面通过 / AWS WAF 挑战页、错误页、无特征页全部拒绝 |
| `test_save_state_atomic` | 状态文件原子写入，无 `.tmp` 残留 |
| `test_no_drift_across_runs` | **P0 回归**：网站无变化时 snap_hash 不漂移（JS 哈希每轮全量） |
| `test_fail_keeps_baseline_then_retry` | **P0 回归**：推送失败保留基线 + 恢复后自动补推 |

## 🛠️ 技术栈

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.6%2B-3776AB?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/标准库-urllib/difflib/hashlib/re-4D6BFE?style=flat-square" alt="stdlib">
  <img src="https://img.shields.io/badge/部署-cron%20%2B%20systemd-8A2BE2?style=flat-square" alt="deploy">
  <img src="https://img.shields.io/badge/推送-飞书互动卡片-3370FF?style=flat-square&logo=lark" alt="feishu">
</p>

- 🐍 Python 标准库全家桶：`urllib` 抓取 · `hashlib` 指纹 · `difflib` 句子级 diff · `fcntl` 互斥锁
- 🚫 零第三方依赖，零 `pip install`
- 🔒 并发安全：`flock` 文件锁，cron 重叠执行自动跳过
- 📦 单文件交付：一个 `.py` 拷走即用

## 🤝 贡献

欢迎 PR / Issue / Star ⭐！

## 📜 License

[MIT](LICENSE)

---

<p align="center">
  <sub>⚠️ 本项目仅用于学习与信息获取，请遵守目标网站的服务条款与 robots 协议，合理控制请求频率。</sub>
</p>

<p align="center">
  <sub>Made with 💙 & 🔫 by <a href="https://github.com/NightsReimu">NightsReimu</a></sub>
</p>
