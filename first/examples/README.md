# examples/ · 接口真实响应快照

> 全部是**服务端原样返回**，由 `app_api.py` 在真实产物上跑出来抓的，不是手写的。
> 前端可以直接照这些 JSON 写类型定义 / mock。

每份文件都是四段式：

```json
{
  "_说明": "GET /platforms → HTTP 200",
  "_请求体": null,
  "_状态码": 200,
  "_响应": { ... }        ← 真正的响应体，下面不再重复
}
```

| 文件 | 请求 | 关键看点 |
|---|---|---|
| `01_health.json` | `GET /health` | 5 个平台的能力：`auth` 文案可直接显示，`antipode:false` 的要把开关置灰 |
| `02_平台列表.json` | `GET /platforms` | 一个开反推荐（知乎，`eligible_count=9`）、一个关（GitHub，`eligible_count=0`）；**没有 token 字段** |
| `03_条目_知乎_只含可反推荐.json` | `GET /platforms/zhihu-1017T2051/items?only=eligible` | 9 条，每条带 1–2 个 `antipode`；`evidence_span` 是卡片上「原文证据」那一段 |
| `04_条目_GitHub.json` | `GET /platforms/github-new-1017T2025/items` | `antipode_eligible:false` + `antipode:[]` —— 「不支持反推荐」分支的全貌 |
| `05_开关被拒_400.json` | `PATCH /platforms/github-new-1017T2025 {"antipode_enabled":true}` | `error` 就是给用户看的原话：「GitHub 只做信息收集：仓库没有「对立面」」 |
| `06_开关成功_200.json` | `PATCH /platforms/zhihu-1017T2051 {"antipode_enabled":true}` | `changed:["antipode_enabled"]` |
| `07_不存在_404.json` | `GET /platforms/not-exist` | 错误响应形状：`{ok:false, error:"没有这个平台"}` |

## 还没抓的两个（形状稳定，照代码即可）

**`POST /platforms` 201**
```json
{"ok": true,
 "platform": { ...同 02 里的单个平台对象... },
 "job": {"id":"j-3f9a1b2c","platform_id":"zhihu-ab12cd","kind":"collect",
         "status":"running","stage":"排队中","progress":[],
         "started_at":"2026-10-17T21:20:00+08:00","finished_at":"",
         "error":"","result":{}}}
```

**`GET /jobs/{id}` 200**
```json
{"ok": true, "job": {"id":"j-3f9a1b2c","platform_id":"zhihu-ab12cd","kind":"collect",
  "status":"done",                       // running | done | failed
  "stage":"完成：10 条信息，9 条可反推荐，16 条反信息",
  "progress":["找到 10 条，开始取正文…","…"],
  "result":{"items":10,"eligible":9,"antipode":16},
  "error":"","started_at":"…","finished_at":"2026-10-17T21:35:12+08:00"}}
```
（没抓它们的原因：需要一个正在跑的采集任务，会打网络、要登录、要十几分钟。
字段与上面给的完全一致，来源是 `app_api.start_job()` 里的字面量。）

## 复现方法

```bash
# 1) 准备一个 platforms.json，让它指向 data/ 里已有的库 id（不用重新采集）
#    id 必须与文件名前缀一致，例如 id=zhihu-1017T2051 → data/vault_zhihu-1017T2051.*
# 2) 起服务
python code/app_api.py --port 8791
# 3) 取
curl -s http://127.0.0.1:8791/platforms/zhihu-1017T2051/items?only=eligible
```
