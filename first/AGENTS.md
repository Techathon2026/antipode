# AGENTS.md —— 在这个文件夹里干活之前先读这一页

你是接手「反仓库（Antipode）」这一侧的编码 agent。
**`first/` 就是全部**：自包含、可运行、没有第二份副本。改这里就是改全部。

---

## 0. 60 秒上手（按顺序做，别跳）

```bash
python code/selftest.py        # 期望：✅ 全部 42 项通过（10 秒，不联网、不花钱）
python code/quickstart.py      # 起后端 127.0.0.1:8790，并用 data/ 里的真实库种好 3 个平台
```

再开一个终端验证它真的在给数据：

```bash
curl http://127.0.0.1:8790/health
curl "http://127.0.0.1:8790/platforms/zhihu-1017T2051/items?only=eligible"   # 9 条，每条带反信息
```

三条都通了 = 环境没问题，可以开始改代码。

---

## 1. 这个项目在做什么（一句话）

把用户关注的信息收进一个**仓库**，再自动建一个**反仓库** —— 存放他**没看见、但值得看见**的东西：
同一议题的对立面、别人的反驳证据。入口是「一个主页网址 + 账号」（GitHub / 知乎 / 抖音 / 微博）。

**现在的最终任务**：把这一层接进桌面应用 ——
左侧菜单加一栏「我的信息平台」→ 用户添加平台 → 每个平台一个库 → 点进去看条目 →
**合适**的条目带反推荐 → 反推荐可按**库**开关。

界面不归这一层写，但接口全都给好了（`02_接口契约.md`）。

---

## 2. 已经有什么（**不要重做**）

| 能力 | 入口文件 | 证据 |
|---|---|---|
| 桌面应用后端：平台增删改查 + 异步采集 + 反推荐开关 | `code/app_api.py` | `examples/`（真实响应） |
| 单条接口：一条 URL → 一条反库条目 | `code/server.py` | `selftest` 第 8 组 |
| 知乎采集（热榜 / 议题 / 关键词）+ 反推荐 | `code/collect_zhihu.py` | `data/vault_zhihu-1017T2051.*`：10 条 → 9 条★ → **16 条反信息** |
| GitHub 新项目采集 + README 中文省流 | `code/collect_github.py` | `data/digest_github-new-1017T2025.md`（5 条） |
| 账号主页采集（三种认证） | `code/collect_account.py` | `docs/账号采集_说明.md` |
| 反引擎：检索 → 粗筛 → 判立场 → 六道闸门 → 打分 | `code/run_vault.py` | `data/antipode_zhihu-1017T2051.jsonl`（16 条） |
| 去广告 / 盐选小说 / 种草软文 | `code/junk_filter.py` | 被上两个采集脚本调用 |
| 中文省流（禁止替用户选边） | `code/summarize.py` | 每条条目都有 `summary` |
| 全链路自检 | `code/selftest.py` | 42 项 |

`data/` 里是**真跑出来的**产物，做界面可以直接当 mock，**不需要先跑采集**（采集要登录、要等十几分钟）。

---

## 3. 接口速查（细节看 `02_接口契约.md`）

```
GET    /health                 能力清单（5 个平台，谁支持反推荐）
GET    /platforms              我加了哪些平台（首屏卡片）
POST   /platforms              添加平台 → 202/201 + job（异步采集）
GET    /platforms/{id}/items?only=all|eligible&q=   库里的信息（含反推荐）
PATCH  /platforms/{id}         开/关反推荐（按库）
POST   /platforms/{id}/refresh 重新采
DELETE /platforms/{id}         移除平台（连带删库文件）
GET    /jobs/{id}              采集进度（前端轮询 2–3 秒）
```

前端只有四个分支，照这个写就不会错：
1. `antipode_eligible=false` → 只显示摘要 + 原文链接，**不显示「看反面」**
2. `=true` 且 `antipode` 非空 → 显示反推荐卡片
3. `=true` 但 `antipode` 空 → 「暂时没找到合格的对面」（**不是错误**）
4. `synthesized=true` → **必须视觉区分**（AI 生成、无出处；默认关闭）

⚠️ 打包陷阱：**不要用 PyInstaller 打成单文件 exe** —— `app_api.py` 靠 `subprocess` 调同目录的
`collect_*.py`，冻结后 `sys.executable` 变成 exe 自身，一 spawn 就崩。
正确做法：随应用分发一个 embeddable Python + 整个 `code/` 目录（`02_接口契约.md` 第 7 节）。

---

