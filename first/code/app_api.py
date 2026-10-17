#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「我的信息平台」接口层 —— 给桌面应用（INFOAGENT 那类 exe）用的后端。

为什么单独一个文件
------------------
`server.py` 是给队友/前端调的**单条能力**接口（一条 → 反库条目），刻意做成只读。
桌面应用要的是**状态**：用户加了哪些平台、每个平台一个库、库里有哪些条目、
这条有没有反推荐、反推荐开没开。状态要落盘、要能恢复，所以单独一层。

    python code/app_api.py            # 默认 127.0.0.1:8790

左侧菜单「我的信息平台」需要的最小能力
--------------------------------------
    GET    /platforms                 我加了哪些平台（卡片列表用）
    POST   /platforms                 添加平台 → 开始采集（异步，返回 job_id）
    GET    /platforms/{id}            单个平台详情
    PATCH  /platforms/{id}            开关反推荐（按库，用户自己决定）
    DELETE /platforms/{id}            移除平台（连带库文件）
    POST   /platforms/{id}/refresh    重新采一次
    GET    /platforms/{id}/items      这个库里筛出来的信息（含反推荐）
    GET    /jobs/{id}                 采集进度（前端轮询，显示"正在读第 3 篇"）

三条不做的事（安全边界，写死）
------------------------------
1. **不收密码**。GitHub 用只读 token；知乎/抖音/微博是"弹窗让人自己登录一次"，
   程序只复用登录态目录，不接触密码、不代登录、不代发言。
2. **不把登录态返回给前端**。Cookie 只在本地采集进程里用，接口里没有这个字段。
3. **不默认监听外网**。默认 127.0.0.1 —— 这是本机应用的后端，不是云服务。

反推荐为什么要做成显式开关
--------------------------
反推荐不是万能配料：邮件、issue、代码仓库没有"对立面"；而观点类文章才有。
所以开关**按库**存（`antipode_enabled`），关掉之后 `run_vault` 会直接拒绝生成，
而不是"跑了但不显示" —— 用户的选择应该体现在产物上，不是 UI 的遮罩。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIRST = os.path.dirname(_HERE)
_DATA = os.path.join(_FIRST, "data")
_STORE = os.path.join(_DATA, "platforms.json")

try:                                                           # Windows 控制台编码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

SERVICE = "antipode-platforms"
VERSION = "0.1"
MAX_BODY = 256 << 10

# ---------------------------------------------------------------------------
# 支持哪些平台
# ---------------------------------------------------------------------------
# `collector` 决定"加了这个平台之后跑哪条采集命令"；
# `auth` 是给界面写提示用的，不是程序逻辑 —— 界面上要如实告诉用户
# 这一步需要他做什么（登录一次 / 填 token / 什么都不用）。
PLATFORMS = {
    "github": {
        "label": "GitHub",
        "hint": "新发布的项目（读 README）；留空就取最热的新项目",
        "auth": "可选：只读 token（不填会读本机已有凭据）",
        "antipode": False,          # 代码仓库没有观点立场，不做反推荐
        "antipode_note": "GitHub 只做信息收集：仓库没有「对立面」",
    },
    "zhihu": {
        "label": "知乎",
        "hint": "填一个议题关键词（如「微服务」）；留空取热榜",
        "auth": "第一次需要弹窗登录（登录态存在本机，之后不用再登）",
        "antipode": True,
        "antipode_note": "观点长文，可开启反推荐（按条判断是否适合）",
    },
    "douyin": {
        "label": "抖音",
        "hint": "你的主页网址，或留空让你在窗口里自己进去",
        "auth": "第一次需要弹窗登录（视频正文短，多数只归档）",
        "antipode": False,
        "antipode_note": "视频详情页正文太短，判不出立场",
    },
    "weibo": {
        "label": "微博",
        "hint": "你的主页网址",
        "auth": "第一次需要弹窗登录",
        "antipode": False,
        "antipode_note": "短文本为主，多数只归档",
    },
    "url": {
        "label": "链接",
        "hint": "一串文章链接，一行一个",
        "auth": "不需要登录（公开文章）",
        "antipode": True,
        "antipode_note": "观点文章可开启反推荐",
    },
}

