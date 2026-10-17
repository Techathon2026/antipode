# data/ · 真实产物（可直接当界面 mock 数据）

> 这里全部是**真跑出来的**，不是手工编的样例。
> 用它做界面，就不必先跑采集（采集要登录、要等十几分钟）。

## 命名规则

```
vault_<id>.json            库元信息（topic / antipode_enabled / 条目数）
vault_<id>.items.jsonl     库条目（一行一条）
antipode_<id>.jsonl        反库条目（一行一条，for_vault_item 指向库条目）
antipode_<id>.stats.json   反引擎体检报告（漏斗 / 拒绝原因 / 耗时）
vault_<id>.junk.jsonl      被过滤掉的垃圾（带原因；空文件 = 这次没有垃圾）
digest_<id>.md             给人看的日报（GitHub 那条线）
```

`<id>` 必须与 `platform.id` 一致，否则 `app_api.py` 找不到文件。

## 该看哪一个

| 文件 | 是什么 | 建议用途 |
|---|---|---|
| **`vault_zhihu-1017T2051.items.jsonl`** | 知乎搜「微服务」10 条：9 条 ★、1 条判错被丢弃（`stance_dropped` 里有完整原因） | **列表页主数据** |
| **`antipode_zhihu-1017T2051.jsonl`** | 16 条真实反信息，每条都有 url + evidence_span | **反推荐卡片主数据** |
| **`antipode_zhihu-1017T2051.stats.json`** | 10 条各自的漏斗（候选 34 → 粗筛 8 → 通过 1–2）与耗时 | 「查看详情」展开用 |
| **`vault_github-new-1017T2025.items.jsonl`** | GitHub 最近 7 天新项目 5 条，README 中文省流 | **「不支持反推荐」分支** |
| `digest_github-new-1017T2025.md` | 同上的日报版（人读的 Markdown，每条带原文地址） | 演示「给你一份今天能读的」 |
| `vault_zhihu-1017T2107.*` | 同一议题再采一次（只做收集、未跑反推荐） | 对照 / 空反库状态 |
| `vault_demo.json` / `vault_demo.items.jsonl` | 早期演示库（9 条，多议题） | 多议题卡片铺量 |
| `antipode_demo.jsonl`（6 条）/ `antipode_demo-vault.jsonl`（6 条） | 早期演示反库 | 复古样式参考，字段结构同上 |
| `vault_url_only.*` / `antipode_url-only.*` | 「只给 URL」的最小用例 | 验证 `build_vault` 那条路 |
| `vault_zhihu-*.junk.jsonl` | 空文件（这次没有垃圾被过滤） | 空状态展示 |

## 两个坑

1. **`.stats.json` 里的 `title` 没剥离知乎的「(12 封私信 / 14 条消息)」角标**，
   展示标题请用 `.items.jsonl` 里的 `title`（那里已经干净了）。
2. **早期演示文件的 id 与文件名对不上**（这是历史命名不一致，已核实）：
   `vault_demo.json` 里的 `vault_id` 其实是 `demo-vault`，但文件叫 `vault_demo.*`；
   它对应的反库是 `antipode_demo-vault.jsonl`（6 条）。
   另一份 `antipode_demo.jsonl` 是更早的一版（同样是 6 条，把 `demo-rv-1-3` 换成了 `demo-ai-1-1`）。
   → **这些 `*demo*` 文件不要直接喂 `app_api.py`**（它按 `<platform.id>` 拼文件名，会找不到）。
   新链路一律用 `<id>` 对齐规则，以 `zhihu-*` / `github-*` 那几个文件为准。

## 不要提交的东西

`data/platforms.json`（用户自己的平台列表）与 `data/urls_*.txt`（临时链接文件）是运行时状态，
已在 `.gitignore` 里排除。新目录记得同步这条规则。
