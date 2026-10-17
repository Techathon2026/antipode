#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反仓库引擎 · HTTP 接口层

给队友（以及前端）一个能直接调的东西。**零第三方依赖**，只用标准库。

    python code/server.py                 # 默认 127.0.0.1:8787
    python code/server.py --port 9000 --host 0.0.0.0

接口一览
--------

    GET  /health              健康检查 + 能力清单（不花钱、不联网）
    GET  /issues              16 个二元议题（议题 id / 两边名字），前端下拉框直接用
    POST /antipode            一条库条目 → 若干反库条目
    POST /antipode/batch      一批库条目 → 逐条结果（单条失败不影响其它条）

请求体（/antipode）
-------------------

    {"url": "https://blog.csdn.net/xxx/article/details/yyy"}

就这一行也能跑 —— 队友只给原文章地址是常态。也可以给更多字段（见接口契约），
有了 `issue_id` + `supports` 就省掉一次模型调用：

    {"url": "...", "issue_id": "microservices_vs_monolith", "supports": "微服务"}

响应
----

    {"ok": true,
     "outcome": "produced",
     "why": "...",                      # outcome 的中文解释，前端可直接显示
     "antipode": [ {...}, ... ],        # 反库条目（契约 §3）
     "funnel": {...} }                  # 漏斗各步数字（答辩 / 调试用）

`ok` 为 true 不等于一定有反信息：`outcome` 才是"有没有、为什么没有"。

查询参数
--------

    ?offline=1      不联网的演示模式（用内置样例候选，不消耗检索与额度）
    ?top_k=3        最多返回几条反信息
    ?max_verify=8   最多送检几条候选（越大越慢越贵）
    ?enrich=0       关掉"HN 文章补评论"这类富化

安全边界（写死在这里，不做开关）
--------------------------------
* 只监听 --host 指定的地址，默认 127.0.0.1（不要暴露到公网）
* 只读：不写任何文件、不代用户登录、不代用户发言
* 请求体上限 1 MiB；单条超时由调用方控制
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                                           # Windows 控制台编码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

from antipode_engine import build_antipode_items, Funnel          # noqa: E402
from antipode_sources import RETRIEVAL_SOURCES                    # noqa: E402
from model_adapter import (                                       # noqa: E402
    OpenAICompatBackend, MockBackend, BatchJsonlBackend, LocalFileBackend,
)
from stance_extractor import ISSUE_TAXONOMY                       # noqa: E402
from run_vault import OUTCOME_LABEL, classify, _normalize_items    # noqa: E402
from build_vault import build_item, dedupe as dedupe_urls, make_opts  # noqa: E402

SERVICE = "antipode-engine"
VERSION = "0.3"
MAX_BODY = 1 << 20            # 1 MiB

BACKENDS = {
    "openai_compat": OpenAICompatBackend,
    "mock": MockBackend,
    "batch_jsonl": BatchJsonlBackend,
    "local_file": LocalFileBackend,
}


def make_backend(name: str):
    cls = BACKENDS.get(name)
    if cls is None:
        raise ValueError(f"未知后端 {name}，可选：{', '.join(BACKENDS)}")
    return cls()


def issue_catalog() -> list[dict]:
    """16 议题的对外视图：只给前端需要的东西，不暴露内部字段。"""
    out = []
    for i in ISSUE_TAXONOMY:
        out.append({
            "issue_id": i["issue_id"],
            "title": i.get("title", ""),
            "side_a": i.get("side_a", ""),
            "side_b": i.get("side_b", ""),
            "retrieval": ("ok" if (i.get("retrieval") or {}).get("status") != "none" else "none"),
        })
    return out


def funnel_dict(f: Funnel) -> dict:
    """漏斗 → 纯 JSON（前端画漏斗图直接吃）。"""
    return {
        "vault_item_id": f.vault_item_id,
        "issue_id": f.issue_id,
        "vault_supports": f.vault_supports,
        "vault_side": f.vault_side,
        "want_side": f.want_side,
        "stance_from": f.stance_from,
        "rejudge": {"tried": f.rejudge, "fixed": f.rejudge_fixed},
        "fetch": {"ok": f.fetch_ok, "chars": f.fetch_chars,
                  "method": f.fetch_method, "error": f.fetch_error},
        "candidates": f.candidates,
        "per_stage": f.per_stage,
        "per_source": f.per_source,
        "off_topic": f.off_topic,
        "self_ref": f.self_ref,
        "prescreened": f.prescreened,
        "verified": f.verify_calls,
        "passed": f.passed,
        "truncated": f.truncated,
        "accepted": f.accepted,
        "rejected": f.rejected,
        "mode": f.mode,
    }