_LOCK = threading.Lock()          # 保护 platforms.json 与 jobs
JOBS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_platforms() -> list[dict]:
    if not os.path.exists(_STORE):
        return []
    try:
        with open(_STORE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:                                          # noqa: BLE001
        return []


def save_platforms(rows: list[dict]) -> None:
    os.makedirs(_DATA, exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _STORE)


def vault_paths(pid: str) -> tuple[str, str, str]:
    """库 / 条目 / 反库 三个文件路径。文件名的前缀必须是 pid，
    采集脚本靠 --vault-id 对齐，否则读不到自己刚采的东西。"""
    return (os.path.join(_DATA, f"vault_{pid}.json"),
            os.path.join(_DATA, f"vault_{pid}.items.jsonl"),
            os.path.join(_DATA, f"antipode_{pid}.jsonl"))


def read_items(pid: str) -> list[dict]:
    _, ipath, apath = vault_paths(pid)
    items: list[dict] = []
    if os.path.exists(ipath):
        with open(ipath, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    anti: dict[str, list[dict]] = {}
    if os.path.exists(apath):
        with open(apath, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    a = json.loads(line)
                except json.JSONDecodeError:
                    continue
                anti.setdefault(str(a.get("for_vault_item") or ""), []).append(a)
    for it in items:
        it["antipode"] = anti.get(str(it.get("id") or ""), [])
    return items


def _counts(pid: str) -> tuple[int, int, int]:
    items = read_items(pid)
    return (len(items),
            sum(1 for i in items if i.get("antipode_eligible")),
            sum(len(i.get("antipode") or []) for i in items))


def _public(p: dict) -> dict:
    """给前端看的平台对象。**不含任何凭据**。"""
    out = dict(p)
    for k in ("token", "cookie", "password"):
        out.pop(k, None)
    meta = PLATFORMS.get(p.get("platform", ""), {})
    out["label"] = p.get("label") or meta.get("label", p.get("platform", ""))
    out["antipode_supported"] = bool(meta.get("antipode"))
    return out


# ---------------------------------------------------------------------------
# 采集任务
# ---------------------------------------------------------------------------
def _py() -> str:
    return sys.executable


def _build_cmd(p: dict) -> list[str]:
    """平台记录 → 采集命令。

    这里不重写采集逻辑，只做参数映射 —— 采集脚本各自都能单跑，
    桌面应用只是把它们串起来。这样命令行、向导、接口三条路走的是同一份代码，
    不会出现"界面上能采、命令行采不到"的分裂。
    """
    pid = p["id"]
    plat = p["platform"]
    limit = int(p.get("limit") or 10)
    q = str(p.get("input") or "").strip()

    if plat == "github":
        cmd = [_py(), os.path.join(_HERE, "collect_github.py"),
               "--vault-id", pid, "--limit", str(limit),
               "--days", str(int(p.get("days") or 7))]
        if p.get("token"):
            cmd += ["--token", str(p["token"])]
        if p.get("lang"):
            cmd += ["--lang", str(p["lang"])]
        return cmd

    if plat == "zhihu":
        cmd = [_py(), os.path.join(_HERE, "collect_zhihu.py"),
               "--vault-id", pid, "--limit", str(limit), "--wait", "120"]
        # 有关键词就按议题搜（这条才有反推荐）；没有就取热榜（只做信息收集）
        if q:
            cmd += ["--query", q]
        return cmd

    if plat in ("douyin", "weibo"):
        # --url 是必填项（脚本要据此判平台），所以没给就落到该平台首页，
        # 让人在弹窗里自己进主页 —— 与向导版的行为一致。
        home = {"douyin": "https://www.douyin.com/",
                "weibo": "https://weibo.com/"}[plat]
        cmd = [_py(), os.path.join(_HERE, "collect_account.py"),
               "--url", q or home,
               "--vault-id", pid, "--limit", str(limit), "--browser"]
        return cmd

    # 一串公开链接：写进临时文件交给 build_vault
    urls_file = os.path.join(_DATA, f"urls_{pid}.txt")
    lines = [x.strip() for x in q.splitlines() if x.strip()]
    with open(urls_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return [_py(), os.path.join(_HERE, "build_vault.py"),
            "--urls", urls_file, "--vault-id", pid, "--limit", str(limit)]


def start_job(pid: str, kind: str = "collect") -> dict:
    job = {"id": "j-" + uuid.uuid4().hex[:8], "platform_id": pid, "kind": kind,
           "status": "running", "stage": "排队中", "progress": [],
           "started_at": _now(), "finished_at": "", "error": "", "result": {}}
    with _LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job


def _run_job(job: dict) -> None:
    pid = job["platform_id"]
    try:
        with _LOCK:
            rows = load_platforms()
            p = next((r for r in rows if r["id"] == pid), None)
        if p is None:
            raise RuntimeError("平台不存在（可能已被移除）")

        job["stage"] = f"正在从{PLATFORMS.get(p['platform'], {}).get('label', p['platform'])}采集"
        cmd = _build_cmd(p)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(cmd, cwd=_FIRST, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            job["progress"].append(line[:200])
            if len(job["progress"]) > 200:                 # 只留最近 200 行
                job["progress"] = job["progress"][-200:]
            # 把采集脚本的中文进度直接当 stage —— 用户在界面上看到的就是这句
            if any(k in line for k in ("取正文", "判立场", "入库", "找到", "滚了")):
                job["stage"] = line.strip()[:80]
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"采集脚本返回 {rc}")

        n, eligible, _ = _counts(pid)
        job["result"].update({"items": n, "eligible": eligible})

        # ---- 需要的话接着跑反推荐 -------------------------------------
        if p.get("antipode_enabled") and eligible and PLATFORMS.get(p["platform"], {}).get("antipode"):
            job["stage"] = f"正在为 {eligible} 条适合的内容找反面"
            vpath, _, _ = vault_paths(pid)
            cmd2 = [_py(), os.path.join(_HERE, "run_vault.py"), "--vault", vpath]
            proc2 = subprocess.Popen(cmd2, cwd=_FIRST, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     bufsize=1)
            assert proc2.stdout is not None
            for line in proc2.stdout:
                line = line.rstrip()
                if line:
                    job["progress"].append(line[:200])
            if len(job["progress"]) > 200:
                job["progress"] = job["progress"][-200:]
            proc2.wait()

        n, eligible, produced = _counts(pid)
        job["status"] = "done"
        job["stage"] = f"完成：{n} 条信息，{eligible} 条可反推荐，{produced} 条反信息"
        job["result"].update({"items": n, "eligible": eligible, "antipode": produced})
    except Exception as e:                                     # noqa: BLE001
        job["status"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}"
        job["stage"] = "失败：" + job["error"][:70]
    finally:
        job["finished_at"] = _now()
        with _LOCK:
            rows = load_platforms()
            for r in rows:
                if r["id"] == pid:
                    n, eligible, produced = _counts(pid)
                    r.update({"item_count": n, "eligible_count": eligible,
                              "antipode_count": produced,
                              "status": job["status"],
                              "last_stage": job["stage"],
                              "last_job": job["id"],
                              "last_error": job["error"],
                              "last_run": job["finished_at"]})
            save_platforms(rows)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVICE}/{VERSION}"

    def log_message(self, fmt, *args):                          # noqa: A003
        pass

    # -- 工具 ----------------------------------------------------------
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:                               # noqa: N802
        self._send(200, {"ok": True})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("请求体太大")
        raw = self.rfile.read(n).decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}

    # -- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:                                   # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = parse_qs(u.query)

        if path == "/health":
            self._send(200, {"ok": True, "service": SERVICE, "version": VERSION,
                            "platforms": {k: {"label": v["label"],
                                              "auth": v["auth"],
                                              "antipode": v["antipode"]}
                                          for k, v in PLATFORMS.items()},
                            "notes": "只监听本机；不存密码；登录态不出本地"})
            return

        if path == "/platforms":
            with _LOCK:
                rows = load_platforms()
            self._send(200, {"ok": True,
                             "count": len(rows),
                             "platforms": [_public(r) for r in rows]})
            return

        m = re.match(r"^/platforms/([\w.-]+)$", path)
        if m:
            with _LOCK:
                p = next((r for r in load_platforms() if r["id"] == m.group(1)), None)
            if not p:
                self._send(404, {"ok": False, "error": "没有这个平台"})
                return
            self._send(200, {"ok": True, "platform": _public(p)})
            return

        m = re.match(r"^/platforms/([\w.-]+)/items$", path)
        if m:
            pid = m.group(1)
            only = (q.get("only") or ["all"])[0]
            kw = (q.get("q") or [""])[0].strip()
            items = read_items(pid)
            if only == "eligible":
                items = [i for i in items if i.get("antipode_eligible")]
            if kw:
                items = [i for i in items
                         if kw in str(i.get("title", "")) or kw in str(i.get("summary", ""))]
            out = []
            for i in items:
                out.append({
                    "id": i.get("id", ""),
                    "title": i.get("title", ""),
                    "url": i.get("url", ""),
                    "summary": i.get("summary", ""),
                    "kind": i.get("kind", ""),
                    # GitHub 条目记的是 readme_chars（它读的是 README），
                    # 其它条目记 text_chars。前端只想要一个"这篇多长"。
                    "text_chars": i.get("text_chars") or i.get("readme_chars") or 0,
                    "antipode_eligible": bool(i.get("antipode_eligible")),
                    "antipode_reason": i.get("eligibility_reason", "")
                                       or i.get("antipode_reason", ""),
                    "antipode": [{
                        "title": a.get("title", ""),
                        "url": a.get("url", ""),
                        "takes_side": a.get("takes_side", ""),
                        "summary": a.get("summary", ""),
                        "opposition_score": a.get("opposition_score", 0),
                        "evidence_span": a.get("evidence_span", ""),
                        "synthesized": bool(a.get("synthesized")),
                    } for a in (i.get("antipode") or [])],
                })
            self._send(200, {"ok": True, "count": len(out), "items": out})
            return

        m = re.match(r"^/jobs/([\w.-]+)$", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                self._send(404, {"ok": False, "error": "没有这个任务"})
                return
            self._send(200, {"ok": True, "job": job})
            return

        if path == "/":
            self._send(200, {"ok": True, "service": SERVICE, "version": VERSION,
                             "endpoints": [
                                 "GET /health", "GET /platforms", "POST /platforms",
                                 "GET /platforms/{id}", "PATCH /platforms/{id}",
                                 "DELETE /platforms/{id}", "POST /platforms/{id}/refresh",
                                 "GET /platforms/{id}/items", "GET /jobs/{id}"]})
            return

        self._send(404, {"ok": False, "error": f"没有这个接口：{path}"})

    def do_POST(self) -> None:                                  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")

        if path == "/platforms":
            try:
                body = self._body()
            except Exception as e:                              # noqa: BLE001
                self._send(400, {"ok": False, "error": f"请求体不合法：{e}"})
                return
            plat = str(body.get("platform") or "").strip().lower()
            if plat not in PLATFORMS:
                self._send(400, {"ok": False,
                                 "error": f"不支持的平台：{plat}",
                                 "supported": list(PLATFORMS)})
                return
            pid = str(body.get("id") or "").strip() or f"{plat}-{uuid.uuid4().hex[:6]}"
            with _LOCK:
                rows = load_platforms()
                if any(r["id"] == pid for r in rows):
                    self._send(409, {"ok": False, "error": f"平台 id 已存在：{pid}"})
                    return
                rec = {
                    "id": pid,
                    "platform": plat,
                    "label": str(body.get("label") or PLATFORMS[plat]["label"]),
                    "input": str(body.get("input") or ""),
                    "limit": int(body.get("limit") or 10),
                    "days": int(body.get("days") or 7),
                    "lang": str(body.get("lang") or ""),
                    "token": str(body.get("token") or ""),   # 只在本地落盘，不入接口返回
                    # 反推荐默认 **关**：平台支持是一回事，用户要不要是另一回事
                    "antipode_enabled": bool(body.get("antipode_enabled", False))
                                        and PLATFORMS[plat]["antipode"],
                    "status": "running",
                    "item_count": 0, "eligible_count": 0, "antipode_count": 0,
                    "created_at": _now(), "last_run": "", "last_stage": "已创建",
                    "last_error": "", "last_job": "",
                }
                rows.append(rec)
                save_platforms(rows)
            job = start_job(pid, "collect")
            with _LOCK:
                rows = load_platforms()
                for r in rows:
                    if r["id"] == pid:
                        r["last_job"] = job["id"]
                save_platforms(rows)
            self._send(201, {"ok": True, "platform": _public(rec), "job": job})
            return

        m = re.match(r"^/platforms/([\w.-]+)/refresh$", path)
        if m:
            pid = m.group(1)
            with _LOCK:
                p = next((r for r in load_platforms() if r["id"] == pid), None)
            if not p:
                self._send(404, {"ok": False, "error": "没有这个平台"})
                return
            job = start_job(pid, "refresh")
            self._send(202, {"ok": True, "job": job})
            return

        self._send(404, {"ok": False, "error": f"没有这个接口：{path}"})

    def do_PATCH(self) -> None:                                 # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        m = re.match(r"^/platforms/([\w.-]+)$", path)
        if not m:
            self._send(404, {"ok": False, "error": "没有这个接口"})
            return
        pid = m.group(1)
        try:
            body = self._body()
        except Exception as e:                                  # noqa: BLE001
            self._send(400, {"ok": False, "error": f"请求体不合法：{e}"})
            return
        with _LOCK:
            rows = load_platforms()
            p = next((r for r in rows if r["id"] == pid), None)
            if not p:
                self._send(404, {"ok": False, "error": "没有这个平台"})
                return
            touched = []
            if "antipode_enabled" in body:
                want = bool(body["antipode_enabled"])
                if want and not PLATFORMS.get(p["platform"], {}).get("antipode"):
                    self._send(400, {"ok": False,
                                     "error": PLATFORMS[p["platform"]]["antipode_note"]})
                    return
                p["antipode_enabled"] = want
                touched.append("antipode_enabled")
            for k in ("label", "input", "limit"):
                if k in body:
                    p[k] = body[k] if k == "label" or k == "input" else int(body[k])
                    touched.append(k)
            save_platforms(rows)
        self._send(200, {"ok": True, "platform": _public(p), "changed": touched})

    def do_DELETE(self) -> None:                                # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        m = re.match(r"^/platforms/([\w.-]+)$", path)
        if not m:
            self._send(404, {"ok": False, "error": "没有这个接口"})
            return
        pid = m.group(1)
        with _LOCK:
            rows = load_platforms()
            keep = [r for r in rows if r["id"] != pid]
            if len(keep) == len(rows):
                self._send(404, {"ok": False, "error": "没有这个平台"})
                return
            save_platforms(keep)
        # 连带清掉这个平台的库文件（只删自己的前缀，别误伤别的库）
        removed = []
        for p in vault_paths(pid):
            if os.path.exists(p):
                try:
                    os.remove(p)
                    removed.append(os.path.basename(p))
                except OSError:
                    pass
        self._send(200, {"ok": True, "removed": removed})


def main() -> int:
    ap = argparse.ArgumentParser(description="「我的信息平台」接口层（桌面应用后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"「我的信息平台」后端已启动  http://{args.host}:{args.port}")
    print("  GET  /platforms              我加了哪些平台")
    print("  POST /platforms              添加平台（异步采集 + 可选反推荐）")
    print("  GET  /platforms/{id}/items   这个库里筛出来的信息（含反推荐）")
    print("  PATCH /platforms/{id}        开关反推荐")
    print("Ctrl-C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
