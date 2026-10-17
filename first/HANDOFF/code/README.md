# code/ · 模块地图

> 31 个文件，按职责分七层。**要改什么就改哪一层**，不要跨层乱改 —— 层与层之间是单向依赖。

---

## 1. 桌面应用层（本包的门面）

| 文件 | 职责 |
|---|---|
| **`app_api.py`** | **桌面应用后端**：`127.0.0.1:8790`，平台增删改查 + 异步采集 + 反推荐开关。契约见 `../02_接口契约.md` |

它不重写采集逻辑，只做参数映射（`_build_cmd()`）—— 命令行、向导、接口三条路走**同一份**采集代码，
不会出现「界面上能采、命令行采不到」的分裂。

## 2. 采集层（把内容读进来）

| 文件 | 职责 |
|---|---|
| `collect_account.py` | 账号主页 → 库（GitHub API / 抖音 / 微博，三种认证） |
| `collect_zhihu.py` | 知乎：热榜 / 推荐流 / 按议题 / 按关键词 → 过滤 → 正文 → 立场 → 省流 → 库 |
| `collect_github.py` | GitHub 新项目 → 读 README → 省流 → 库 + 日报（**只做收集**） |
| `build_vault.py` | 一批链接 → 库（抓正文 / 判观点类 / 省流） |
| `vault_fetch.py` | **只给 URL 也能用**：站点适配 → 通用容器 → 分块打分，抓不到就明确失败 |
| `browser_cdp.py` | 极简 CDP 客户端（起浏览器 / 执行 JS / 取 Cookie / 落盘登录态），零依赖 |

## 3. 清洗层（去掉不该进来的东西）

| 文件 | 职责 |
|---|---|
| `junk_filter.py` | 四层判定：广告 / 盐选小说 / 网文（≥2 特征）/ 种草软文（≥2 特征）/ 空壳 → `{drop, junk, reason}` |
| `summarize.py` | 中文省流（客观转述 + 要点），**prompt 明确禁止判立场** |

## 4. 反引擎（核心：库条目 → 反库条目）

依赖方向：`run_vault` → `antipode_engine` → (`stance_extractor`, `antipode_sources`, `passage_selector`, `retrieval_terms`, `model_adapter`)

| 文件 | 职责 | 改动影响 |
|---|---|---|
| `stance_extractor.py` | 模块 0：议题 + 立场抽取（4 分类）+ 证据校验。**`ISSUE_TAXONOMY` 在这里** | 改 prompt 会作废 90.2% 基线 |
| `retrieval_terms.py` | 议题 → 检索词表；`TOPIC_GUARD` 拦同形异义；`NO_RETRIEVAL` 排除梗类 | 加议题时要同步改这里 |
| `antipode_sources.py` | 模块 1：检索适配器（V2EX / 掘金 / CSDN / GitHub / HN）。**加源 = 加一个函数** | 独立可测 |
| `passage_selector.py` | 模块 2：立场段落粗筛（省 token，压缩比 36%） | 影响成本，不影响正确性 |
| `model_adapter.py` | 模型适配层（OpenAI 兼容 / 批量 JSONL / 本地文件），**模型无关** | 换模型只改 `.env` |
| `antipode_engine.py` | 引擎：闸门 + 打分 + 产出反库条目（带漏斗报告） | 六道闸门在这里 |
| `run_vault.py` | 批量编排：一个库 → 一批反库条目 + `.stats.json` | 断点续跑、`--dry-run`、`--offline` |

## 5. 接口层（给队友 / 前端）

| 文件 | 职责 |
|---|---|
| `server.py` | `127.0.0.1:8787`，`POST /antipode`（一条 → 一条反库条目）、`/antipode/batch`、`/issues`。**只读、无状态** |

## 6. 自检与演示

| 文件 | 职责 |
|---|---|
| `selftest.py` | 全链路自检 42 项（语法/依赖/词表/离线链路/HTTP/产物），10 秒 |
| `try_me.py` | 向导版入口：轮询地址栏读到主页 → 建库。配合 `../试一试.bat`（在 `first/` 下） |
| `run_demo.py` | 三个议题一次跑完（离线演示） |
| `make_demo_page.py` | 生成 `docs/反库演示.html`（双栏、单文件、断网可用） |
| `make_defense_page.py` | 生成 `docs/答辩看板.html`（数字直接读实测产物，不手抄） |

## 7. 数据与配置

| 文件 | 说明 |
|---|---|
| `gold_set.jsonl` / `gold_hard.jsonl` | 立场抽取**评测集**（41 条），**不是训练集** |
| `.env.example` | 密钥模板（复制成 `.env`，`.env` 永不入库） |
| `requirements.txt` | 可选依赖（核心链路只用标准库） |
| `antipode_item.example.json` / `vault.example.json` / `vault_item.example.json` | 字段样例 |
| `cookies.example.json` / `label_map.example.json` | Cookie 串与批量标签文件的格式样例 |
| `check_api_key.js` | 小工具：验证密钥能用（不参与链路） |

---

## 8. 加一个新平台要动哪几处（清单）

1. `app_api.PLATFORMS` 加一条（`label` / `auth` / `antipode` / `antipode_note`）
2. `app_api._build_cmd()` 加一个分支（平台 → 采集命令）
3. 新建 `collect_<平台>.py`（照 `collect_zhihu.py` 抄骨架：取列表 → 过滤 → 取正文 → 判立场 → 写库）
4. `selftest.check_collect()` 里补参数检查（防止命令行与接口的字段慢慢漂移）

**不需要动**：反引擎、检索层、接口返回结构 —— 它们只认「库条目」这个统一结构。

## 9. 加一个新议题要动哪几处（清单）

1. `stance_extractor.ISSUE_TAXONOMY` 加一条：`issue_id` / `side_a` / `side_b` / 描述
2. `retrieval_terms.ISSUE_TERMS[issue_id]` 加中文检索词（两侧各若干）
3. 若有同形异义的坑，在 `retrieval_terms.TOPIC_GUARD` 加拦截规则
4. 是梗/无检索价值的，放进 `NO_RETRIEVAL`
5. `python code/selftest.py` 必须仍然全绿（它会检查议题表完整性）