def run_one(item: dict, vault: dict | None, opts: dict) -> dict:
    """跑一条库条目，返回可直接序列化的结果。异常也变成结果，不往外抛。"""
    t0 = time.time()
    try:
        produced, f = build_antipode_items(
            item, vault,
            backend=opts["backend"],
            sources=opts["sources"],
            origin=str(item.get("source") or ""),
            max_verify=opts["max_verify"],
            top_k=opts["top_k"],
            offline=opts["offline"],
            enrich=opts["enrich"],
            min_score=opts["min_score"],
            verbose=False,
        )
        outcome = classify(produced, f)
        return {
            "ok": True,
            "item_id": str(item.get("id") or ""),
            # 这四项与 funnel 里的同名值一致，提到顶层是为了前端不必挖漏斗
            "issue_id": f.issue_id,
            "vault_supports": f.vault_supports,
            "want_side": f.want_side,
            "outcome": outcome,
            "why": OUTCOME_LABEL.get(outcome, outcome),
            "antipode": produced,
            "antipode_count": len(produced),
            "funnel": funnel_dict(f),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except RuntimeError as e:
        # 引擎的"可预期拒绝"：中立 / 无正文 / 开关关 / 串味 —— 不是故障
        return {
            "ok": True,
            "item_id": str(item.get("id") or ""),
            "issue_id": "", "vault_supports": "", "want_side": "",
            "outcome": _outcome_of(str(e)),
            "why": str(e)[:300],
            "antipode": [],
            "antipode_count": 0,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:                                     # noqa: BLE001
        return {
            "ok": False,
            "item_id": str(item.get("id") or ""),
            "issue_id": "", "vault_supports": "", "want_side": "",
            "outcome": "error",
            "why": f"{type(e).__name__}: {str(e)[:240]}",
            "antipode": [],
            "antipode_count": 0,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }


def run_vault_build(body: dict, opts: dict) -> dict:
    """客户给的链接 → 一批库条目（抓正文 / 判立场 / 写省流）。

    只读服务：结果在响应里返回，**不写文件**（要落盘用 build_vault.py CLI）。
    """
    t0 = time.time()
    raw = body.get("urls")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    if body.get("url"):
        raw.append(body["url"])

    urls = [str(u).strip() for u in raw if str(u or "").strip()]
    if not urls:
        return {"ok": False, "error": "至少要给一个 url（urls 数组或 url 字段）"}

    max_items = max(1, min(int(body.get("max_items") or 10), 20))
    urls = dedupe_urls(urls)[:max_items]

    no_model = bool(body.get("no_model"))
    o = make_opts(no_model=no_model, max_chars=int(body.get("max_chars") or 6000))

    items = []
    for u in urls:
        try:
            items.append(build_item(u, None if no_model else opts["backend"], o))
        except Exception as e:                                 # noqa: BLE001
            items.append({"url": u, "error": f"{type(e).__name__}: {str(e)[:160]}",
                          "kind": "error", "antipode_eligible": False})

    ok_items = [i for i in items if not i.get("error")]
    slim = [{k: v for k, v in i.items() if k != "full_text"} for i in items]
    return {
        "ok": True,
        "vault_id": str(body.get("vault_id") or "adhoc"),
        "requested": len(urls),
        "built": len(ok_items),
        "failed": len(urls) - len(ok_items),
        "eligible": sum(1 for i in items if i.get("antipode_eligible")),
        "note": "full_text 没在这里返回（体量大）；要用它跑反推荐请走 build_vault.py 落盘",
        "items": slim,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def _outcome_of(msg: str) -> str:
    if "取不到正文" in msg:
        return "fetch_failed"
    if "串味" in msg:
        return "off_issue"
    if "未能从库条目" in msg:
        return "no_stance"
    if "对立面" in msg or "中立" in msg:
        return "neutral"
    if "未开启反推荐" in msg:
        return "disabled"
    if "正文" in msg or "文本" in msg:
        return "no_text"
    return "error"


class Handler(BaseHTTPRequestHandler):
    server_version = f"Antipode/{VERSION}"

    # ---------- 基础设施 ----------
    def log_message(self, fmt, *args):                          # 安静一点，只打一行摘要
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:                               # 前端跨域预检
        self._send(204, {})

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ValueError("请求体为空")
        if n > MAX_BODY:
            raise ValueError(f"请求体过大（{n} 字节 > {MAX_BODY}）")
        raw = self.rfile.read(n)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _opts(self) -> dict:
        q = parse_qs(urlparse(self.path).query)
        pick = lambda k, d: (q.get(k) or [d])[0]                 # noqa: E731
        backend_name = pick("backend", os.environ.get("ANTIPODE_BACKEND", "openai_compat"))
        return {
            "backend": make_backend(backend_name),
            "sources": tuple(s for s in pick("sources", "").split(",") if s) or RETRIEVAL_SOURCES,
            "offline": pick("offline", "0") in ("1", "true", "yes"),
            "top_k": int(pick("top_k", "3")),
            "max_verify": int(pick("max_verify", "8")),
            "min_score": float(pick("min_score", "0.7")),
            "enrich": pick("enrich", "1") not in ("0", "false", "no"),
        }

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send(200, {
                "ok": True, "service": SERVICE, "version": VERSION,
                "sources": list(RETRIEVAL_SOURCES),
                "issues": len(ISSUE_TAXONOMY),
                "backends": list(BACKENDS),
                "fetcher": True,
                "notes": "只读服务：不写文件、不代登录、不代发言",
            })
        elif path == "/issues":
            self._send(200, {"ok": True, "count": len(ISSUE_TAXONOMY),
                             "issues": issue_catalog()})
        elif path == "/":
            self._send(200, {"ok": True, "service": SERVICE, "version": VERSION,
                             "endpoints": ["/health", "/issues", "/vault",
                                           "/antipode", "/antipode/batch"]})
        else:
            self._send(404, {"ok": False, "error": f"没有这个接口：{path}"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path not in ("/antipode", "/antipode/batch", "/vault"):
            self._send(404, {"ok": False, "error": f"没有这个接口：{path}"})
            return
        try:
            body = self._read_json()
        except Exception as e:                                  # noqa: BLE001
            self._send(400, {"ok": False, "error": f"请求体不合法：{e}"})
            return

        try:
            opts = self._opts()
        except Exception as e:                                  # noqa: BLE001
            self._send(400, {"ok": False, "error": f"参数不合法：{e}"})
            return

        if path == "/vault":
            self._send(200, run_vault_build(body, opts))
            return

        if path == "/antipode":
            item = body.get("item") if isinstance(body.get("item"), dict) else body
            vault = body.get("vault") if isinstance(body.get("vault"), dict) else None
            if not str(item.get("url") or item.get("full_text") or item.get("summary") or "").strip():
                self._send(400, {"ok": False,
                                 "error": "至少要给 url（或 full_text / summary 之一）"})
                return
            item = _normalize_items([item])[0]
            self._send(200, run_one(item, vault, opts))
            return

        # ---- 批量 ----
        items = body.get("items")
        if not isinstance(items, list) or not items:
            self._send(400, {"ok": False, "error": "items 必须是非空数组"})
            return
        vault = body.get("vault") if isinstance(body.get("vault"), dict) else None
        items = _normalize_items([i for i in items if isinstance(i, dict)])
        results = [run_one(it, vault, opts) for it in items]     # 单条失败不中断
        produced = sum(len(r["antipode"]) for r in results)
        self._send(200, {
            "ok": True,
            "vault_id": (vault or {}).get("vault_id", ""),
            "items": len(results),
            "produced": produced,
            "hits": sum(1 for r in results if r["outcome"] == "produced"),
            "results": results,
        })


def main() -> int:
    ap = argparse.ArgumentParser(description="反仓库引擎 HTTP 接口")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认只本机）")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--cookies", default="", metavar="PATH",
                    help="登录态 Cookie 文件（可选，仅取用户已登录站点用）")
    args = ap.parse_args()

    if args.cookies:
        os.environ["ANTIPODE_COOKIES"] = os.path.abspath(args.cookies)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"反仓库引擎已启动  http://{args.host}:{args.port}")
    print(f"  GET  /health        健康检查")
    print(f"  GET  /issues        16 个议题")
    print(f"  POST /vault         客户给的链接 → 库条目（抓正文 / 判立场 / 写省流）")
    print(f"  POST /antipode      一条库条目 → 反库条目")
    print(f"  POST /antipode/batch 一批库条目")
    print("Ctrl-C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
