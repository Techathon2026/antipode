# 拼接说明 · first/ 怎么并进整个产品

> 给**拼接的人**看，不是给读代码的人看。
> `first/` 是一个能独立跑起来的「反仓库服务」：喂它库条目，它还你反库条目。
> 拼的时候**不需要读它的实现**，只要按下面的接口喂数据、接结果。

---

## 1. 边界在哪

```
first/（本目录，两段都在这里）                      second/（队友）
──────────────────────────────────                ─────────────────
①  链接 / 账号 → 抓正文 → 判立场 → 省流 → 建库
                     │
                     ▼
②          库条目 → 推对立面 → 检索 → 六道闸门 → 写反库       兴趣筛选 / 分类模型
                     │
                     └── vault item（至少要 url）可以来自 second/
                     └── antipode item（必有真实 URL）交回拼接层
```

一句话划清：**「把信息收进库」和「给库里的东西找反面」归 first/；「判断用户对什么感兴趣」归 second/**。

变化说明：早前采集归队友，现在归我们 —— 客户给的是"一个网址 + 账号"，
必须自己登进去读，所以 `collect_account.py` / `browser_cdp.py` 放在这一侧。
如果队友那一侧已经有库，直接用第 2 节的三种接法喂进来，两段互不影响。

---

## 2. 三种接法，按集成成本从低到高

### 2.1 HTTP（推荐：跨语言、跨进程）

```bash
cd first
python code/server.py --port 8787          # 只监听 127.0.0.1
```

```bash
curl -s -X POST http://127.0.0.1:8787/antipode \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://blog.csdn.net/fudaihb/article/details/136061101"}'
```

返回里 `outcome` 决定前端怎么画，`antipode[]` 直接渲染。字段表见
[`docs/接口契约_仓库到反仓库.md`](docs/接口契约_仓库到反仓库.md) §7。

### 2.2 Python 导入（同一个进程）

```python
import sys; sys.path.insert(0, "first/code")
from antipode_engine import build_antipode_items

items, funnel = build_antipode_items(
    {"url": "https://example.com/post", "issue_id": "microservices_vs_monolith", "supports": "微服务"},
    {"vault_id": "my-vault", "antipode_enabled": True},
    verbose=False,
)
# items: list[dict]，每条都是一个反库条目；funnel: 漏斗账目（可画图）
```

异常是**语义**，不是故障：中立 / 未开启 / 抓不到正文都会抛 `RuntimeError`，
消息本身就是给用户看的中文原因。

### 2.3 文件（离线 / 批量 / 只想看结果）

```bash
cd first
python code/run_vault.py --vault data/vault_demo.json          # → data/antipode_<vault_id>.jsonl
```

输入 `data/*.items.jsonl`（一行一个条目），输出 `data/*.jsonl`（一行一个反库条目）+ `.stats.json`（漏斗体检）。

---

## 3. 输入：队友最少要给什么

| 字段 | 必需 | 说明 |
|---|---|---|
| `url` | **是**（或给 `full_text`） | 原文章地址。first/ 自己去取正文 |
| `full_text` | 否 | 已经取到正文就传，省一次抓取；**比 summary 可靠** |
| `summary` | 否 | 只能用来定位议题，**判立场必须用全文**（省流版会把作者态度抹平） |
| `issue_id` | 否 | 给了就省一次模型调用；不给会自己判 |
| `supports` | 否 | 同上，作者站哪一边 |

最小可用请求就是 `{"url": "..."}` —— 2026-10-17 已实测（7 条只给链接，2 条产出反信息）。

---

## 4. 输出：每条反库条目保证什么

| 字段 | 保证 |
|---|---|
| `url` | **一定有，且可点开**（第三档 AI 自编默认关闭，开时必打 `synthesized: true`） |
| `evidence_span` | **一定是候选原文的真实子串**（逐字校验过，不是"意思差不多"） |
| `takes_side` | 一定是原条目的**符号对立面**（`opposite(supports)`），不是"感觉相反" |
| `opposition_score` | 对立度，阈值 0.7 以上才会出现 |
| `found_by.stage` | `origin`（原站）/ `global`（全网）/ `synthesized`（自编，默认关） |
| `for_vault_item` | 指回库里那条，前端做左右对照就靠它 |

**两条铁律，拼接时不要绕开**：没有真实 URL 的不要；没有原文证据的不要。

---

## 5. 依赖与环境

- Python **3.10+**（用了 `dict | None` 这类语法）
- **零第三方依赖**：核心链路只用标准库，任何机器 `git clone` 就能跑
- 只有在真调模型时需要 `openai`（函数内延迟导入，不装也能跑离线自检）
- 三个环境变量（写 `first/code/.env`）：`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `STANCE_MODEL`
- 可选：`ANTIPODE_NO_PROXY=1` —— 本机代理把请求挡成 502 时用

---

## 6. 拼接时最容易踩的五个坑

| 坑 | 症状 | 正确做法 |
|---|---|---|
| 只传 `summary` 不传全文 | 判立场漂移、`neutral` 变多 | 传 `full_text` 或只传 `url` 让它自己抓 |
| 用 `ok` 判断有没有反信息 | `ok:true` 却是空的 | 看 `outcome`（`produced` 才是有） |
| 当成同步接口 | 前端转圈两分钟 | 单条 60–120 秒，必须异步任务 + 轮询 |
| 库没开开关 | 一直返回 `disabled` | `vault.antipode_enabled` 设 `true` |
| 期待"每条都有反面" | 觉得系统没用 | 找不到**是结论**（中立/议题不在表内/候选全被闸门拒），强行凑数才是错 |

---

## 7. 拼接验收（按顺序做一遍）

```bash
cd first
python code/selftest.py                       # ① 15 项全绿，10 秒
python code/run_vault.py --vault <队友的库>.json --dry-run   # ② 看会处理哪些条目
python code/run_vault.py --vault <队友的库>.json --limit 3   # ③ 拿真实数据跑 3 条，人工看对不对
```

- [ ] ① 全绿
- [ ] ② 没有被误判成"不可处理"的条目
- [ ] ③ 抽 1 条点开原文，确认 `evidence_span` 真的在里面
- [ ] 前端按 `outcome` 分支渲染，`why` 直接展示给用户
- [ ] **反库单独一个库/面板**，不混进仓库列表（产品卖点就是"仓库之外还有一层"）

---

## 8. 这个目录里都是什么

| 路径 | 干什么 | 拼接时要不要动 |
|---|---|---|
| `code/antipode_engine.py` | 引擎主流程（库条目 → 反库条目） | 不动 |
| `code/server.py` | HTTP 接口层 | 直接起服务 |
| `code/run_vault.py` | 批量编排（断点续跑、体检报告） | 跑批用 |
| `code/vault_fetch.py` | 从链接取正文 | 不动 |
| `code/collect_account.py` | **账号 + 网址 → 库**（GitHub API / 浏览器登录） | 换平台时改选择器 |
| `code/browser_cdp.py` | 极简 CDP：起浏览器、执行 JS、取 Cookie | 不动 |
| `code/build_vault.py` | 一批链接 → 库（采集层的下游） | 不动 |
| `code/stance_extractor.py` | 立场抽取 + 证据校验 | 不动（有评测基线） |
| `code/retrieval_terms.py` | 议题 → 检索词 / 议题守卫 | 加议题时改这里 |
| `code/antipode_sources.py` | 检索适配器（V2EX / 掘金 / CSDN / GitHub） | 加检索源时改这里 |
| `code/selftest.py` | 全链路自检 | 随时可跑 |
| `data/` | 库与反库样例（JSONL） | 换成真实库 |
| `docs/` | 设计与实测文档 | 参考，尤其接口契约 |
| `deck/` | 5 分钟路演 PPT + 讲稿 | 演示用 |

---

## 9. 如果队友也要往仓库里放东西

- 各自一个顶层目录：`first/`（反仓库侧）、`second/`（仓库侧），**不要互相改**
- 拼接层只碰两处：**调接口**和**渲染 outcome**
- 要新增议题：改 `retrieval_terms.ISSUE_TERMS` + `stance_extractor.ISSUE_TAXONOMY`，
  然后跑 `python code/selftest.py` 确认两侧词都配齐了