## 4. 数据结构（三层，字段别自己发明）

```
库 Vault          vault_<id>.json        一个平台 = 一个库，id 就是 platform.id
 └ 条目 Item      vault_<id>.items.jsonl   id / title / url / summary / antipode_eligible / antipode_reason
反库 Anti Item    antipode_<id>.jsonl     独立存放，for_vault_item → 条目 id
                                          takes_side / url / evidence_span / opposition_score / synthesized
```

两个「开关」不是一回事，别合并：
- **`antipode_eligible`（条目级）**：这条内容适不适合找反面 —— 由内容决定
- **`antipode_enabled`（库级）**：这个库用户要不要开 —— 由用户决定

---

## 5. 铁律（改代码也不能破）

1. **LLM 只负责「提问」，答案必须来自真实检索。** 反库每条都要有真实 URL；抓不到就报失败，**绝不编正文或链接**。
2. **对立面是符号运算**：`takes_side == opposite(原条目.supports)`。没有「大致相反」。
3. **不可反推荐的条目也要显示，并给原因。** 「10 条里只有 1 条值得找反面」本身就是信息。
4. **开关关掉 = 不生成**（`run_vault` 直接拒绝），不是 UI 遮罩。
5. **默认关闭一切可能说谎的东西**：`synthesized`（AI 自编、无真实 URL）默认关。

---

## 6. 不要做的事

| 别做 | 原因 |
|---|---|
| 新建第二份 `code/`、或把代码复制到别处再改 | 一旦两份就会分叉；这里就是唯一副本 |
| 为了多出反信息而放松闸门 / 降低阈值 | 反库会退化成杠精池，产品的价值就没了 |
| 改 `stance_extractor` 里的 `SYSTEM_PROMPT` | 一改，90.2% 的评测基线作废；个案用 `retrieval_terms.TOPIC_GUARD` 拦 |
| 用知乎**热榜**演示反推荐 | 热榜是综合时事，与 16 个技术议题一条都不沾边，实测 10/10 被拦 —— ★=0 是正确结果 |
| 收账号密码 / 拿密码 POST 登录表单 | 三家都有验证码与设备校验，脚本必被拦；密码落盘是最贵的一类漏洞。用 `--browser` 让人自己登 |
| 把 `data/platforms.json` / `code/.env` / `.profiles/` 提交进 git | 分别是运行时状态、密钥、登录态 |
| 在接口里返回 token / cookie | `_public()` 主动剥离，别加回去 |

---

## 7. 改哪里

| 想改 | 动这里 |
|---|---|
| 加一个平台 | `code/app_api.PLATFORMS` + `_build_cmd()` + 新建 `collect_<平台>.py`（清单在 `code/README.md` 第 8 节） |
| 加一个议题 | `stance_extractor.ISSUE_TAXONOMY` + `retrieval_terms.ISSUE_TERMS`（+ `TOPIC_GUARD`）；清单在 `code/README.md` 第 9 节 |
| 改「找反面」的效果 | `code/antipode_engine.py`（闸门/打分）、`code/antipode_sources.py`（检索源）、`code/passage_selector.py`（粗筛） |
| 换模型 | 只改 `code/.env` 三个变量：`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `STANCE_MODEL`（模型无关，实测用 GLM 免费档） |
| 改省流文案 | `code/summarize.py`（**不要**让它判断立场） |

---

## 8. 验证你改对了

```bash
python code/selftest.py                     # 必须仍然 42 项全绿
python code/quickstart.py --seed-only       # 平台登记没坏
python code/antipode_engine.py --selftest   # 反引擎离线自检
python code/run_vault.py --vault data/vault_zhihu-1017T2051.json --dry-run   # 只看会处理哪些，不联网
```

要真跑一条（会联网 + 花模型额度）：

```bash
python code/selftest.py --live
```

---

## 9. 再往下读

| 文件 | 什么时候读 |
|---|---|
| `README.md` | 导航与三句话概览 |
| `01_现状与边界.md` | 想知道「哪些已经做了、哪些明确不做」——**动手前必读**，防止重做 |
| `02_接口契约.md` | 写界面 / 接线 / 打包 |
| `03_运行与验证.md` | 命令、数据文件命名、9 条故障排查 |
| `04_坑与限制.md` | 改引擎之前；答辩数字与话术 |
| `code/README.md` | 模块地图（七层依赖方向、加平台/加议题清单） |
| `data/README.md` | 哪个产物能当哪种 mock |
| `docs/` | 深层细节：账号采集、检索实测、立场抽取、流水线机制 |
