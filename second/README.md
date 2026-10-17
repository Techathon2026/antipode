# second/ · 仓库这一侧（待并入）

> 这个目录留给「采集 → 判兴趣 → 概括 → 建库」那一侧。
> 目前仓库里只有 `first/`（反仓库这一侧）已就绪，这里是占位。

## 这一侧要交付什么

一条能持续产出的链路：**从用户授权的信息源里取内容 → 按兴趣筛 → 概括 → 写进库**。
成品是两份东西：

1. 一个**库（vault）**：`{"vault_id": "...", "topic": "...", "antipode_enabled": true}`
2. 一批**库条目（vault item）**，每条至少要有 `url`

字段的完整定义在 [`../first/docs/接口契约_仓库到反仓库.md`](../first/docs/接口契约_仓库到反仓库.md) §2。

## 怎么和 first/ 对接

不需要 import 对方的代码，也不要把两边的库混在一起。三条路任选：

| 方式 | 一句话 |
|---|---|
| HTTP（推荐） | `first/code/server.py` 起服务，这边 `POST /antipode` |
| 文件 | 这边产出 `*.items.jsonl`，`first/code/run_vault.py --vault <库>` 消费 |
| Python | `sys.path` 加 `first/code`，调 `build_antipode_items()` |

细节见 [`../first/INTEGRATION.md`](../first/INTEGRATION.md)。

## 三条别踩的线

1. **只给 `summary` 会让判立场失真** —— 省流版会把作者态度抹平，请给 `url` 或 `full_text`
2. **反库要单独一个库/面板** —— 混进仓库列表，产品卖点（"仓库之外还有一层"）就没了
3. **`antipode_enabled` 是用户的选择** —— 邮件、代码 issue 这类私人事务默认关掉

## 并入时改哪里

- 只在本目录里加文件，**不要改 `first/`**（那边有自检与评测基线，改动会连带失效）
- 并入后从仓库根跑一次 `python first/code/selftest.py`，确认 15 项仍全绿
